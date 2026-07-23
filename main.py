import asyncio
import logging
import json
import re
from datetime import datetime, timedelta, timezone
import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАСТРОЙКИ СИСТЕМЫ ---
BOT_TOKEN = "8076069178:AAEXQCMwSJEswUlsL44AHndok5hZ58XEmtQ"
WEB_APP_URL = "https://github.io"
API_KEY = "feb9b5e694287da782e4d92deee40c20"
DB_NAME = "football_predict_bot.db"

# Конфигурация лиг (РПЛ на первом месте)
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
    """Экранирование служебных символов для Telegram Markdown v2"""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', str(text))

# --- ИНИЦИАЛИЗАЦИЯ БД ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS matches (
                match_id INTEGER PRIMARY KEY,
                league_id INTEGER,
                date TEXT,
                home_team TEXT,
                away_team TEXT,
                result TEXT,
                upcoming_notified INTEGER DEFAULT 0,
                finished_notified INTEGER DEFAULT 0
            )
        ''')
        await db.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT)')
        await db.execute('CREATE TABLE IF NOT EXISTS user_leagues (user_id INTEGER, league_id INTEGER, points INTEGER DEFAULT 0, PRIMARY KEY (user_id, league_id))')
        await db.execute('CREATE TABLE IF NOT EXISTS predictions (user_id INTEGER, match_id INTEGER, predicted_score TEXT, PRIMARY KEY (user_id, match_id))')
        await db.commit()

# --- СТАРТОВАЯ КОМАНДА ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or f"id{user_id}"
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
        for l_id in LEAGUE_IDS:
            await db.execute("INSERT OR IGNORE INTO user_leagues (user_id, league_id, points) VALUES (?, ?, 0)", (user_id, l_id))
        await db.commit()
        
    builder = InlineKeyboardBuilder()
    builder.button(
        text="ОТКРЫТЬ МАТЧ-ЦЕНТР 📱", 
        web_app=types.WebAppInfo(url=WEB_APP_URL)
    )
    
    text = (
        "📊 *АНАЛИТИЧЕСКАЯ СИСТЕМА ПРОГНОЗИРОВАНИЯ*\n\n"
        f"Учетная запись *@{escape_md(username)}* успешно активирована\.\n\n"
        "Используйте кнопку ниже, чтобы открыть графический интерфейс, "
        "выставить прогнозы на матчи РПЛ и мировых лиг, а также проверить турнирные таблицы лидеров:"
    )
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="MarkdownV2")

# --- ПРИЕМ ПРОГНОЗОВ ИЗ WEB APP ИНТЕРФЕЙСА ---
@dp.message(F.content_type == types.ContentType.WEB_APP_DATA)
async def process_web_app_data(message: types.Message):
    try:
        raw_data = message.web_app_data.data
        data = json.loads(raw_data)
        
        if data.get("action") == "predict":
            match_id = int(data.get("match_id"))
            score = data.get("score")
            user_id = message.from_user.id
            
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute("SELECT home_team, away_team FROM matches WHERE match_id = ?", (match_id,)) as cursor:
                    match = await cursor.fetchone()
                    
                if match:
                    home, away = match
                    await db.execute('INSERT OR REPLACE INTO predictions VALUES (?, ?, ?)', (user_id, match_id, score))
                    await db.commit()
                    
                    await message.answer(
                        "✅ *Прогноз внесен в реестр*\n\n"
                        f"Событие: {escape_md(home)} — {escape_md(away)}\n"
                        f"Ставка: `{escape_md(score)}`", 
                        parse_mode="MarkdownV2"
                    )
                else:
                    await message.answer("❌ Ошибка: Матч не найден в локальной базе данных\.")
    except Exception as e:
        logging.error(f"Ошибка Web App: {e}")
        await message.answer("❌ Ошибка при регистрации прогноза\.")

# --- СЛУЖЕБНЫЙ АВТОМАТИЧЕСКИЙ БЛОК API И РАСЧЕТОВ ОЧКОВ ---
def calculate_predicted_points(predict_str: str, result_str: str) -> int:
    try:
        p_home, p_away = map(int, predict_str.split(":"))
        r_home, r_away = map(int, result_str.split(":"))
    except: return 0
    if p_home == r_home and p_away == r_away: return 5
    if (p_home - p_away) == (r_home - r_away): return 3
    if ((p_home - p_away) > 0 and (r_home - r_away) > 0) or ((p_home - p_away) < 0 and (r_home - r_away) < 0): return 2
    return 0

async def fetch_matches_from_api(date_str: str):
    url = f"https://api-sports.io{date_str}"
    headers = {"x-apisports-key": API_KEY, "x-rapidapi-host": "v3.football.api-sports.io"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=15) as response:
                if response.status != 200: return []
                data = await response.json()
                all_fixtures = data.get("response", [])
                filtered_matches = []
                for item in all_fixtures:
                    l_id = item["league"]["id"]
                    if l_id in LEAGUE_IDS:
                        status = item["fixture"]["status"]["short"]
                        score = f"{item['goals']['home']}:{item['goals']['away']}" if status in ["FT", "AET", "PEN"] else None
                        filtered_matches.append({
                            "match_id": item["fixture"]["id"], "league_id": l_id, "date": item["fixture"]["date"],
                            "home_team": item["teams"]["home"]["name"], "away_team": item["teams"]["away"]["name"], "result": score
                        })
                return filtered_matches
        except: return []

async def sync_today_matches():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    matches = await fetch_matches_from_api(today_str)
    if not matches: return
    async with aiosqlite.connect(DB_NAME) as db:
        for m in matches:
            await db.execute('INSERT OR IGNORE INTO matches VALUES (?, ?, ?, ?, ?, ?, 0, 0)', (m["match_id"], m["league_id"], m["date"], m["home_team"], m["away_team"], m["result"]))
        await db.commit()

async def check_live_results_and_notify():
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT match_id FROM matches WHERE result IS NULL") as cursor:
            if not await cursor.fetchall(): return
        
        api_matches = await fetch_matches_from_api(now.strftime("%Y-%m-%d"))
        for m in api_matches:
            if m["result"] is not None:
                async with db.execute("SELECT finished_notified, league_id FROM matches WHERE match_id = ?", (m["match_id"],)) as res_cur:
                    row = await res_cur.fetchone()
                    if row and row == 0:
                        league_id = row
                        await db.execute("UPDATE matches SET result = ?, finished_notified = 1 WHERE match_id = ?", (m["result"], m["match_id"]))
                        
                        async with db.execute("SELECT user_id, predicted_score FROM predictions WHERE match_id = ?", (m["match_id"],)) as pred_cursor:
                            async for user_id, predicted_score in pred_cursor:
                                earned_points = calculate_predicted_points(predicted_score, m["result"])
                                if earned_points > 0:
                                    await db.execute("UPDATE user_leagues SET points = points + ? WHERE user_id = ? AND league_id = ?", (earned_points, user_id, league_id))
                                    try: 
                                        alert_text = (
                                            f"📊 *РЕЗУЛЬТАТ МАТЧА:* {escape_md(LEAGUES_DICT[league_id])}\n"
                                            f"⚽ *{escape_md(m['home_team'])} {escape_md(m['result'])} {escape_md(m['away_team'])}*\n\n"
                                            f"Ваш прогноз: `{escape_md(predicted_score)}`\n"
                                            f"Начислено баллов: *+{earned_points}*"
                                        )
                                        await bot.send_message(chat_id=user_id, text=alert_text, parse_mode="MarkdownV2")
                                    except: pass
        await db.commit()

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    # Планировщик фоновых задач
    scheduler.add_job(sync_today_matches, 'cron', hour=6, minute=0)
    scheduler.add_job(sync_today_matches, 'cron', hour=14, minute=0)
    scheduler.add_job(check_live_results_and_notify, 'interval', minutes=30)
    scheduler.start()
    
    await sync_today_matches()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): pass
