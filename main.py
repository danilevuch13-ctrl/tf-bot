import telebot
import requests
import time
import threading
import os
import re
from flask import Flask

# --- ДАННЫЕ БОТА ---
TOKEN = '8626634626:AAHLC6m4k9sFvHGvKzxJrVkqcAqqH6hhNoA'
bot = telebot.TeleBot(TOKEN)

# Словари для хранения данных в памяти
watchers = {}      # {link: set(chat_id1, chat_id2, ...)}
last_state = {}    # {link: "OPEN" / "FULL" / "ERROR"}
known_users = {}   # {chat_id: "Имя/Юзернейм"}
lock = threading.Lock()

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1',
    'Accept-Language': 'en-US,en;q=0.9'
}

# --- Flask для Render (чтобы сервер не засыпал) ---
app = Flask(__name__)

@app.route('/')
def index():
    return 'Bot is running', 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- Логика проверки TestFlight ---
def check_testflight_slot(url):
    clean_url = url.split('?')[0]
    # Добавляем временную метку, чтобы Apple не выдавала кэшированную страницу
    no_cache_url = f"{clean_url}?t={int(time.time() * 1000)}"

    try:
        response = requests.get(no_cache_url, headers=HEADERS, timeout=10)
        if response.status_code == 200:
            text = response.text
            if "Join the Beta" in text or "To join the" in text or '"status":"ACCEPTING"' in text:
                return "OPEN"
            elif "This beta is full" in text or '"status":"FULL"' in text:
                return "FULL"
        elif response.status_code == 429:
            time.sleep(10)
    except Exception:
        pass
    return "ERROR"

# --- Фоновый мониторинг ссылок ---
def monitor_link():
    while True:
        with lock:
            snapshot_watchers = {link: set(ids) for link, ids in watchers.items()}
            snapshot_state = dict(last_state)

        for link, chat_ids in snapshot_watchers.items():
            if not chat_ids:
                continue

            current_status = check_testflight_slot(link)
            prev_status = snapshot_state.get(link, "FULL")

            # Если место открылось
            if current_status == "OPEN" and prev_status != "OPEN":
                for chat_id in chat_ids:
                    try:
                        bot.send_message(chat_id, f"🟢 Место появилось! Быстрее забирай:\n{link}")
                    except Exception:
                        pass
                with lock:
                    last_state[link] = "OPEN"

            # Если места закончились
            elif current_status == "FULL" and prev_status == "OPEN":
                for chat_id in chat_ids:
                    try:
                        bot.send_message(chat_id, f"🔴 Места закончились. Жду следующего окна для:\n{link}")
                    except Exception:
                        pass
                with lock:
                    last_state[link] = "FULL"

        time.sleep(0.5)

# --- Команды бота ---

@bot.message_handler(commands=['start'])
def send_welcome(message):
    # Запоминаем пользователя
    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    known_users[message.chat.id] = username

    # Текст с кликабельной ссылкой
    text = ("👋 Рад приветствовать всех, а особенно участников <a href='https://t.me/NuviraByteCore_bot'>NuviraByteCore</a>!\n\n"
            "Отправь ссылку TestFlight, чтобы добавить её в отслеживание.\n\n"
            "Команды:\n"
            "📋 /list — посмотреть все твои активные ссылки\n"
            "🗑 /del [ссылка] — удалить конкретную ссылку\n"
            "⛔ /stop — удалить вообще все твои ссылки")
    
    bot.reply_to(message, text, parse_mode='html', disable_web_page_preview=True)

@bot.message_handler(commands=['danyaxap'])
def show_stats(message):
    if not known_users:
        bot.reply_to(message, "Пока никого нет в базе.")
        return
        
    text = f"📊 Всего пользователей: {len(known_users)}\n\nСписок:\n"
    for chat_id, name in known_users.items():
        text += f"👤 {name}\n"
    bot.reply_to(message, text)

@bot.message_handler(commands=['list'])
def list_links(message):
    chat_id = message.chat.id
    active_links = []
    with lock:
        for link, users in watchers.items():
            if chat_id in users:
                active_links.append(link)

    if active_links:
        text = "📋 Ты сейчас отслеживаешь:\n\n" + "\n\n".join(active_links)
        bot.reply_to(message, text)
    else:
        bot.reply_to(message, "Твой список пуст.")

@bot.message_handler(commands=['del'])
def del_link(message):
    chat_id = message.chat.id
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Укажи ссылку после команды.")
        return

    link_to_remove = parts[1].strip()
    removed = False
    with lock:
        if link_to_remove in watchers and chat_id in watchers[link_to_remove]:
            watchers[link_to_remove].discard(chat_id)
            removed = True
            if not watchers[link_to_remove]:
                del watchers[link_to_remove]
                if link_to_remove in last_state:
                    del last_state[link_to_remove]

    if removed:
        bot.reply_to(message, "🗑 Ссылка удалена.")
    else:
        bot.reply_to(message, "Этой ссылки нет в твоем списке.")

@bot.message_handler(commands=['stop'])
def stop_monitoring(message):
    chat_id = message.chat.id
    removed = False
    with lock:
        for link in list(watchers.keys()):
            if chat_id in watchers[link]:
                watchers[link].discard(chat_id)
                removed = True
                if not watchers[link]:
                    del watchers[link]
                    if link in last_state:
                        del last_state[link]
    if removed:
        bot.reply_to(message, "⛔ Все твои ссылки удалены.")
    else:
        bot.reply_to(message, "У тебя нет активных ссылок.")

@bot.message_handler(func=lambda m: 'testflight.apple.com/join/' in m.text and not m.text.startswith('/del'))
def set_link(message):
    chat_id = message.chat.id
    
    # Очистка ссылки от лишнего текста
    match = re.search(r'(https://testflight\.apple\.com/join/[a-zA-Z0-9_-]+)', message.text)
    
    if not match:
        bot.reply_to(message, "❌ Ссылка TestFlight не найдена.")
        return
        
    link = match.group(1)

    with lock:
        if link not in watchers:
            watchers[link] = set()
            last_state[link] = "FULL"
        watchers[link].add(chat_id)
        user_links = sum(1 for users in watchers.values() if chat_id in users)

    bot.reply_to(message, f"✅ Ссылка добавлена. Ищу свободные места. Отслеживается: {user_links}")

if __name__ == '__main__':
    print("Бот запущен.")
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=monitor_link, daemon=True).start()
    bot.infinity_polling(timeout=10, long_polling_timeout=5)
