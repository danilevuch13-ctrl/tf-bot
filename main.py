import telebot
import requests
import time
import threading
import os
import re
import psycopg2
from flask import Flask

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
    # Таблица пользователей
    cur.execute('''CREATE TABLE IF NOT EXISTS users (
        chat_id BIGINT PRIMARY KEY,
        username TEXT
    )''')
    # Таблица ссылок
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
                
                cur.execute("SELECT chat_id, last_status FROM links WHERE url = %s", (url,))
                records = cur.fetchall()
                
                old_status = records[0][1] if records else "FULL"

                if current_status == "OPEN" and old_status != "OPEN":
                    for row in records:
                        bot.send_message(row[0], f"🟢 Место появилось! Быстрее забирай:\n{url}")
                elif current_status == "FULL" and old_status == "OPEN":
                    for row in records:
                        bot.send_message(row[0], f"🔴 Места закончились для:\n{url}")
                
                cur.execute("UPDATE links SET last_status = %s WHERE url = %s", (current_status, url))
            
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print(f"Monitor error: {e}")
        time.sleep(1)

# --- КОМАНДЫ ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    uid = message.chat.id
    name = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO users (chat_id, username) VALUES (%s, %s) ON CONFLICT (chat_id) DO UPDATE SET username = %s", (uid, name, name))
    conn.commit()
    cur.close()
    conn.close()

    text = ("👋 Привет участникам <a href='https://t.me/NuviraByteCore_bot'>NuviraByteCore</a>!\n\n"
            "Пришли ссылку для отслеживания.\n"
            "📋 /list — твои ссылки\n"
            "🗑 /del — удаление (или 'del' в ответ на сообщение)")
    bot.reply_to(message, text, parse_mode='html', disable_web_page_preview=True)

@bot.message_handler(commands=['danyaxap'])
def admin_stats(message):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT username FROM users")
    users = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM links")
    links_count = cur.fetchone()[0]
    
    text = f"📊 Пользователей: {len(users)}\n🔗 Ссылок в работе: {links_count}\n\n"
    text += "\n".join([u[0] for u in users])
    bot.reply_to(message, text)
    cur.close()
    conn.close()

@bot.message_handler(commands=['list'])
def list_links(message):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT url FROM links WHERE chat_id = %s", (message.chat.id,))
    rows = cur.fetchall()
    bot.reply_to(message, "📋 Твои ссылки:\n\n" + "\n".join([r[0] for r in rows]) if rows else "Список пуст.")
    cur.close()
    conn.close()

@bot.message_handler(commands=['del'])
@bot.message_handler(func=lambda m: m.text and m.text.lower() == 'del' and m.reply_to_message)
def delete_link(message):
    target = None
    if message.reply_to_message and message.reply_to_message.text:
        match = re.search(r'(https://testflight\.apple\.com/join/[a-zA-Z0-9_-]+)', message.reply_to_message.text)
        if match: target = match.group(1)
    if not target and message.text.startswith('/del'):
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1: target = parts[1].strip()

    if target:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM links WHERE chat_id = %s AND url = %s", (message.chat.id, target))
        conn.commit()
        bot.reply_to(message, "🗑 Удалено.")
        cur.close()
        conn.close()
    else:
        bot.reply_to(message, "❌ Ссылка не найдена.")

@bot.message_handler(func=lambda m: 'testflight.apple.com/join/' in m.text)
def add_link(message):
    match = re.search(r'(https://testflight\.apple\.com/join/[a-zA-Z0-9_-]+)', message.text)
    if match:
        url = match.group(1)
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO links (chat_id, url) VALUES (%s, %s)", (message.chat.id, url))
            conn.commit()
            bot.reply_to(message, "✅ Добавлено в базу. Теперь я никогда её не забуду!")
        except:
            bot.reply_to(message, "⚠️ Эта ссылка уже отслеживается.")
        cur.close()
        conn.close()

# --- ЗАПУСК ---
app = Flask(__name__)
@app.route('/')
def home(): return "OK", 200

if __name__ == '__main__':
    init_db()
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000))), daemon=True).start()
    threading.Thread(target=monitor_logic, daemon=True).start()
    bot.infinity_polling()
