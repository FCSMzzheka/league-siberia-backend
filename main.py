import asyncio
import logging
import json
import re
import urllib.parse
import os
from datetime import datetime, timedelta, timezone
import aiohttp
import aiosqlite
import cloudscraper  # Подключаем инструмент обхода защиты сайтов
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАСТРОЙКИ СИСТЕМЫ ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEB_APP_URL = "https://fcsmzzheka.github.io/LeagueOfSiberia/"
DB_NAME = "football_predict_bot.db"

LEAGUES_DICT = {
    235: "Российская Премьер-Лига",
    39: "Английская Премьер-Лига",
    140: "Ла Лига (Испания)",
    135: "Серия А (Италия)",
    78: "Бундеслига (Германия)",
    2: "Лига Чемпионов УЕФА"
}
LEAGUE_IDS = list(LEAGUES_DICT.keys())

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

def escape_md(text: str) -> str:
    return re.sub(f'([{re.escape(r"_*[]()~`>#+-=|{}.!")}])', r'\\\1', str(text))

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS matches (
                match_id INTEGER PRIMARY KEY, league_id INTEGER, date TEXT,
                home_team TEXT, away_team TEXT, result TEXT,
                upcoming_notified INTEGER DEFAULT 0, finished_notified INTEGER DEFAULT 0
            )
        ''')
        await db.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT)')
        await db.execute('CREATE TABLE IF NOT EXISTS user_leagues (user_id INTEGER, league_id INTEGER, points INTEGER DEFAULT 0, PRIMARY KEY (user_id, league_id))')
        await db.execute('CREATE TABLE IF NOT EXISTS predictions (user_id INTEGER, match_id INTEGER, predicted_score TEXT, PRIMARY KEY (user_id, match_id))')
        await db.commit()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or f"id{user_id}"
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
        for l_id in LEAGUE_IDS:
            await db.execute("INSERT OR IGNORE INTO user_leagues (user_id, league_id, points) VALUES (?, ?, 0)", (user_id, l_id))
        await db.commit()
        query_matches = "SELECT match_id, league_id, date, home_team, away_team FROM matches WHERE result IS NULL ORDER BY date ASC"
        async with db.execute(query_matches) as cursor:
            matches_rows = await cursor.fetchall()
        query_leaders = """
            SELECT ul.league_id, u.username, ul.points FROM user_leagues ul 
            JOIN users u ON ul.user_id = u.user_id ORDER BY ul.league_id, ul.points DESC
        """
        async with db.execute(query_leaders) as cursor:
            leaders_rows = await cursor.fetchall()

    matches_list = []
    for r in matches_rows:
        matches_list.append({"id": r[0], "league": LEAGUES_DICT.get(r[1], "Турнир"), "date": r[2], "home": r[3], "away": r[4]})
    leaders_dict = {l_id: [] for l_id in LEAGUE_IDS}
    for l_id, u_name, pts in leaders_rows:
        if l_id in leaders_dict and len(leaders_dict[l_id]) < 10:
            leaders_dict[l_id].append({"username": u_name, "points": pts})

    init_data = {"matches": matches_list, "leaderboards": leaders_dict, "leagues": LEAGUES_DICT}
    encoded_data = urllib.parse.urlencode({"data": json.dumps(init_data)})
    final_url = f"{WEB_APP_URL}?{encoded_data}"
    builder = InlineKeyboardBuilder()
    builder.button(text="ОТКРЫТЬ МАТЧ-ЦЕНТР 📱", web_app=types.WebAppInfo(url=final_url))
    text = (
        "<b>📊 КОНКУРС ПРОГНОЗОВ</b>\n\n"
        f"Учетная запись <b>@{username}</b> успешно активирована.\n\n"
        "Нажмите на кнопку ниже, чтобы открыть графический интерфейс матчей и таблиц:"
    )
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.message(F.content_type == types.ContentType.WEB_APP_DATA)
async def process_web_app_data(message: types.Message):
    try:
        data = json.loads(message.web_app_data.data)
        if data.get("action") == "predict":
            match_id = int(data.get("match_id"))
            score = data.get("score")
            user_id = message.from_user.id
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute("SELECT home_team, away_team FROM matches WHERE match_id = ?", (match_id,)) as cursor:
                    match = await cursor.fetchone()
                if match:
                    await db.execute('INSERT OR REPLACE INTO predictions VALUES (?, ?, ?)', (user_id, match_id, score))
                    await db.commit()
                    await message.answer(f"✅ <b>Прогноз внесен в реестр</b>\n\n⚔️ {escape_md(match[0])} — {escape_md(match[1])}\n🔮 Ставка: <code>{score}</code>", parse_mode="HTML")
    except Exception as e: logging.error(f"Ошибка Web App: {e}")

def calculate_predicted_points(predict_str: str, result_str: str) -> int:
    try:
        p_home, p_away = map(int, predict_str.split(":"))
        r_home, r_away = map(int, result_str.split(":"))
    except: return 0
    if p_home == r_home and p_away == r_away: return 5
    if (p_home - p_away) == (r_home - r_away): return 3
    if ((p_home - p_away) > 0 and (r_home - r_away) > 0) or ((p_home - p_away) < 0 and (r_home - r_away) < 0): return 2
    return 0

def fetch_games_via_scraper():
    """Бронебойный автоматический сбор расписания РПЛ и Европы в обход любых блокировок сайтов"""
    scraper = cloudscraper.create_scraper()
    # Стучимся на открытый спортивный новостной хаб, защищенный Cloudflare
    url = "https://sports.ru"
    
    try:
        response = scraper.get(url, timeout=15)
        logging.info(f"Запрос к спортивному серверу. Статус: {response.status_code}")
        if response.status_code != 200: return []
        
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Находим блоки всех футбольных матчей на ближайшие дни
        match_cards = soup.find_all('div', class_='match-teaser')
        result_matches = []
        
        # Карта лиг для распределения по вкладкам
        league_keywords = {
            "Россия": 235, "Англия": 39, "Испания": 140, "Италия": 135, "Германия": 78, "Лига чемпионов": 2
        }
        
        for card in match_cards:
            try:
                league_text = card.find('div', class_='tournament').get_text(strip=True)
                l_id = None
                for key, val in league_keywords.items():
                    if key in league_text:
                        l_id = val
                        break
                
                if l_id:
                    home = card.find('div', class_='team-home').get_text(strip=True)
                    away = card.find('div', class_='team-away').get_text(strip=True)
                    m_time = card.find('div', class_='match-time').get_text(strip=True)
                    
                    # Проверяем результат, если матч сыгран
                    score_element = card.find('div', class_='match-score')
                    score = score_element.get_text(strip=True).replace(" ", "") if score_element else None
                    if score and ":" not in score: score = None
                    
                    m_id = abs(hash(home + away + m_time)) % 1000000
                    result_matches.append({
                        "match_id": m_id, "league_id": l_id, "date": m_time, "home_team": home, "away_team": away, "result": score
                    })
            except: continue
        return result_matches
    except Exception as e:
        logging.error(f"Ошибка автоматического сбора данных: {e}")
        return []

async def sync_three_days_matches():
    # Запускаем синхронизацию через встроенный пул, так как cloudscraper работает синхронно
    loop = asyncio.get_event_loop()
    matches = await loop.run_in_executor(None, fetch_games_via_scraper)
    if matches:
        async with aiosqlite.connect(DB_NAME) as db:
            for m in matches:
                await db.execute('INSERT OR IGNORE INTO matches VALUES (?, ?, ?, ?, ?, ?, 0, 0)', (m["match_id"], m["league_id"], m["date"], m["home_team"], m["away_team"], m["result"]))
            await db.commit()
        logging.info(f"Автоматика сработала. База SQLite наполнилась матчами: {len(matches)}")

async def check_live_results_and_notify():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT match_id FROM matches WHERE result IS NULL") as cursor:
            if not await cursor.fetchall(): return
        
        loop = asyncio.get_event_loop()
        web_matches = await loop.run_in_executor(None, fetch_games_via_scraper)
        
        for m in web_matches:
            if m["result"] is not None:
                async with db.execute("SELECT finished_notified, league_id FROM matches WHERE match_id = ?", (m["match_id"],)) as res_cur:
                    row = await res_cur.fetchone()
                    if row and row[0] == 0:
                        league_id = row[1]
                        await db.execute("UPDATE matches SET result = ?, finished_notified = 1 WHERE match_id = ?", (m["result"], m["match_id"]))
                        async with db.execute("SELECT user_id, predicted_score FROM predictions WHERE match_id = ?", (m["match_id"],)) as pred_cursor:
                            async for user_id, predicted_score in pred_cursor:
                                earned_points = calculate_predicted_points(predicted_score, m["result"])
                                if earned_points > 0:
                                    await db.execute("UPDATE user_leagues SET points = points + ? WHERE user_id = ? AND league_id = ?", (earned_points, user_id, league_id))
                                    try:
                                        alert_text = f"📊 <b>МАТЧ ЗАВЕРШЕН: {LEAGUES_DICT[league_id]}</b>\n⚽ {m['home_team']} {m['result']} {m['away_team']}\n\nВаш прогноз: <code>{predicted_score}</code>\nНачислено баллов: <b>+{earned_points}</b>"
                                        await bot.send_message(chat_id=user_id, text=alert_text, parse_mode="HTML")
                                    except: pass
        await db.commit()

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    scheduler.add_job(sync_three_days_matches, 'cron', hour=6, minute=0)
    scheduler.add_job(sync_three_days_matches, 'cron', hour=14, minute=0)
    scheduler.add_job(check_live_results_and_notify, 'interval', minutes=30)
    scheduler.start()
    asyncio.create_task(sync_three_days_matches())
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): pass
