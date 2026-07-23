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
WEB_APP_URL = "https://fcsmzzheka.github.io/LeagueOfSiberia/"
API_KEY = "feb9b5e694287da782e4d92deee40c20"
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
    encoded_data = aiohttp.helpers.urlencode({"data": json.dumps(init_data)})
    final_url = f"{WEB_APP_URL}?{encoded_data}"
    builder = InlineKeyboardBuilder()
    builder.button(text="ОТКРЫТЬ МАТЧ-ЦЕНТР 📱", web_app=types.WebAppInfo(url=final_url))
    text = (
        "<b>📊 КОНКУРС ПРОГНОЗОВ</b>\n\n"
        f"Учетная запись <b>@{username}</b> успешно активирована.\n\n"
        "Нажмите на кнопку ниже, чтобы открыть графический интерфейс матчей и таблиц:"
    )
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")

# --- ПРИЕМ ПРОГНОЗОВ ИЗ WEB APP ИНТЕРФЕЙСА ---
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

async def sync_three_days_matches():
    now = datetime.now(timezone.utc)
    for i in range(3):
        date_str = (now + timedelta(days=i)).strftime("%Y-%m-%d")
        matches = await fetch_matches_from_api(date_str)
        if matches:
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
    await sync_three_days_matches()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): pass
