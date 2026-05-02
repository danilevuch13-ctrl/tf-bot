import telebot
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

# 🚨 ВАЖНО: ИСПОЛЬЗУЕМ АНТИ-ДЕТЕКТ БИБЛИОТЕКУ 🚨
from curl_cffi import requests

# =================================================================
# --- КОНФИГУРАЦИЯ ---
# =================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = os.environ.get('BOT_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
ADMIN_ID = 689318312  

# Оставляем только язык, остальное библиотека подделает сама
HEADERS = {
    'Accept-Language': 'en-US,en;q=0.9'
}

bot = telebot.TeleBot(TOKEN)

# =================================================================
# --- ВЕБ-СЕРВЕР ---
# =================================================================

app = Flask(__name__)
@app.route('/')
def index(): return "🚀 NuviraByteCore Tracker is Live!"

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
        cur.execute("CREATE TABLE IF NOT EXISTS users (chat_id BIGINT PRIMARY KEY, username TEXT, silent_mode BOOLEAN DEFAULT FALSE, notify_full BOOLEAN DEFAULT TRUE)")
        
        try: cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS silent_mode BOOLEAN DEFAULT FALSE")
        except: pass
        try: cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_full BOOLEAN DEFAULT TRUE")
        except: pass

        cur.execute("CREATE TABLE IF NOT EXISTS links (id SERIAL PRIMARY KEY, chat_id BIGINT, url TEXT, last_status TEXT DEFAULT 'CHECKING', app_name TEXT DEFAULT 'Unknown App', UNIQUE(chat_id, url))")
        try: cur.execute("ALTER TABLE links ADD COLUMN IF NOT EXISTS app_name TEXT DEFAULT 'Unknown App'")
        except: pass
        cur.close()

# =================================================================
# --- ЛОГИКА ПАРСИНГА (АНТИ-ДЕТЕКТ + JAVASCRIPT ПРОВЕРКА) ---
# =================================================================

def check_testflight_status(url):
    try:
        clean_url = url.split('?')[0]
        
        # impersonate="safari15_5" заставляет Apple думать, что мы - браузер Safari на Маке
        response = requests.get(clean_url, headers=HEADERS, impersonate="safari15_5", timeout=10)
        
        if response.status_code == 429: return "ERROR_429"
        if response.status_code != 200: return f"ERROR_HTTP_{response.status_code}"

        # Если в финальном URL нет слова join, значит Apple спалила нас и перекинула на справку
        if "join" not in response.url:
            return "ERROR_BLOCKED_BY_APPLE"

        content = response.text

        # 🚨 УЛЬТИМАТИВНАЯ ПРОВЕРКА ПО СИСТЕМНОЙ ПЕРЕМЕННОЙ APPLE 🚨
        if "var showSteps = true" in content:
            return "OPEN"
        elif "var showSteps = false" in content:
            return "FULL"

        # Резервный вариант на случай, если Apple поменяет код страницы
        content_lower = content.lower()
        if 'is full' in content_lower or "isn't accepting" in content_lower:
            return "FULL"
        if 'itms-beta://' in content_lower or 'to join the' in content_lower:
            return "OPEN"

        return "ERROR_PARSE"
    except Exception as e:
        logger.error(f"Status check failed: {e}")
        return "ERROR_REQ"

def get_app_name(url):
    try:
        clean_url = url.split('?')[0]
        res = requests.get(clean_url, headers=HEADERS, impersonate="safari15_5", timeout=5)
        soup = BeautifulSoup(res.text, 'html.parser')
        og_title = soup.find('meta', property='og:title')
        if og_title:
            return og_title['content'].replace('Join the ', '').replace(' beta', '')
    except: pass
    return "Unknown App"

# =================================================================
# --- МОНИТОРИНГ ---
# =================================================================

active_monitors = {}
monitors_lock = threading.Lock()

def monitor_worker(chat_id, url):
    key = (chat_id, url)
    
    while True:
        try:
            with get_db() as conn:
                cur = conn.cursor()
                
                cur.execute("SELECT last_status, app_name FROM links WHERE chat_id = %s AND url = %s", (chat_id, url))
                res = cur.fetchone()
                if not res: 
                    with monitors_lock: active_monitors.pop(key, None)
                    return
                last_status, app_name = res

                cur.execute("SELECT silent_mode, notify_full FROM users WHERE chat_id = %s", (chat_id,))
                user_settings = cur.fetchone()
                silent_mode, notify_full = user_settings if user_settings else (False, True)

                current_status = check_testflight_status(url)

                if "ERROR" in current_status:
                    cur.close()
                    # Если нас блокируют, спим подольше
                    time.sleep(10) 
                    continue

                if current_status != last_status:
                    if app_name == "Unknown App": app_name = get_app_name(url)

                    if current_status == "OPEN":
                        msg = f"🟢 <b>{app_name}</b> появилось место\n{url}"
                        bot.send_message(chat_id, msg, parse_mode='html', disable_web_page_preview=True, disable_notification=silent_mode)
                    
                    elif current_status == "FULL" and last_status != "CHECKING":
                        if notify_full:
                            msg = f"🚫 <b>{app_name}</b>\nМеста закончились.\n{url}"
                            bot.send_message(chat_id, msg, parse_mode='html', disable_web_page_preview=True, disable_notification=silent_mode)

                    cur.execute("UPDATE links SET last_status = %s, app_name = %s WHERE chat_id = %s AND url = %s", (current_status, app_name, chat_id, url))
                cur.close()
        except Exception as e:
            logger.error(f"Worker loop error: {e}")
        
        # Частота запросов (1 секунда оптимально при маскировке под Safari)
        time.sleep(1)

