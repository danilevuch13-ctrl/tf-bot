import telebot
import requests
import time
import threading
import os
import re
import psycopg2
from flask import Flask
from telebot import types

# --- КОНФИГУРАЦИЯ ---
TOKEN = '8626634626:AAHLC6m4k9sFvHGvKzxJrVkqcAqqH6hhNoA'
DATABASE_URL = 'postgresql://postgres:29118041393Aa@db.hdjvfiolfbghpvesuumm.supabase.co:5432/postgres'

bot = telebot.TeleBot(TOKEN)
lock = threading.Lock()

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1'
}

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS users (
        chat_id BIGINT PRIMARY KEY,
        username TEXT,
        silent_mode BOOLEAN DEFAULT FALSE,
        notify_full BOOLEAN DEFAULT TRUE
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS links (
        id SERIAL PRIMARY KEY,
        chat_id BIGINT,
        url TEXT,
        last_status TEXT DEFAULT 'FULL',
        UNIQUE(chat_id, url)
    )''')
    conn.commit()
    cur.close()
    conn.close()

# --- ЛОГИКА ПРОВЕРКИ ---
def check_testflight_slot(url):
    clean_url = url.split('?')[0]
    no_cache_url = f"{clean_url}?t={int(time.time() * 1000)}"
    try:
        response = requests.get(no_cache_url, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            if "Join the Beta" in response.text or '"status":"ACCEPTING"' in response.text:
                return "OPEN"
            return "FULL"
    except: pass
    return "ERROR"

def monitor_logic():
    while True:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT url FROM links")
            urls = [row[0] for row in cur.fetchall()]
            for url in urls:
                current_status = check_testflight_slot(url)
                cur.execute("SELECT l.chat_id, l.last_status, u.silent_mode, u.notify_full FROM links l JOIN users u ON l.chat_id = u.chat_id WHERE l.url = %s", (url,))
                records = cur.fetchall()
                if not records: continue
                old_status = records[0][1]
                if current_status != old_status:
                    for chat_id, _, silent, notify_full in records:
                        try:
                            if current_status == "OPEN":
                                bot.send_message(chat_id, f"🟢 Место появилось!\n{url}", disable_notification=silent)
                            elif current_status == "FULL" and notify_full:
                                bot.send_message(chat_id, f"🔴 Места закончились для:\n{url}", disable_notification=silent)
                        except: pass
                    cur.execute("UPDATE links SET last_status = %s WHERE url = %s", (current_status, url))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e: print(f"Monitor error: {e}")
        time.sleep(1)

# --- КЛАВИАТУРЫ ---
def get_settings_keyboard(chat_id):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT silent_mode, notify_full FROM users WHERE chat_id = %s", (chat_id,))
    res = cur.fetchone(); cur.close(); conn.close()
    if not res: return None
    silent, full = res
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton(f"🔔 Звук: {'ВЫКЛ' if silent else 'ВКЛ'}", callback_data="toggle_silent"))
    markup.add(types.InlineKeyboardButton(f"🔴 Уведомления о FULL: {'ВКЛ' if full else 'ВЫКЛ'}", callback_data="toggle_full"))
    return markup

# --- КОМАНДЫ ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    uid = message.chat.id
    name = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO users (chat_id, username) VALUES (%s, %s) ON CONFLICT (chat_id) DO UPDATE SET username = %s", (uid, name, name))
    conn.commit(); cur.close(); conn.close()
    text = ("👋 Рад видеть тебя в <a href='https://t.me/NuviraByteCore_bot'>NuviraByteCore</a>!\n\nПришли ссылку TestFlight для начала.\n\n⚙️ Настройки:")
    bot.send_message(uid, text, parse_mode='html', reply_markup=get_settings_keyboard(uid), disable_web_page_preview=True)

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    conn = get_db_connection(); cur = conn.cursor()
    if call.data == "toggle_silent":
        cur.execute("UPDATE users SET silent_mode = NOT silent_mode WHERE chat_id = %s", (call.message.chat.id,))
    elif call.data == "toggle_full":
        cur.execute("UPDATE users SET notify_full = NOT notify_full WHERE chat_id = %s", (call.message.chat.id,))
    conn.commit(); cur.close(); conn.close()
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=get_settings_keyboard(call.message.chat.id))
    bot.answer_callback_query(call.id, "Настройки обновлены")

@bot.message_handler(commands=['list'])
def list_links(message):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT url FROM links WHERE chat_id = %s", (message.chat.id,))
    rows = cur.fetchall(); cur.close(); conn.close()
    bot.reply_to(message, "📋 Твои ссылки:\n\n" + "\n".join([r[0] for r in rows]) if rows else "Список пуст.")

@bot.message_handler(commands=['del'])
@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith('del') and m.reply_to_message)
def delete_link(message):
    target = None
    if message.reply_to_message and message.reply_to_message.text:
        match = re.search(r'(https://testflight\.apple\.com/join/[a-zA-Z0-9_-]+)', message.reply_to_message.text)
        if match: target = match.group(1)
    if not target and message.text.startswith('/del'):
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1: target = parts[1].strip()
    if target:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("DELETE FROM links WHERE chat_id = %s AND url = %s", (message.chat.id, target))
        conn.commit(); cur.close(); conn.close()
        bot.reply_to(message, "🗑 Удалено.")
    else:
        bot.reply_to(message, "❌ Ссылка не найдена.")

@bot.message_handler(commands=['danyaxap'])
def admin_stats(message):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT username FROM users"); users = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM links"); links_count = cur.fetchone()[0]
    bot.reply_to(message, f"📊 Юзеров: {len(users)}\n🔗 Ссылок: {links_count}\n\n" + "\n".join([u[0] for u in users]))
    cur.close(); conn.close()

@bot.message_handler(func=lambda m: m.text and 'testflight.apple.com/join/' in m.text)
def add_link(message):
    match = re.search(r'(https://testflight\.apple\.com/join/[a-zA-Z0-9_-]+)', message.text)
    if match:
        url = match.group(1)
        conn = get_db_connection(); cur = conn.cursor()
        try:
            cur.execute("INSERT INTO links (chat_id, url) VALUES (%s, %s)", (message.chat.id, url))
            conn.commit(); bot.reply_to(message, "✅ Добавлено.")
        except: bot.reply_to(message, "⚠️ Уже в списке.")
        cur.close(); conn.close()

app = Flask(__name__)
@app.route('/')
def home(): return "OK", 200

if __name__ == '__main__':
    init_db()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    threading.Thread(target=monitor_logic, daemon=True).start()
    bot.infinity_polling()
