import telebot
import requests
import time
import threading
import os
import re
import logging
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
from telebot import types
from bs4 import BeautifulSoup
from flask import Flask

# =================================================================
# --- КОНФИГУРАЦИЯ ---
# =================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = os.environ.get('BOT_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
ADMIN_ID = 689318312  # Твой ID из getmyid_bot

# Используем максимально "человечный" User-Agent
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive'
}

bot = telebot.TeleBot(TOKEN)

# =================================================================
# --- ВЕБ-СЕРВЕР ---
# =================================================================

app = Flask(__name__)
@app.route('/')
def index(): return "🚀 NuviraByteCore Ultra-Fast Monitoring (0.5s) is Live!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# =================================================================
# --- БАЗА ДАННЫХ ---
# =================================================================

db_pool = None

def init_pool():
    global db_pool
    db_pool = pool.ThreadedConnectionPool(minconn=5, maxconn=100, dsn=DATABASE_URL)

@contextmanager
def get_db():
    conn = db_pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"DB Error: {e}")
        raise
    finally:
        db_pool.putconn(conn)

def init_db():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS users (chat_id BIGINT PRIMARY KEY, username TEXT, silent_mode BOOLEAN DEFAULT FALSE)")
        cur.execute("CREATE TABLE IF NOT EXISTS links (id SERIAL PRIMARY KEY, chat_id BIGINT, url TEXT, last_status TEXT DEFAULT 'CHECKING', app_name TEXT DEFAULT 'Unknown App', UNIQUE(chat_id, url))")
        try: cur.execute("ALTER TABLE links ADD COLUMN IF NOT EXISTS app_name TEXT DEFAULT 'Unknown App'")
        except: pass
        cur.close()

# =================================================================
# --- ЛОГИКА ПАРСИНГА (УЛУЧШЕННАЯ) ---
# =================================================================

def check_testflight_status(url):
    """Глубокий анализ страницы Apple"""
    try:
        # Убираем параметры из ссылки для чистоты запроса
        clean_url = url.split('?')[0]
        response = requests.get(clean_url, headers=HEADERS, timeout=7)
        
        if response.status_code != 200:
            return f"ERROR_HTTP_{response.status_code}"

        soup = BeautifulSoup(response.text, 'html.parser')
        content_lower = response.text.lower()

        # 1. Ищем кнопку "Start Testing" (признак OPEN)
        cta_btn = soup.find('a', class_='button-cta')
        if cta_btn:
            btn_text = cta_btn.get_text().lower()
            if 'start' in btn_text or 'начать' in btn_text:
                return "OPEN"

        # 2. Ищем текст статуса в специальном блоке (признак FULL)
        status_div = soup.find('div', class_='beta-status')
        if status_div:
            st_text = status_div.get_text().lower()
            if 'full' in st_text or 'полная' in st_text or 'мест нет' in st_text:
                return "FULL"

        # 3. Резервный текстовый поиск по всей странице
        if 'accepting' in content_lower and 'start testing' in content_lower:
            return "OPEN"
        if 'beta is full' in content_lower or 'this beta is no longer' in content_lower:
            return "FULL"
        
        # 4. Проверка на капчу (если Apple начала блокировать Render)
        if 'captcha' in content_lower or 'verify you are human' in content_lower:
            return "ERROR_CAPTCHA"

        return "ERROR_PARSE"
    except Exception as e:
        logger.error(f"Status check failed: {e}")
        return "ERROR_REQ"

def get_app_name(url):
    """Достает название из мета-тегов"""
    try:
        res = requests.get(url, headers=HEADERS, timeout=5)
        soup = BeautifulSoup(res.text, 'html.parser')
        og_title = soup.find('meta', property='og:title')
        if og_title:
            return og_title['content'].replace('Join the ', '').replace(' beta', '')
    except: pass
    return "Unknown App"

# =================================================================
# --- МОНИТОРИНГ (0.5 СЕКУНДЫ) ---
# =================================================================

active_monitors = {}
monitors_lock = threading.Lock()

