import telebot
import requests
import time
import threading
import os
import re
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
from telebot import types
from bs4 import BeautifulSoup
from flask import Flask

# --- КОНФИГУРАЦИЯ ---
TOKEN = os.environ['BOT_TOKEN']
DATABASE_URL = os.environ['DATABASE_URL']
ADMIN_ID = 689318312  # Твой Telegram ID вписан сюда!

bot = telebot.TeleBot(TOKEN)

# --- Flask заглушка для Render ---
app = Flask(__name__)

@app.route('/')
def home():
   return "✅ Бот работает 24/7!"

def run_web():
   port = int(os.environ.get("PORT", 10000))
   app.run(host="0.0.0.0", port=port)

HEADERS = {
   'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1',
   'Accept-Language': 'en-US,en;q=0.9',
   'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
}

app_name_cache = {}
active_monitors = {}
active_monitors_lock = threading.Lock()
db_pool = None

def init_pool():
   global db_pool
   db_pool = pool.ThreadedConnectionPool(
       minconn=2,
       maxconn=40,
       dsn=DATABASE_URL
   )
   print("[pool] Пул соединений создан")

@contextmanager
def get_db():
   conn = db_pool.getconn()
   try:
       yield conn
       conn.commit()
   except Exception:
       conn.rollback()
       raise
   finally:
       db_pool.putconn(conn)

