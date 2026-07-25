import asyncio
import logging
import json
import re
import os
import urllib.parse
from datetime import datetime
import aiosqlite
import aiohttp
from bs4 import BeautifulSoup
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

SPORT_RU_URLS = {
    235: "https://www.sport.ru/football/rfpl/results/",
    39: "https://www.sport.ru/football/premer-liga_angliya/results/",
    140: "https://www.sport.ru/football/primera_ispaniya/results/",
    135: "https://www.sport.ru/football/seriya_a_italiya/results/",
    78: "https://www.sport.ru/football/bundesliga_germaniya/results/",
    2: "https://www.sport.ru/football/liga_chempionov_uefa/results/"
}

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
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
        for l_id in LEAGUE_IDS:
            await db.execute("INSERT OR IGNORE INTO user_leagues (user_id, league_id, points) VALUES (?, ?, 0)", (user_id, l_id))
        await db.commit()
        
        query_matches = "SELECT match_id, league_id, date, home_team, away_team FROM matches WHERE result IS NULL"
        async with db.execute(query_matches) as cursor:
            matches_rows = await cursor.fetchall()

    matches_list = []
    for r in matches_rows:
        matches_list.append({
            "id": r[0], 
            "league": LEAGUES_DICT.get(r[1], "Турнир"), 
            "date": r[2], 
            "home": r[3], 
            "away": r[4]
        })

    init_data = {"matches": matches_list}
    json_string = json.dumps(init_data, separators=(',', ':'))
    encoded_data = urllib.parse.quote(json_string)
    final_url = f"{WEB_APP_URL}?data={encoded_data}"
        
    builder = InlineKeyboardBuilder()
    builder.button(text="ОТКРЫТЬ МАТЧ-ЦЕНТР 📱", web_app=types.WebAppInfo(url=final_url))
    
    text = (
        "<b>📊 АНАЛИТИЧЕСКАЯ СИСТЕМА ПРОГНОЗИРОВАНИЯ</b>\n\n"
        f"Учетная запись <b>@{username}</b> успешно активирована.\n\n"
        "Нажмите на кнопку ниже, чтобы открыть графический матч-центр:"
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
    except Exception as e:
        logging.error(f"Ошибка Web App: {e}")

async def sync_sport_ru():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    now = datetime.now()
    
    async with aiohttp.ClientSession(headers=headers) as session:
        for league_id, url in SPORT_RU_URLS.items():
            try:
                # ЭТАП 1: Получаем основную страницу и ищем ссылку на iframe
                async with session.get(url, timeout=15) as response:
                    if response.status != 200: continue
                    html = await response.text()
                    soup = BeautifulSoup(html, "html.parser")
                    
                    iframe = soup.find("iframe", id="myFrame")
                    if not iframe:
                        logging.error(f"Не найден iframe на странице лиги {league_id}")
                        continue
                        
                    iframe_src = iframe.get("src")
                    iframe_url = f"https://www.sport.ru{iframe_src}"
                
                # ЭТАП 2: Скачиваем таблицу из iframe и парсим её
                async with session.get(iframe_url, timeout=15) as iframe_response:
                    if iframe_response.status != 200: continue
                    iframe_html = await iframe_response.text()
                    iframe_soup = BeautifulSoup(iframe_html, "html.parser")
                    
                    # Ищем таблицу с классом matches
                    matches_table = iframe_soup.find("table", class_="matches")
                    if not matches_table:
                        continue
                        
                    rows = matches_table.find_all("tr")
                    added_count = 0
                    
                    async with aiosqlite.connect(DB_NAME) as db:
                        for row in rows:
                            cols = row.find_all("td")
                            
                            # Если колонок мало, это строка-заголовок, пропускаем
                            if len(cols) < 5:
                                continue
                                
                            # Ищем команды по специальному классу ссылок
                            teams = row.find_all("a", class_="team-link")
                            if len(teams) >= 2:
                                team1 = teams[0].get_text(strip=True)
                                team2 = teams[1].get_text(strip=True)
                                
                                # Достаем текст из первой колонки и ищем дату
                                date_text = cols[0].get_text(separator=" ", strip=True)
                                match_date = re.search(r'(\d{2}\.\d{2}\.\d{4})\s*(\d{2}:\d{2})', date_text)
                                
                                if match_date:
                                    date_str = f"{match_date.group(1)} {match_date.group(2)}"
                                    
                                    try:
                                        match_dt = datetime.strptime(date_str, "%d.%m.%Y %H:%M")
                                        
                                        # Сохраняем только будущие матчи
                                        if match_dt > now:
                                            match_id = abs(hash(team1 + team2 + date_str)) % 1000000
                                            
                                            await db.execute(
                                                'INSERT OR IGNORE INTO matches (match_id, league_id, date, home_team, away_team, result) VALUES (?, ?, ?, ?, ?, NULL)',
                                                (match_id, league_id, date_str, team1, team2)
                                            )
                                            added_count += 1
                                    except Exception as parse_err:
                                        logging.error(f"Ошибка парсинга даты: {parse_err}")
                        await db.commit()
                
                logging.info(f"Лига {league_id} успешно спарсена. Найдено будущих матчей: {added_count}")
                
            except Exception as e:
                logging.error(f"Глобальная ошибка парсинга лиги {league_id}: {e}")

async def scheduler_loop():
    while True:
        await sync_sport_ru()
        await asyncio.sleep(14400)

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    asyncio.create_task(scheduler_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