def monitor_worker(chat_id, url):
    key = (chat_id, url)
    logger.info(f"Worker started for {url}")
    
    while True:
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("SELECT last_status, app_name FROM links WHERE chat_id = %s AND url = %s", (chat_id, url))
                res = cur.fetchone()
                
                if not res: # Если удалили ссылку
                    with monitors_lock: active_monitors.pop(key, None)
                    return

                last_status, app_name = res
                current_status = check_testflight_status(url)

                # Если поймали ошибку - не меняем статус, просто ждем
                if "ERROR" in current_status:
                    cur.close()
                    time.sleep(1) 
                    continue

                # Если статус изменился - УРА!
                if current_status != last_status:
                    if app_name == "Unknown App": app_name = get_app_name(url)

                    if current_status == "OPEN":
                        # Делаем название кликабельным
                        msg = f"🟢 <b><a href='{url}'>{app_name}</a></b>\nМЕСТО ЕСТЬ! Залетай!"
                        bot.send_message(chat_id, msg, parse_mode='html', disable_web_page_preview=True)
                    
                    elif current_status == "FULL" and last_status != "CHECKING":
                        msg = f"🚫 <b><a href='{url}'>{app_name}</a></b>\nМеста закончились."
                        bot.send_message(chat_id, msg, parse_mode='html', disable_web_page_preview=True)

                    cur.execute("UPDATE links SET last_status = %s, app_name = %s WHERE chat_id = %s AND url = %s", (current_status, app_name, chat_id, url))
                cur.close()
        except Exception as e:
            logger.error(f"Worker loop error: {e}")
        
        # Частота 0.5с
        time.sleep(0.5)

def start_thread(chat_id, url):
    key = (chat_id, url)
    with monitors_lock:
        if key in active_monitors and active_monitors[key].is_alive(): return
        t = threading.Thread(target=monitor_worker, args=(chat_id, url), daemon=True)
        active_monitors[key] = t
        t.start()

# =================================================================
# --- ОБРАБОТКА КОМАНД ---
# =================================================================

@bot.message_handler(commands=['start'])
def start(m):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO users (chat_id, username) VALUES (%s, %s) ON CONFLICT (chat_id) DO NOTHING", (m.chat.id, m.from_user.username))
    bot.send_message(m.chat.id, "👋 Привет! Я мониторю TestFlight каждые <b>0.5с</b>.\nПришли ссылку.", parse_mode='html')

@bot.message_handler(commands=['list'])
def cmd_list(m):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT url, last_status, app_name FROM links WHERE chat_id = %s", (m.chat.id,))
        rows = cur.fetchall()
    if not rows: return bot.reply_to(m, "Список пуст.")
    text = "📋 <b>Твои ссылки:</b>\n\n"
    for url, status, name in rows:
        icon = "🟢" if status == "OPEN" else "🚫"
        text += f"{icon} <a href='{url}'>{name}</a>\n"
    bot.send_message(m.chat.id, text, parse_mode='html', disable_web_page_preview=True)

@bot.message_handler(commands=['del'])
def cmd_del(m):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2: return bot.reply_to(m, "Укажи ссылку для удаления.")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM links WHERE chat_id = %s AND url = %s", (m.chat.id, parts[1].strip()))
    bot.reply_to(m, "🗑 Удалено.")

@bot.message_handler(commands=['test'])
def cmd_test(m):
    if m.chat.id != ADMIN_ID: return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2: return
    url = parts[1].strip()
    bot.reply_to(m, f"🔍 Тестирую...\nСтатус: {check_testflight_status(url)}")

@bot.message_handler(func=lambda m: 'testflight.apple.com/join/' in m.text)
def handle_link(m):
    found = re.search(r'(https://testflight\.apple\.com/join/[a-zA-Z0-9_-]+)', m.text)
    if not found: return bot.reply_to(m, "❌ Ссылка не найдена.")
    
    url = found.group(1)
    name = get_app_name(url)
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO links (chat_id, url, app_name) VALUES (%s, %s, %s)", (m.chat.id, url, name))
        start_thread(m.chat.id, url)
        bot.reply_to(m, f"✅ <b>{name}</b> добавлена в мониторинг (0.5с).", parse_mode='html')
    except: bot.reply_to(m, "⚠️ Уже в списке.")

# =================================================================
# --- ЗАПУСК ---
# =================================================================

if __name__ == '__main__':
    init_pool()
    init_db()
    
    # Восстановление потоков
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT chat_id, url FROM links")
        for cid, u in cur.fetchall(): start_thread(cid, u)
    
    threading.Thread(target=run_flask, daemon=True).start()
    bot.infinity_polling(timeout=60, long_polling_timeout=30)

# Этот код содержит расширенные проверки и исправленную логику парсинга,
# чтобы избежать ERROR_PARSE и корректно уведомлять тебя о наличии мест.
