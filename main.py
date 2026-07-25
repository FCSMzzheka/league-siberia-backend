import asyncio
import logging
import json
import re
import urllib.parse
import os
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

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
    
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
            for l_id in LEAGUE_IDS:
                await db.execute("INSERT OR IGNORE INTO user_leagues (user_id, league_id, points) VALUES (?, ?, 0)", (user_id, l_id))
            await db.commit()
    except Exception as e:
        logging.error(f"Регистрация пользователя в БД: {e}")
        
    builder = InlineKeyboardBuilder()
    builder.button(text="ОТКРЫТЬ МАТЧ-ЦЕНТР 📱", web_app=types.WebAppInfo(url=WEB_APP_URL))
    
    text = (
        "<b>📊 АНАЛИТИЧЕСКАЯ СИСТЕМА ПРОГНОЗИРОВАНИЯ</b>\n\n"
        f"Учетная запись <b>@{username}</b> успешно активирована.\n\n"
        "Нажмите на кнопку ниже, чтобы открыть графический матч-центр турнира:"
    )
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.message(F.content_type == types.ContentType.WEB_APP_DATA)
async def process_web_app_data(message: types.Message):
    try:
        data = json.loads(message.web_app_data.data)
        if data.get("action") == "predict":
            match_id = int(data.get("match_id"))
            score = data.get("score")
            home_team = data.get("home")
            away_team = data.get("away")
            user_id = message.from_user.id
            
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute('INSERT OR IGNORE INTO matches VALUES (?, 235, "2026-07-25", ?, ?, NULL, 0, 0)', (match_id, home_team, away_team))
                await db.execute('INSERT OR REPLACE INTO predictions VALUES (?, ?, ?)', (user_id, match_id, score))
                await db.commit()
                
            await message.answer(
                f"✅ <b>Прогноз внесен в реестр</b>\n\n"
                f"⚔️ {escape_md(home_team)} — {escape_md(away_team)}\n"
                f"🔮 Ставка: <code>{score}</code>", 
                parse_mode="HTML"
            )
    except Exception as e:
        logging.error(f"Ошибка Web App: {e}")

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    logging.info("Бот успешно запущен в сеть!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): pass
