"""
ربات تلگرام ارسال خودکار تبلیغات - نسخه نهایی و پایدار
"""

import os
import sys
import logging
import sqlite3
import json
import time
from datetime import datetime
from functools import wraps

from flask import Flask, request, jsonify
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton

# ==================== تنظیمات ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# متغیرهای محیطی
BOT_TOKEN = os.environ.get('BOT_TOKEN', '7411923756:AAGe7yq7Cu9cfX4oxaXsVxuDfXVxdCrolD8')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', 'https://cod-uyxn.onrender.com')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 7411923756))

# ==================== راه‌اندازی ====================
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ==================== دیتابیس ====================
def get_db():
    conn = sqlite3.connect('bot_data.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT UNIQUE,
                title TEXT,
                username TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS ads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT,
                file_id TEXT,
                type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()

init_db()

# ==================== کیبورد ====================
def main_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("➕ افزودن گروه", "📋 لیست گروه‌ها")
    keyboard.add("📝 ثبت تبلیغ", "▶️ شروع ارسال")
    keyboard.add("⏹ توقف ارسال", "📊 وضعیت")
    return keyboard

# ==================== دکوریتور ادمین ====================
def admin_only(func):
    @wraps(func)
    def wrapper(message):
        if message.from_user.id != ADMIN_ID:
            bot.reply_to(message, "⛔ دسترسی غیرمجاز")
            return
        return func(message)
    return wrapper

# ==================== هندلرها ====================
@bot.message_handler(commands=['start'])
def start(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ دسترسی غیرمجاز")
        return
    
    bot.send_message(
        message.chat.id,
        "🤖 ربات ارسال خودکار فعال است",
        reply_markup=main_keyboard()
    )

@bot.message_handler(func=lambda m: m.text == "➕ افزودن گروه")
@admin_only
def add_group(message):
    msg = bot.send_message(
        message.chat.id,
        "🔹 لطفاً یوزرنیم گروه را با @ ارسال کنید:\nمثال: @mygroup"
    )
    bot.register_next_step_handler(msg, process_group)

def process_group(message):
    username = message.text.strip()
    
    try:
        # دریافت اطلاعات گروه
        chat = bot.get_chat(username)
        
        # ذخیره در دیتابیس
        with get_db() as conn:
            conn.execute(
                'INSERT OR REPLACE INTO groups (chat_id, title, username) VALUES (?, ?, ?)',
                (str(chat.id), chat.title, username)
            )
            conn.commit()
        
        bot.send_message(
            message.chat.id,
            f"✅ گروه {chat.title} با موفقیت اضافه شد",
            reply_markup=main_keyboard()
        )
    except Exception as e:
        bot.send_message(
            message.chat.id,
            f"❌ خطا: {str(e)}",
            reply_markup=main_keyboard()
        )

@bot.message_handler(func=lambda m: m.text == "📋 لیست گروه‌ها")
@admin_only
def list_groups(message):
    with get_db() as conn:
        groups = conn.execute('SELECT * FROM groups').fetchall()
    
    if not groups:
        bot.send_message(message.chat.id, "📭 گروهی ثبت نشده")
        return
    
    text = "📋 لیست گروه‌ها:\n\n"
    for g in groups:
        text += f"• {g['title']} - {g['username']}\n"
    
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "📝 ثبت تبلیغ")
@admin_only
def add_ad(message):
    msg = bot.send_message(
        message.chat.id,
        "🔹 متن تبلیغ را ارسال کنید:"
    )
    bot.register_next_step_handler(msg, process_ad)

def process_ad(message):
    with get_db() as conn:
        conn.execute(
            'INSERT INTO ads (content, type) VALUES (?, ?)',
            (message.text, 'text')
        )
        conn.commit()
    
    bot.send_message(
        message.chat.id,
        "✅ تبلیغ با موفقیت ثبت شد",
        reply_markup=main_keyboard()
    )

@bot.message_handler(func=lambda m: m.text == "📊 وضعیت")
@admin_only
def status(message):
    with get_db() as conn:
        groups = conn.execute('SELECT COUNT(*) as c FROM groups').fetchone()['c']
        ads = conn.execute('SELECT COUNT(*) as c FROM ads').fetchone()['c']
    
    text = f"""
📊 وضعیت سیستم:

👥 تعداد گروه‌ها: {groups}
📝 تعداد تبلیغات: {ads}
⚙️ وضعیت: فعال
    """
    bot.send_message(message.chat.id, text)

# ==================== Webhook ====================
@app.route('/webhook', methods=['POST'])
def webhook():
    """دریافت آپدیت از تلگرام"""
    if request.headers.get('content-type') == 'application/json':
        try:
            update = request.get_data().decode('utf-8')
            update = telebot.types.Update.de_json(update)
            bot.process_new_updates([update])
            return jsonify({'status': 'ok'}), 200
        except Exception as e:
            logger.error(f"خطا: {e}")
            return jsonify({'status': 'error'}), 500
    
    return jsonify({'status': 'bad request'}), 400

@app.route('/')
def health():
    """بررسی سلامت"""
    return jsonify({
        'status': 'running',
        'time': datetime.now().isoformat(),
        'bot': 'active'
    }), 200

@app.route('/set_webhook')
def set_webhook():
    """تنظیم webhook"""
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
        return jsonify({'status': 'ok', 'message': 'webhook set'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ==================== اجرا ====================
if __name__ == '__main__':
    # تنظیم webhook در زمان اجرا
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
        logger.info(f"Webhook تنظیم شد: {WEBHOOK_URL}/webhook")
    except Exception as e:
        logger.error(f"خطا در تنظیم webhook: {e}")
    
    # اجرای سرور
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