def init_db():
   with get_db() as conn:
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
           last_status TEXT DEFAULT 'CHECKING',
           app_name TEXT DEFAULT 'Unknown App',
           UNIQUE(chat_id, url)
       )''')
       cur.close()

def check_testflight_slot(url):
   clean_url = url.split('?')[0] # Убрали ?t=... чтобы не бесить защиту Apple
   try:
       response = requests.get(clean_url, headers=HEADERS, timeout=10)
       if response.status_code == 200:
           soup = BeautifulSoup(response.text, 'html.parser')

           start_btn = soup.find('a', class_='button-cta')
           if start_btn:
               return "OPEN"

           beta_status = soup.find('span', class_='beta-status')
           if beta_status and 'full' in beta_status.get_text(strip=True).lower():
               return "FULL"

           text = response.text.lower()
           if 'start testing' in text or '"status":"accepting"' in text:
               return "OPEN"
           if 'beta is full' in text or 'not accepting' in text or '"status":"full"' in text or '"status":"closed"' in text:
               return "FULL"

           return "ERROR_PARSE"
       else:
           return f"ERROR_{response.status_code}"
   except Exception as e:
       print(f"[check_testflight_slot] Ошибка для {url}: {e}")
       return f"ERROR_REQ"

def get_link_metadata(url):
   if url in app_name_cache:
       return app_name_cache[url]
   try:
       response = requests.get(url, headers=HEADERS, timeout=5)
       if response.status_code == 200:
           soup = BeautifulSoup(response.text, 'html.parser')
           title = soup.find('meta', property='og:title')
           app_name = title['content'].replace('Join the ', '').replace(' beta', '') if title else "Unknown App"
           app_name_cache[url] = app_name
           return app_name
   except Exception as e:
       print(f"[get_link_metadata] Ошибка для {url}: {e}")
   return "Unknown App"

def monitor_link(chat_id, url):
   key = (chat_id, url)
   print(f"[monitor] Запущен поток для chat_id={chat_id}, url={url}")

   while True:
       try:
           with get_db() as conn:
               cur = conn.cursor()
               cur.execute("SELECT 1 FROM links WHERE chat_id = %s AND url = %s", (chat_id, url))
               exists = cur.fetchone()
               cur.close()
       except Exception as e:
           print(f"[monitor] Ошибка проверки существования {key}: {e}")
           time.sleep(3)
           continue

       if not exists:
           print(f"[monitor] Ссылка удалена, останавливаю поток для {key}")
           with active_monitors_lock:
               active_monitors.pop(key, None)
           return

       current_status = check_testflight_slot(url)

       if "ERROR" in current_status:
           print(f"[monitor] Ошибка {current_status} для {key}")
           time.sleep(3)
           continue

       try:
           with get_db() as conn:
               cur = conn.cursor()
               cur.execute("""
                   SELECT l.last_status, u.silent_mode, u.notify_full
                   FROM links l
                   JOIN users u ON l.chat_id = u.chat_id
                   WHERE l.chat_id = %s AND l.url = %s
               """, (chat_id, url))
               row = cur.fetchone()
               cur.close()
       except Exception as e:
           print(f"[monitor] Ошибка чтения статуса {key}: {e}")
           time.sleep(3)
           continue

       if not row:
           time.sleep(3)
           continue

       last_status, silent, notify_full = row

       if current_status != last_status:
           try:
               app_name = get_link_metadata(url)

               if current_status == "OPEN":
                   text_msg = f"🟢 <b>{app_name}</b> beta is Available now\n\n{url}"
                   bot.send_message(chat_id, text_msg, parse_mode='html', disable_notification=silent, disable_web_page_preview=True)

               elif current_status == "FULL" and notify_full and last_status != "CHECKING":
                   text_msg = f"🚫 <b>{app_name}</b> beta is Unavailable now"
                   bot.send_message(chat_id, text_msg, parse_mode='html', disable_notification=silent, disable_web_page_preview=True)

           except Exception as e:
               print(f"[monitor] Ошибка отправки сообщения {key}: {e}")

           try:
               with get_db() as conn:
                   cur = conn.cursor()
                   cur.execute(
                       "UPDATE links SET last_status = %s WHERE chat_id = %s AND url = %s",
                       (current_status, chat_id, url)
                   )
                   cur.close()
           except Exception as e:
               print(f"[monitor] Ошибка обновления статуса {key}: {e}")

       time.sleep(1)
       time.sleep(3)

def start_monitor(chat_id, url):
   key = (chat_id, url)
   with active_monitors_lock:
       if key in active_monitors and active_monitors[key].is_alive():
           return
       t = threading.Thread(target=monitor_link, args=(chat_id, url), daemon=True)
       active_monitors[key] = t
       t.start()

def restore_monitors():
   try:
       with get_db() as conn:
           cur = conn.cursor()
           cur.execute("SELECT chat_id, url FROM links")
           rows = cur.fetchall()
           cur.close()
       for chat_id, url in rows:
           start_monitor(chat_id, url)
       print(f"[restore] Восстановлено {len(rows)} потоков мониторинга")
   except Exception as e:
       print(f"[restore] Ошибка восстановления мониторинга: {e}")

def get_settings_keyboard(chat_id):
   try:
       with get_db() as conn:
           cur = conn.cursor()
           cur.execute("SELECT silent_mode, notify_full FROM users WHERE chat_id = %s", (chat_id,))
           res = cur.fetchone()
           cur.close()
       if not res:
           return None
       silent, full = res
       markup = types.InlineKeyboardMarkup()
       markup.add(types.InlineKeyboardButton(f"🔔 Звук: {'ВЫКЛ' if silent else 'ВКЛ'}", callback_data="toggle_silent"))
       markup.add(types.InlineKeyboardButton(f"🔴 Уведомления о FULL: {'ВКЛ' if full else 'ВЫКЛ'}", callback_data="toggle_full"))
       return markup
   except Exception as e:
       print(f"[get_settings_keyboard] Ошибка: {e}")
       return None

@bot.message_handler(commands=['start'])
def send_welcome(message):
   uid = message.chat.id
   name = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
   try:
       with get_db() as conn:
           cur = conn.cursor()
           cur.execute(
               "INSERT INTO users (chat_id, username) VALUES (%s, %s) ON CONFLICT (chat_id) DO UPDATE SET username = %s",
               (uid, name, name)
           )
           cur.close()
   except Exception as e:
       print(f"[start] Ошибка БД: {e}")

   text = (f"👋 <b><a href='https://t.me/NuviraByteCore'>NuviraByteCore</a> TestFlight Tracker</b>\n\n"
           f"Просто отправь мне ссылку TestFlight, и я начну её отслеживать.\n\n"
           f"<b>Команды:</b>\n"
           f"/list — посмотреть свои ссылки\n"
           f"/del <i>ссылка</i> — удалить ссылку\n\n"
           f"⚙️ <b>Настройки уведомлений:</b>")
   bot.send_message(uid, text, parse_mode='html', reply_markup=get_settings_keyboard(uid), disable_web_page_preview=True)

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
   try:
       with get_db() as conn:
           cur = conn.cursor()
           if call.data == "toggle_silent":
               cur.execute("UPDATE users SET silent_mode = NOT silent_mode WHERE chat_id = %s", (call.message.chat.id,))
           elif call.data == "toggle_full":
               cur.execute("UPDATE users SET notify_full = NOT notify_full WHERE chat_id = %s", (call.message.chat.id,))
           cur.close()
       bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=get_settings_keyboard(call.message.chat.id))
       bot.answer_callback_query(call.id, "Обновлено")
   except Exception as e:
       print(f"[callback_query] Ошибка: {e}")

@bot.message_handler(commands=['list'])
def list_links(message):
   try:
       with get_db() as conn:
           cur = conn.cursor()
           cur.execute("SELECT url, last_status, app_name FROM links WHERE chat_id = %s", (message.chat.id,))
           rows = cur.fetchall()
           cur.close()
   except Exception as e:
       print(f"[list] Ошибка БД: {e}")
       bot.reply_to(message, "❌ Ошибка получения списка.")
       return

   if not rows:
       bot.reply_to(message, "Твой список пуст.")
       return

   text = "📋 <b>Твои отслеживаемые ссылки:</b>\n\n"
   for url, status, app_name in rows:
       if status == "OPEN": icon = "🟢"
       elif status == "FULL": icon = "🚫"
       else: icon = "🟠"
       display_name = app_name if app_name and app_name != "Unknown App" else url
       text += f"{icon} <b>{display_name}</b>\n{url}\n\n"

   bot.reply_to(message, text, parse_mode='html', disable_web_page_preview=True)

@bot.message_handler(commands=['del'])
@bot.message_handler(func=lambda m: m.text and m.text.lower().startswith('del') and m.reply_to_message)
def delete_link(message):
   target = None
   if message.reply_to_message and message.reply_to_message.text:
       match = re.search(r'(https://testflight\.apple\.com/join/[a-zA-Z0-9_-]+)', message.reply_to_message.text)
       if match:
           target = match.group(1)
   if not target and message.text.startswith('/del'):
       parts = message.text.split(maxsplit=1)
       if len(parts) > 1:
           target = parts[1].strip()
   if target:
       try:
           with get_db() as conn:
               cur = conn.cursor()
               cur.execute("DELETE FROM links WHERE chat_id = %s AND url = %s", (message.chat.id, target))
               cur.close()
           app_name_cache.pop(target, None)
           bot.reply_to(message, "🗑 Ссылка удалена.")
       except Exception as e:
           print(f"[del] Ошибка БД: {e}")
           bot.reply_to(message, "❌ Ошибка удаления.")
   else:
       bot.reply_to(message, "❌ Ссылка не найдена. Напиши: /del ссылка")

@bot.message_handler(commands=['danyaxap'])
def admin_stats(message):
   if message.chat.id != ADMIN_ID:
       return
   try:
       with get_db() as conn:
           cur = conn.cursor()
           cur.execute("SELECT username FROM users")
           users = cur.fetchall()
           cur.execute("SELECT COUNT(*) FROM links")
           links_count = cur.fetchone()[0]
           cur.close()
       with active_monitors_lock:
           threads_count = sum(1 for t in active_monitors.values() if t.is_alive())
       bot.reply_to(message,
           f"📊 Юзеров: {len(users)}\n"
           f"🔗 Ссылок: {links_count}\n"
           f"🧵 Активных потоков: {threads_count}\n\n"
           + "\n".join([u[0] for u in users])
       )
   except Exception as e:
       print(f"[admin_stats] Ошибка: {e}")
       bot.reply_to(message, "❌ Ошибка получения статистики.")

@bot.message_handler(commands=['test'])
def test_apple_connection(message):
   if message.chat.id != ADMIN_ID:
       return
   parts = message.text.split(maxsplit=1)
   if len(parts) < 2:
       bot.reply_to(message, "Отправь так: /test ссылка_testflight")
       return

   url = parts[1].strip()
   bot.reply_to(message, "⏳ Стучусь к Apple...")

   parsed_status = check_testflight_slot(url)

   if parsed_status in ["OPEN", "FULL", "ERROR_PARSE"]:
       try:
           with get_db() as conn:
               cur = conn.cursor()
               cur.execute(
                   "UPDATE links SET last_status = %s WHERE chat_id = %s AND url = %s",
                   (parsed_status, message.chat.id, url)
               )
               cur.close()
           bot.reply_to(message, f"✅ Доступ есть.\n🤖 Статус: {parsed_status}\n💾 База обновлена.", disable_web_page_preview=True)
       except Exception as e:
           bot.reply_to(message, f"✅ Доступ есть.\n🤖 Статус: {parsed_status}\n⚠️ Ошибка БД: {e}", disable_web_page_preview=True)
   else:
       bot.reply_to(message, f"⚠️ Ответ сервера: {parsed_status}", disable_web_page_preview=True)


@bot.message_handler(func=lambda m: m.text and 'testflight.apple.com/join/' in m.text)
def add_link(message):
   match = re.search(r'(https://testflight\.apple\.com/join/[a-zA-Z0-9_-]+)', message.text)
   if match:
       url = match.group(1)
       try:
           with get_db() as conn:
               cur = conn.cursor()
               cur.execute(
                   "INSERT INTO links (chat_id, url, last_status) VALUES (%s, %s, 'CHECKING')",
                   (message.chat.id, url)
               )
               cur.close()
           app_name = get_link_metadata(url)
           with get_db() as conn:
               cur = conn.cursor()
               cur.execute(
                   "UPDATE links SET app_name = %s WHERE chat_id = %s AND url = %s",
                   (app_name, message.chat.id, url)
               )
               cur.close()
           start_monitor(message.chat.id, url)
           bot.reply_to(message, f"✅ Добавлено: <b>{app_name}</b>", parse_mode='html')
       except psycopg2.errors.UniqueViolation:
           bot.reply_to(message, "⚠️ Эта ссылка уже есть в твоём списке.")
       except Exception as e:
           print(f"[add_link] Ошибка: {e}")
           bot.reply_to(message, "❌ Ошибка при добавлении ссылки.")

if __name__ == '__main__':
   init_pool()
   init_db()
   restore_monitors()
   threading.Thread(target=run_web, daemon=True).start()
   bot.infinity_polling()
