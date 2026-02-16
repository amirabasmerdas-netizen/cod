"""
ربات تلگرام ارسال خودکار تبلیغات
طراحی شده برای دیپلوی روی Render.com
نسخه اصلاح شده برای رفع مشکل دیتابیس
"""

import os
import sys
import logging
import sqlite3
import json
import asyncio
import threading
import time
from datetime import datetime
from functools import wraps
from contextlib import contextmanager

from flask import Flask, request, jsonify
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton

# ==================== تنظیمات اولیه ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# دریافت متغیرهای محیطی
BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN:
    logger.error("BOT_TOKEN تنظیم نشده است!")
    sys.exit(1)

WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
if not WEBHOOK_URL:
    logger.error("WEBHOOK_URL تنظیم نشده است!")
    sys.exit(1)

ADMIN_ID = os.environ.get('ADMIN_ID')
if not ADMIN_ID:
    logger.error("ADMIN_ID تنظیم نشده است!")
    sys.exit(1)

# تبدیل ADMIN_ID به عدد صحیح
try:
    ADMIN_ID = int(ADMIN_ID)
except ValueError:
    logger.error("ADMIN_ID باید یک عدد صحیح باشد!")
    sys.exit(1)

# ==================== راه‌اندازی ربات و Flask ====================
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__)

# ==================== مدیریت دیتابیس ====================
DATABASE = 'bot_data.db'

# قفل برای دسترسی هم‌زمان به دیتابیس
db_lock = threading.Lock()

@contextmanager
def get_db():
    """مدیریت context دیتابیس با قفل"""
    with db_lock:
        conn = sqlite3.connect(DATABASE, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

def init_database():
    """ایجاد جداول دیتابیس"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            
            # جدول گروه‌ها
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT UNIQUE NOT NULL,
                    username TEXT,
                    title TEXT,
                    is_active INTEGER DEFAULT 1,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # جدول تبلیغات
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS advertisements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_type TEXT NOT NULL,
                    content TEXT,
                    file_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active INTEGER DEFAULT 1
                )
            ''')
            
            # جدول تنظیمات زمان‌بندی
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS schedule_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    interval_minutes INTEGER DEFAULT 5,
                    max_sends INTEGER DEFAULT 0,
                    current_sends INTEGER DEFAULT 0,
                    is_running INTEGER DEFAULT 0,
                    last_send_time TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # درج تنظیمات پیش‌فرض اگر وجود نداشته باشد
            cursor.execute('SELECT COUNT(*) as count FROM schedule_settings')
            if cursor.fetchone()['count'] == 0:
                cursor.execute('''
                    INSERT INTO schedule_settings (interval_minutes, max_sends, is_running)
                    VALUES (5, 0, 0)
                ''')
            
            logger.info("✅ دیتابیس با موفقیت راه‌اندازی شد")
    except Exception as e:
        logger.error(f"❌ خطا در راه‌اندازی دیتابیس: {e}")

# ==================== توابع کمکی دیتابیس ====================
def save_group(chat_id, username, title):
    """ذخیره گروه در دیتابیس"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO groups (chat_id, username, title, is_active)
                VALUES (?, ?, ?, 1)
            ''', (str(chat_id), username, title))
            return True
    except Exception as e:
        logger.error(f"خطا در ذخیره گروه: {e}")
        return False

def get_all_groups():
    """دریافت لیست تمام گروه‌ها"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM groups WHERE is_active = 1 ORDER BY added_at DESC')
            return cursor.fetchall()
    except Exception as e:
        logger.error(f"خطا در دریافت گروه‌ها: {e}")
        return []

def save_advertisement(message_type, content=None, file_id=None):
    """ذخیره تبلیغ در دیتابیس"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO advertisements (message_type, content, file_id)
                VALUES (?, ?, ?)
            ''', (message_type, content, file_id))
            return True
    except Exception as e:
        logger.error(f"خطا در ذخیره تبلیغ: {e}")
        return False

def get_active_advertisement():
    """دریافت آخرین تبلیغ فعال"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM advertisements 
                WHERE is_active = 1 
                ORDER BY created_at DESC LIMIT 1
            ''')
            return cursor.fetchone()
    except Exception as e:
        logger.error(f"خطا در دریافت تبلیغ: {e}")
        return None

def remove_inactive_group(chat_id):
    """غیرفعال کردن گروه"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE groups SET is_active = 0 WHERE chat_id = ?', (str(chat_id),))
            return True
    except Exception as e:
        logger.error(f"خطا در غیرفعال کردن گروه: {e}")
        return False