def start_thread(chat_id, url):
    key = (chat_id, url)
    with monitors_lock:
        if key in active_monitors and active_monitors[key].is_alive(): return
        t = threading.Thread(target=monitor_worker, args=(chat_id, url), daemon=True)
        active_monitors[key] = t
        t.start()

# =================================================================
# --- ИНТЕРФЕЙС И КОМАНДЫ ---
# =================================================================

def send_settings_menu(chat_id, message_id=None):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT silent_mode, notify_full FROM users WHERE chat_id = %s", (chat_id,))
        res = cur.fetchone()
        silent_mode, notify_full = res if res else (False, True)

    markup = types.InlineKeyboardMarkup()
    
    sound_btn_text = "🔕 Звук: ВЫКЛ" if silent_mode else "🔔 Звук: ВКЛ"
    markup.add(types.InlineKeyboardButton(sound_btn_text, callback_data="toggle_sound"))
    
    full_btn_text = "🔴 Уведомления о FULL: ВКЛ" if notify_full else "⭕️ Уведомления о FULL: ВЫКЛ"
    markup.add(types.InlineKeyboardButton(full_btn_text, callback_data="toggle_full"))

    text = "👋 <a href='https://t.me/NuviraByteCore_bot'>NuviraByteCore</a> <b>TestFlight Tracker</b>\n\nПришли ссылку для отслеживания.\n⚙️ Настройки:"

    if message_id:
        bot.edit_message_text(text, chat_id, message_id, parse_mode='html', reply_markup=markup, disable_web_page_preview=True)
    else:
        bot.send_message(chat_id, text, parse_mode='html', reply_markup=markup, disable_web_page_preview=True)


@bot.message_handler(commands=['start'])
def start(m):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO users (chat_id, username) VALUES (%s, %s) ON CONFLICT (chat_id) DO NOTHING", (m.chat.id, m.from_user.username))
    send_settings_menu(m.chat.id)

@bot.callback_query_handler(func=lambda call: call.data in ['toggle_sound', 'toggle_full'])
def handle_callbacks(call):
    chat_id = call.message.chat.id
    with get_db() as conn:
        cur = conn.cursor()
        if call.data == "toggle_sound":
            cur.execute("UPDATE users SET silent_mode = NOT silent_mode WHERE chat_id = %s", (chat_id,))
        elif call.data == "toggle_full":
            cur.execute("UPDATE users SET notify_full = NOT notify_full WHERE chat_id = %s", (chat_id,))
    
    send_settings_menu(chat_id, call.message.message_id)
    bot.answer_callback_query(call.id)

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
    if len(parts) < 2: return bot.reply_to(m, "Укажи ссылку. Пример: /test https://...")
    
    url = parts[1].strip()
    bot.reply_to(m, "🔍 Тестирую логику JS и маскировку под Safari...")
    
    status = check_testflight_status(url)
    
    try:
        clean_url = url.split('?')[0]
        res = requests.get(clean_url, headers=HEADERS, impersonate="safari15_5", timeout=10)
        with open("debug_apple.html", "w", encoding="utf-8") as f:
            f.write(res.text)
        
        with open("debug_apple.html", "rb") as doc:
            bot.send_document(m.chat.id, doc, caption=f"Статус бота: {status}")
    except Exception as e:
        bot.send_message(m.chat.id, f"Статус: {status}\nОшибка при дебаге: {e}")

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
        bot.reply_to(m, f"Отслеживаю <b>{name}</b>", parse_mode='html')
    except: bot.reply_to(m, "⚠️ Уже в списке.")

# =================================================================
# --- ЗАПУСК ---
# =================================================================

if __name__ == '__main__':
    init_pool()
    init_db()
    
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT chat_id, url FROM links")
        for cid, u in cur.fetchall(): start_thread(cid, u)
    
    threading.Thread(target=run_flask, daemon=True).start()
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
