import telebot
import requests
import time
import threading
import os
from flask import Flask

# --- НАСТРОЙКА ДЛЯ RENDER (чтобы не засыпал) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Render работает 24/7!"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web, daemon=True).start()

# --- ОСНОВНОЙ КОД БОТА ---
TOKEN = '8626634626:AAEJQmGBiOV7wl_CSOssozaEckjHRJOJE-E'
bot = telebot.TeleBot(TOKEN)

user_data = {}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1',
    'Accept-Language': 'en-US,en;q=0.9'
}

def check_testflight_slot(url):
    clean_url = url.split('?')[0]
    no_cache_url = f"{clean_url}?t={int(time.time() * 1000)}"
    
    try:
        response = requests.get(no_cache_url, headers=HEADERS, timeout=10)
        
        if response.status_code == 200:
            text = response.text
            if "Join the Beta" in text or "To join the" in text or '"status":"ACCEPTING"' in text:
                print("[!] НАШЕЛ МЕСТО! Отправляю сообщение...")
                return True
            elif "This beta is full" in text or '"status":"FULL"' in text:
                print("[Х] Мест нет. Жду 0.5 сек...")
                return False
            else:
                return False
        else:
            print(f"[!] Ошибка от серверов: {response.status_code}")
    except Exception as e:
        print(f"Ошибка соединения: {e}")
    return False

def monitor_link():
    while True:
        for chat_id, link in list(user_data.items()):
            if link:
                if check_testflight_slot(link):
                    bot.send_message(chat_id, f"🟢 Место появилось!\nБыстрее забирай: {link}")
                    user_data[chat_id] = None 
                    print("Слежение остановлено.")
        time.sleep(0.5)

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Кидай ссылку. Ищу места (интервал 0.5 сек).")

@bot.message_handler(func=lambda message: 'testflight.apple.com/join/' in message.text)
def set_link(message):
    chat_id = message.chat.id
    link = message.text.strip()
    user_data[chat_id] = link
    bot.reply_to(message, "✅ Принято. Начинаю проверку.")

if __name__ == '__main__':
    threading.Thread(target=monitor_link, daemon=True).start()
    bot.polling(none_stop=True)