def get_schedule_settings():
    """دریافت تنظیمات زمان‌بندی"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM schedule_settings LIMIT 1')
            return cursor.fetchone()
    except Exception as e:
        logger.error(f"خطا در دریافت تنظیمات: {e}")
        return None

def update_schedule_settings(interval=None, max_sends=None, is_running=None):
    """به‌روزرسانی تنظیمات زمان‌بندی"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            if interval is not None:
                cursor.execute('UPDATE schedule_settings SET interval_minutes = ?', (interval,))
            if max_sends is not None:
                cursor.execute('UPDATE schedule_settings SET max_sends = ?, current_sends = 0', (max_sends,))
            if is_running is not None:
                cursor.execute('UPDATE schedule_settings SET is_running = ?', (1 if is_running else 0,))
            cursor.execute('UPDATE schedule_settings SET updated_at = CURRENT_TIMESTAMP')
            return True
    except Exception as e:
        logger.error(f"خطا در به‌روزرسانی تنظیمات: {e}")
        return False

def increment_send_count():
    """افزایش تعداد ارسال‌ها"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE schedule_settings 
                SET current_sends = current_sends + 1,
                    last_send_time = CURRENT_TIMESTAMP
            ''')
            return True
    except Exception as e:
        logger.error(f"خطا در افزایش تعداد ارسال: {e}")
        return False

# ==================== توابع کمکی ====================
def admin_only(func):
    """دکوریتور برای محدود کردن دسترسی به ادمین"""
    @wraps(func)
    def wrapper(message):
        if message.from_user.id != ADMIN_ID:
            bot.reply_to(message, "⛔ شما اجازه استفاده از این دستور را ندارید.")
            return
        return func(message)
    return wrapper

def get_chat_id_from_username(username):
    """دریافت chat_id از یوزرنیم گروه"""
    try:
        username = username.strip().lstrip('@')
        chat = bot.get_chat(f"@{username}")
        return chat.id, chat.title
    except Exception as e:
        logger.error(f"خطا در دریافت chat_id برای {username}: {e}")
        return None, None

def check_bot_admin(chat_id):
    """بررسی اینکه ربات در گروه ادمین است"""
    try:
        bot_member = bot.get_chat_member(chat_id, bot.get_me().id)
        return bot_member.status in ['administrator', 'creator']
    except Exception as e:
        logger.error(f"خطا در بررسی وضعیت ادمین: {e}")
        return False

def get_main_keyboard():
    """ایجاد کیبورد اصلی"""
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        KeyboardButton("📤 ثبت تبلیغ"),
        KeyboardButton("👥 افزودن گروه")
    )
    keyboard.add(
        KeyboardButton("📋 لیست گروه‌ها"),
        KeyboardButton("⏱ تنظیم زمان ارسال")
    )
    keyboard.add(
        KeyboardButton("▶️ شروع ارسال"),
        KeyboardButton("⛔ توقف ارسال")
    )
    keyboard.add(KeyboardButton("📊 وضعیت"))
    return keyboard

# ==================== مدیریت وضعیت کاربران ====================
user_states = {}

def set_user_state(user_id, state, data=None):
    """تنظیم وضعیت کاربر"""
    user_states[user_id] = {'state': state, 'data': data or {}}

def get_user_state(user_id):
    """دریافت وضعیت کاربر"""
    return user_states.get(user_id, {'state': None, 'data': {}})

def clear_user_state(user_id):
    """پاک کردن وضعیت کاربر"""
    if user_id in user_states:
        del user_states[user_id]

# ==================== هندلرهای ربات ====================
@bot.message_handler(commands=['start'])
def start_command(message):
    """دستور شروع"""
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ شما اجازه استفاده از این ربات را ندارید.")
        return
    
    bot.send_message(
        message.chat.id,
        "🤖 به ربات ارسال خودکار تبلیغات خوش آمدید!\n\nاز منوی زیر استفاده کنید:",
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(func=lambda message: message.text == "📤 ثبت تبلیغ")
@admin_only
def add_advertisement(message):
    """شروع فرآیند ثبت تبلیغ"""
    set_user_state(message.from_user.id, 'waiting_ad_type')
    
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add("متن", "عکس")
    keyboard.add("ویدیو", "فایل")
    keyboard.add("🔙 بازگشت")
    
    bot.send_message(
        message.chat.id,
        "📝 نوع تبلیغ را انتخاب کنید:",
        reply_markup=keyboard
    )

@bot.message_handler(func=lambda message: get_user_state(message.from_user.id)['state'] == 'waiting_ad_type')
@admin_only
def process_ad_type(message):
    """پردازش نوع تبلیغ"""
    if message.text == "🔙 بازگشت":
        clear_user_state(message.from_user.id)
        bot.send_message(message.chat.id, "🔙 بازگشت به منوی اصلی", reply_markup=get_main_keyboard())
        return
    
    type_map = {
        "متن": "text",
        "عکس": "photo",
        "ویدیو": "video",
        "فایل": "document"
    }
    
    if message.text not in type_map:
        bot.reply_to(message, "❌ لطفاً یک گزینه معتبر انتخاب کنید.")
        return
    
    set_user_state(
        message.from_user.id,
        'waiting_ad_content',
        {'type': type_map[message.text]}
    )
    
    if message.text == "متن":
        bot.send_message(message.chat.id, "📝 لطفاً متن تبلیغ را ارسال کنید:")
    else:
        bot.send_message(message.chat.id, f"📎 لطفاً {message.text} مورد نظر را ارسال کنید:")

@bot.message_handler(content_types=['text', 'photo', 'video', 'document'], 
                    func=lambda message: get_user_state(message.from_user.id)['state'] == 'waiting_ad_content')
@admin_only
def process_ad_content(message):
    """پردازش محتوای تبلیغ"""
    user_data = get_user_state(message.from_user.id)['data']
    ad_type = user_data.get('type')
    
    try:
        success = False
        
        if ad_type == 'text' and message.text:
            success = save_advertisement('text', content=message.text)
            reply_text = "✅ متن تبلیغ با موفقیت ثبت شد!"
            
        elif ad_type == 'photo' and message.photo:
            file_id = message.photo[-1].file_id
            caption = message.caption or ""
            success = save_advertisement('photo', content=caption, file_id=file_id)
            reply_text = "✅ عکس با موفقیت ثبت شد!"
            
        elif ad_type == 'video' and message.video:
            file_id = message.video.file_id
            caption = message.caption or ""
            success = save_advertisement('video', content=caption, file_id=file_id)
            reply_text = "✅ ویدیو با موفقیت ثبت شد!"
            
        elif ad_type == 'document' and message.document:
            file_id = message.document.file_id
            caption = message.caption or ""
            success = save_advertisement('document', content=caption, file_id=file_id)
            reply_text = "✅ فایل با موفقیت ثبت شد!"
        
        if success:
            bot.send_message(message.chat.id, reply_text, reply_markup=get_main_keyboard())
            clear_user_state(message.from_user.id)
        else:
            bot.reply_to(message, "❌ خطا در ثبت تبلیغ. لطفاً دوباره تلاش کنید.")
            
    except Exception as e:
        logger.error(f"خطا در پردازش محتوای تبلیغ: {e}")
        bot.reply_to(message, "❌ خطایی رخ داد. لطفاً دوباره تلاش کنید.")

@bot.message_handler(func=lambda message: message.text == "👥 افزودن گروه")
@admin_only
def add_group(message):
    """شروع فرآیند افزودن گروه"""
    set_user_state(message.from_user.id, 'waiting_group_username')
    bot.send_message(
        message.chat.id,
        "🔗 لطفاً یوزرنیم گروه را با @ وارد کنید:\nمثال: @mygroup"
    )

@bot.message_handler(func=lambda message: get_user_state(message.from_user.id)['state'] == 'waiting_group_username')
@admin_only
def process_group_username(message):
    """پردازش یوزرنیم گروه"""
    username = message.text.strip()
    
    chat_id, title = get_chat_id_from_username(username)
    
    if not chat_id:
        bot.reply_to(message, "❌ گروه مورد نظر یافت نشد.")
        return
    
    if not check_bot_admin(chat_id):
        bot.reply_to(message, "❌ ربات در این گروه ادمین نیست.")
        return
    
    if save_group(chat_id, username, title):
        bot.reply_to(message, f"✅ گروه {title} با موفقیت اضافه شد!")
        clear_user_state(message.from_user.id)
    else:
        bot.reply_to(message, "❌ خطا در ذخیره گروه.")

@bot.message_handler(func=lambda message: message.text == "📋 لیست گروه‌ها")
@admin_only
def list_groups(message):
    """نمایش لیست گروه‌ها"""
    groups = get_all_groups()
    
    if not groups:
        bot.send_message(message.chat.id, "📭 هیچ گروه فعالی وجود ندارد.")
        return
    
    text = "📋 لیست گروه‌های فعال:\n\n"
    for i, group in enumerate(groups, 1):
        text += f"{i}. {group['title']}\n"
        text += f"   یوزرنیم: {group['username']}\n"
        text += f"   آیدی: {group['chat_id']}\n\n"
    
    # ارسال در چند بخش اگر طولانی شد
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            bot.send_message(message.chat.id, text[i:i+4000])
    else:
        bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda message: message.text == "⏱ تنظیم زمان ارسال")
@admin_only
def schedule_settings(message):
    """تنظیمات زمان‌بندی"""
    settings = get_schedule_settings()
    
    if not settings:
        bot.reply_to(message, "❌ خطا در دریافت تنظیمات.")
        return
    
    text = "⚙️ تنظیمات فعلی:\n\n"
    text += f"⏱ فاصله ارسال: {settings['interval_minutes']} دقیقه\n"
    text += f"📊 تعداد ارسال: "
    text += "نامحدود" if settings['max_sends'] == 0 else f"{settings['current_sends']}/{settings['max_sends']}\n"
    text += f"▶️ وضعیت: {'فعال' if settings['is_running'] else 'غیرفعال'}\n\n"
    
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add("⏱ تنظیم فاصله", "📊 تنظیم تعداد")
    keyboard.add("🔙 بازگشت")
    
    bot.send_message(message.chat.id, text, reply_markup=keyboard)
    set_user_state(message.from_user.id, 'waiting_schedule_option')

@bot.message_handler(func=lambda message: get_user_state(message.from_user.id)['state'] == 'waiting_schedule_option')
@admin_only
def process_schedule_option(message):
    """پردازش گزینه تنظیم زمان"""
    if message.text == "🔙 بازگشت":
        clear_user_state(message.from_user.id)
        bot.send_message(message.chat.id, "🔙 بازگشت به منوی اصلی", reply_markup=get_main_keyboard())
        return
    
    if message.text == "⏱ تنظیم فاصله":
        set_user_state(message.from_user.id, 'waiting_interval')
        bot.send_message(message.chat.id, "⏱ لطفاً فاصله ارسال را به دقیقه وارد کنید:")
    
    elif message.text == "📊 تنظیم تعداد":
        set_user_state(message.from_user.id, 'waiting_max_sends')
        bot.send_message(message.chat.id, "📊 لطفاً تعداد دفعات ارسال را وارد کنید (0 برای نامحدود):")
    
    else:
        bot.reply_to(message, "❌ گزینه نامعتبر.")

@bot.message_handler(func=lambda message: get_user_state(message.from_user.id)['state'] == 'waiting_interval')
@admin_only
def process_interval(message):
    """پردازش فاصله ارسال"""
    try:
        interval = int(message.text)
        if interval < 1:
            bot.reply_to(message, "❌ فاصله باید حداقل 1 دقیقه باشد.")
            return
        
        if update_schedule_settings(interval=interval):
            bot.reply_to(message, f"✅ فاصله ارسال به {interval} دقیقه تنظیم شد.")
            clear_user_state(message.from_user.id)
            bot.send_message(message.chat.id, "🔙 بازگشت به منوی اصلی", reply_markup=get_main_keyboard())
        else:
            bot.reply_to(message, "❌ خطا در تنظیم فاصله.")
            
    except ValueError:
        bot.reply_to(message, "❌ لطفاً یک عدد وارد کنید.")

@bot.message_handler(func=lambda message: get_user_state(message.from_user.id)['state'] == 'waiting_max_sends')
@admin_only
def process_max_sends(message):
    """پردازش تعداد ارسال"""
    try:
        max_sends = int(message.text)
        if max_sends < 0:
            bot.reply_to(message, "❌ تعداد نمی‌تواند منفی باشد.")
            return
        
        if update_schedule_settings(max_sends=max_sends):
            if max_sends == 0:
                bot.reply_to(message, "✅ تعداد ارسال به حالت نامحدود تنظیم شد.")
            else:
                bot.reply_to(message, f"✅ تعداد ارسال به {max_sends} بار تنظیم شد.")
            
            clear_user_state(message.from_user.id)
            bot.send_message(message.chat.id, "🔙 بازگشت به منوی اصلی", reply_markup=get_main_keyboard())
        else:
            bot.reply_to(message, "❌ خطا در تنظیم تعداد.")
            
    except ValueError:
        bot.reply_to(message, "❌ لطفاً یک عدد وارد کنید.")

@bot.message_handler(func=lambda message: message.text == "▶️ شروع ارسال")
@admin_only
def start_sending(message):
    """شروع ارسال خودکار"""
    if not get_active_advertisement():
        bot.send_message(message.chat.id, "❌ ابتدا یک تبلیغ ثبت کنید.")
        return
    
    if not get_all_groups():
        bot.send_message(message.chat.id, "❌ حداقل یک گروه اضافه کنید.")
        return
    
    if update_schedule_settings(is_running=True):
        bot.send_message(message.chat.id, "✅ ارسال خودکار شروع شد!")
    else:
        bot.send_message(message.chat.id, "❌ خطا در شروع ارسال.")

@bot.message_handler(func=lambda message: message.text == "⛔ توقف ارسال")
@admin_only
def stop_sending(message):
    """توقف ارسال خودکار"""
    if update_schedule_settings(is_running=False):
        bot.send_message(message.chat.id, "⛔ ارسال خودکار متوقف شد.")
    else:
        bot.send_message(message.chat.id, "❌ خطا در توقف ارسال.")

@bot.message_handler(func=lambda message: message.text == "📊 وضعیت")
@admin_only
def show_status(message):
    """نمایش وضعیت"""
    settings = get_schedule_settings()
    ad = get_active_advertisement()
    groups = get_all_groups()
    
    if not settings:
        bot.reply_to(message, "❌ خطا در دریافت وضعیت.")
        return
    
    text = "📊 وضعیت ربات:\n\n"
    text += f"👥 گروه‌های فعال: {len(groups)}\n"
    text += f"📝 تبلیغ: {'✅ موجود' if ad else '❌ ندارد'}\n"
    text += f"⏱ فاصله: {settings['interval_minutes']} دقیقه\n"
    text += f"📨 ارسال شده: {settings['current_sends']}\n"
    text += f"🎯 هدف: {'نامحدود' if settings['max_sends'] == 0 else settings['max_sends']}\n"
    text += f"⚡ وضعیت: {'✅ فعال' if settings['is_running'] else '⏸ غیرفعال'}\n"
    
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda message: message.text == "🔙 بازگشت")
def back_to_main(message):
    """بازگشت به منوی اصلی"""
    clear_user_state(message.from_user.id)
    bot.send_message(message.chat.id, "🔙 بازگشت به منوی اصلی", reply_markup=get_main_keyboard())

# ==================== Webhook و Flask ====================
@app.route('/webhook', methods=['POST'])
def webhook():
    """دریافت آپدیت‌های تلگرام"""
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    return 'OK', 200

@app.route('/')
def health_check():
    """بررسی سلامت ربات"""
    return jsonify({
        'status': 'running',
        'time': datetime.now().isoformat()
    }), 200

@app.route('/set_webhook', methods=['GET'])
def set_webhook():
    """تنظیم webhook"""
    try:
        bot.remove_webhook()
        time.sleep(1)
        webhook_url = f"{WEBHOOK_URL}/webhook"
        bot.set_webhook(url=webhook_url)
        return jsonify({'status': 'success', 'webhook': webhook_url})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ==================== اجرای اصلی ====================
if __name__ == '__main__':
    # راه‌اندازی دیتابیس
    init_database()
    
    # تنظیم webhook
    try:
        webhook_url = f"{WEBHOOK_URL}/webhook"
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=webhook_url)
        logger.info(f"✅ Webhook تنظیم شد: {webhook_url}")
    except Exception as e:
        logger.error(f"❌ خطا در تنظیم webhook: {e}")
    
    # اجرای سرور
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
