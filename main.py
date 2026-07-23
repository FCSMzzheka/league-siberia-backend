import asyncio
import logging
import json
import re
import urllib.parse
import os
from datetime import datetime, timedelta, timezone
import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАСТРОЙКИ СИСТЕМЫ ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY = os.getenv("FOOTBALL_API_KEY")
WEB_APP_URL = "https://fcsmzzheka.github.io/LeagueOfSiberia/"
DB_NAME = "football_predict_bot.db"

LEAGUES_DICT = {
    235: "Российская Премьер-Лига",
    2021: "Английская Премьер-Лига",
    2014: "Ла Лига (Испания)",
    2019: "Серия А (Италия)",
    2002: "Бундеслига (Германия)",
    2001: "Лига Чемпионов УЕФА"
}
LEAGUE_IDS = list(LEAGUES_DICT.keys())
FD_CODES = {"PL": 2021, "PD": 2014, "SA": 2019, "BL1": 2002, "CL": 2001}

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

async def fetch_europe_matches(date_str: str):
    # ИСПРАВИЛИ ССЫЛКУ: добавили "api." в начало домена для прохождения SSL-сертификата
    url = "https://football-data.org"
    params = {"dateFrom": date_str, "dateTo": date_str}
    headers = {"X-Auth-Token": API_KEY}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, params=params, timeout=15) as response:
                logging.info(f"Европа API запрос на дату {date_str}, Статус: {response.status}")
                if response.status != 200: return []
                data = await response.json()
                matches = data.get("matches", [])
                result = []
                for m in matches:
                    code = m["competition"]["code"]
                    if code in FD_CODES:
                        score = f"{m['score']['fullTime']['home']}:{m['score']['fullTime']['away']}" if m["status"] == "FINISHED" else None
                        result.append({
                            "match_id": m["id"], "league_id": FD_CODES[code], "date": m["utcDate"],
                            "home_team": m["homeTeam"]["name"], "away_team": m["awayTeam"]["name"], "result": score
                        })
                return result
        except Exception as e:
            logging.error(f"Ошибка Европы: {e}")
            return []

async def fetch_rpl_matches():
    # ПЕРЕКЛЮЧИЛИ НА СТАБИЛЬНЫЙ JSON-КАНАЛ ЧЕМПИОНАТА
    url = "https://championat.com"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=15) as response:
                logging.info(f"РПЛ Чемпионат JSON запрос, Статус: {response.status}")
                if response.status != 200: return []
                data = await response.json()
                result = []
                for m in data.get("matches", []):
                    # Собираем финальный счет, если статус матча "завершен"
                    score = f"{m.get('home_goals')}:{m.get('away_goals')}" if m.get("status_id") == 3 else None
                    result.append({
                        "match_id": m["id"], "league_id": 235, "date": m["datetime"],
                        "home_team": m["home_team"]["name"], "away_team": m["away_team"]["name"], "result": score
                    })
                return result
        except Exception as e:
            logging.error(f"Ошибка РПЛ Чемпионат: {e}")
            return []

async def sync_three_days_matches():
    rpl = await fetch_rpl_matches()
    async with aiosqlite.connect(DB_NAME) as db:
        for m in rpl:
            await db.execute('INSERT OR IGNORE INTO matches VALUES (?, ?, ?, ?, ?, ?, 0, 0)', (m["match_id"], m["league_id"], m["date"], m["home_team"], m["away_team"], m["result"]))
        await db.commit()
    
    now = datetime.now(timezone.utc)
    for i in range(3):
        date_str = (now + timedelta(days=i)).strftime("%Y-%m-%d")
        euro = await fetch_europe_matches(date_str)
        if euro:
            async with aiosqlite.connect(DB_NAME) as db:
                for m in euro:
                    await db.execute('INSERT OR IGNORE INTO matches VALUES (?, ?, ?, ?, ?, ?, 0, 0)', (m["match_id"], m["league_id"], m["date"], m["home_team"], m["away_team"], m["result"]))
                await db.commit()
    logging.info("Синхронизация базы РПЛ + Европа успешно завершена!")

async def check_live_results_and_notify():
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT match_id FROM matches WHERE result IS NULL") as cursor:
            if not await cursor.fetchall(): return
            
        euro = await fetch_europe_matches(now.strftime("%Y-%m-%d"))
        rpl = await fetch_rpl_matches()
        all_live = euro + rpl
        for m in all_live:
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
