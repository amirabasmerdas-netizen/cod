"""
ربات تلگرام ارسال خودکار تبلیغات
نسخه نهایی با دیتابیس قدرتمند و بدون خطا
طراحی شده برای دیپلوی روی Render.com
"""

import os
import sys
import logging
import sqlite3
import time
import threading
import json
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
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
ADMIN_ID = os.environ.get('ADMIN_ID')

if not all([BOT_TOKEN, WEBHOOK_URL, ADMIN_ID]):
    logger.error("❌ متغیرهای محیطی تنظیم نشده‌اند!")
    logger.error("لطفاً BOT_TOKEN, WEBHOOK_URL, ADMIN_ID را تنظیم کنید.")
    sys.exit(1)

try:
    ADMIN_ID = int(ADMIN_ID)
    logger.info(f"✅ ADMIN_ID: {ADMIN_ID}")
except ValueError:
    logger.error("❌ ADMIN_ID باید یک عدد صحیح باشد!")
    sys.exit(1)

logger.info(f"✅ BOT_TOKEN: {BOT_TOKEN[:10]}...")
logger.info(f"✅ WEBHOOK_URL: {WEBHOOK_URL}")

# ==================== راه‌اندازی ربات و Flask ====================
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__)

# ==================== دیتابیس پیشرفته ====================
DATABASE = 'bot_data.db'
db_lock = threading.RLock()  # قفل بازگشتی برای امنیت بیشتر

@contextmanager
def get_db():
    """مدیریت اتصال به دیتابیس با قفل"""
    with db_lock:
        conn = None
        try:
            conn = sqlite3.connect(DATABASE, timeout=30)
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"❌ خطای دیتابیس: {e}")
            raise
        finally:
            if conn:
                conn.close()

def reset_database():
    """حذف و ایجاد مجدد دیتابیس با ساختار صحیح"""
    try:
        # حذف فایل دیتابیس قدیمی اگر خراب باشد
        if os.path.exists(DATABASE):
            try:
                # تست اینکه فایل دیتابیس سالم است
                with sqlite3.connect(DATABASE) as test_conn:
                    test_conn.execute("SELECT 1")
                logger.info("✅ دیتابیس موجود سالم است")
                return False  # دیتابیس سالم است، نیازی به ریست نیست
            except sqlite3.DatabaseError:
                logger.warning("⚠️ دیتابیس خراب است، در حال حذف...")
                os.remove(DATABASE)
                logger.info("✅ فایل دیتابیس خراب حذف شد")
        
        # ایجاد دیتابیس جدید
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
                CREATE TABLE IF NOT EXISTS ads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_type TEXT NOT NULL,
                    content TEXT,
                    file_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active INTEGER DEFAULT 1
                )
            ''')
            
            # جدول تنظیمات
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    interval_minutes INTEGER DEFAULT 5,
                    max_sends INTEGER DEFAULT 0,
                    current_sends INTEGER DEFAULT 0,
                    is_running INTEGER DEFAULT 0,
                    last_send_time TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # جدول لاگ خطاها
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS error_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    error_type TEXT,
                    error_message TEXT,
                    chat_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # اضافه کردن تنظیمات پیش‌فرض
            cursor.execute('SELECT COUNT(*) as count FROM settings')
            if cursor.fetchone()['count'] == 0:
                cursor.execute('''
                    INSERT INTO settings (interval_minutes, max_sends, is_running)
                    VALUES (5, 0, 0)
                ''')
                logger.info("✅ تنظیمات پیش‌فرض اضافه شد")
            
            logger.info("✅ دیتابیس جدید با موفقیت ساخته شد")
            return True
            
    except Exception as e:
        logger.error(f"❌ خطا در ریست دیتابیس: {e}")
        return False

# ==================== توابع دیتابیس ====================
def add_group_to_db(chat_id, username, title):
    """افزودن گروه به دیتابیس"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO groups (chat_id, username, title, is_active)
                VALUES (?, ?, ?, 1)
            ''', (str(chat_id), username, title))
            logger.info(f"✅ گروه {title} با آیدی {chat_id} ذخیره شد")
            return True
    except Exception as e:
        logger.error(f"❌ خطا در ذخیره گروه: {e}")
        log_error_to_db("add_group_error", str(e), str(chat_id))
        return False

def get_all_groups_from_db():
    """دریافت همه گروه‌های فعال"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM groups WHERE is_active = 1 ORDER BY added_at DESC')
            return cursor.fetchall()
    except Exception as e:
        logger.error(f"❌ خطا در دریافت گروه‌ها: {e}")
        return []

def get_group_count():
    """تعداد گروه‌های فعال"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) as count FROM groups WHERE is_active = 1')
            result = cursor.fetchone()
            return result['count'] if result else 0
    except Exception as e:
        logger.error(f"❌ خطا در دریافت تعداد گروه‌ها: {e}")
        return 0

def remove_group_from_db(chat_id):
    """غیرفعال کردن گروه"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE groups SET is_active = 0 WHERE chat_id = ?', (str(chat_id),))
            logger.info(f"✅ گروه {chat_id} غیرفعال شد")
            return True
    except Exception as e:
        logger.error(f"❌ خطا در غیرفعال کردن گروه: {e}")
        return False

def save_ad_to_db(message_type, content=None, file_id=None):
    """ذخیره تبلیغ"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            # غیرفعال کردن تبلیغات قبلی
            cursor.execute('UPDATE ads SET is_active = 0')
            # ثبت تبلیغ جدید
            cursor.execute('''
                INSERT INTO ads (message_type, content, file_id, is_active)
                VALUES (?, ?, ?, 1)
            ''', (message_type, content, file_id))
            logger.info(f"✅ تبلیغ جدید ثبت شد: {message_type}")
            return True
    except Exception as e:
        logger.error(f"❌ خطا در ذخیره تبلیغ: {e}")
        log_error_to_db("ad_save_error", str(e))
        return False

def get_active_ad_from_db():
    """دریافت آخرین تبلیغ فعال"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM ads WHERE is_active = 1 ORDER BY created_at DESC LIMIT 1')
            return cursor.fetchone()
    except Exception as e:
        logger.error(f"❌ خطا در دریافت تبلیغ: {e}")
        return None

def get_settings_from_db():
    """دریافت تنظیمات"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM settings LIMIT 1')
            return cursor.fetchone()
    except Exception as e:
        logger.error(f"❌ خطا در دریافت تنظیمات: {e}")
        return None

def update_settings_in_db(interval=None, max_sends=None, is_running=None):
    """به‌روزرسانی تنظیمات"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            if interval is not None:
                cursor.execute('UPDATE settings SET interval_minutes = ?', (interval,))
            if max_sends is not None:
                cursor.execute('UPDATE settings SET max_sends = ?, current_sends = 0', (max_sends,))
            if is_running is not None:
                cursor.execute('UPDATE settings SET is_running = ?', (1 if is_running else 0,))
            cursor.execute('UPDATE settings SET updated_at = CURRENT_TIMESTAMP')
            logger.info("✅ تنظیمات به‌روزرسانی شد")
            return True
    except Exception as e:
        logger.error(f"❌ خطا در به‌روزرسانی تنظیمات: {e}")
        return False

def increment_send_count_in_db():
    """افزایش شمارنده ارسال"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE settings 
                SET current_sends = current_sends + 1,
                    last_send_time = CURRENT_TIMESTAMP
            ''')
            return True
    except Exception as e:
        logger.error(f"❌ خطا در افزایش شمارنده: {e}")
        return False

def log_error_to_db(error_type, error_message, chat_id=None):
    """ثبت خطا در دیتابیس"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO error_logs (error_type, error_message, chat_id)
                VALUES (?, ?, ?)
            ''', (error_type, str(error_message)[:500], str(chat_id) if chat_id else None))
    except:
        pass  # اگر خطا در ثبت خطا باشد، نادیده بگیر

# ==================== توابع کمکی ====================
def admin_only(func):
    """دکوراتور محدودیت دسترسی ادمین"""
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
        logger.error(f"❌ خطا در دریافت chat_id برای {username}: {e}")
        log_error_to_db("get_chat_error", str(e), username)
        return None, None

def check_bot_admin(chat_id):
    """بررسی ادمین بودن ربات در گروه"""
    try:
        bot_member = bot.get_chat_member(chat_id, bot.get_me().id)
        is_admin = bot_member.status in ['administrator', 'creator']
        logger.info(f"🔍 بررسی ادمین در {chat_id}: {is_admin}")
        return is_admin
    except Exception as e:
        logger.error(f"❌ خطا در بررسی ادمین: {e}")
        log_error_to_db("check_admin_error", str(e), str(chat_id))
        return False

def get_main_keyboard():
    """کیبورد اصلی"""
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        KeyboardButton("📤 ثبت تبلیغ"),
        KeyboardButton("👥 افزودن گروه")
    )
    keyboard.add(
        KeyboardButton("📋 لیست گروه‌ها"),
        KeyboardButton("⏱ تنظیم زمان")
    )
    keyboard.add(
        KeyboardButton("▶️ شروع ارسال"),
        KeyboardButton("⛔ توقف ارسال")
    )
    keyboard.add(KeyboardButton("📊 وضعیت"))
    return keyboard

# ==================== مدیریت وضعیت کاربران ====================
user_states = {}
user_data = {}

def set_user_state(user_id, state, data=None):
    """تنظیم وضعیت کاربر"""
    user_states[user_id] = state
    if data:
        user_data[user_id] = data
    else:
        user_data[user_id] = {}

def get_user_state(user_id):
    """دریافت وضعیت کاربر"""
    return user_states.get(user_id)

def get_user_data(user_id):
    """دریافت داده‌های کاربر"""
    return user_data.get(user_id, {})

def clear_user_state(user_id):
    """پاک کردن وضعیت کاربر"""
    if user_id in user_states:
        del user_states[user_id]
    if user_id in user_data:
        del user_data[user_id]

# ==================== هندلرهای ربات ====================
@bot.message_handler(commands=['start'])
def start_command(message):
    """دستور شروع"""
    user_id = message.from_user.id
    logger.info(f"📨 پیام start از {user_id}")
    
    if user_id != ADMIN_ID:
        bot.reply_to(message, "🤖 ربات ارسال خودکار تبلیغات فعال است.")
        return
    
    bot.send_message(
        message.chat.id,
        "✨ به ربات مدیریت تبلیغات خوش آمدید!\n\n"
        "از طریق دکمه‌های زیر می‌توانید ربات را مدیریت کنید:",
        reply_markup=get_main_keyboard()
    )

# ==================== ثبت تبلیغ ====================
@bot.message_handler(func=lambda m: m.text == "📤 ثبت تبلیغ")
@admin_only
def add_advertisement(message):
    """شروع ثبت تبلیغ"""
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

@bot.message_handler(func=lambda m: get_user_state(m.from_user.id) == 'waiting_ad_type')
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
                    func=lambda m: get_user_state(m.from_user.id) == 'waiting_ad_content')
@admin_only
def process_ad_content(message):
    """پردازش محتوای تبلیغ"""
    user_info = get_user_data(message.from_user.id)
    ad_type = user_info.get('type')
    
    try:
        success = False
        
        if ad_type == 'text' and message.text:
            success = save_ad_to_db('text', content=message.text)
            reply_text = "✅ متن تبلیغ با موفقیت ثبت شد!"
            
        elif ad_type == 'photo' and message.photo:
            file_id = message.photo[-1].file_id
            caption = message.caption or ""
            success = save_ad_to_db('photo', content=caption, file_id=file_id)
            reply_text = "✅ عکس با موفقیت ثبت شد!"
            
        elif ad_type == 'video' and message.video:
            file_id = message.video.file_id
            caption = message.caption or ""
            success = save_ad_to_db('video', content=caption, file_id=file_id)
            reply_text = "✅ ویدیو با موفقیت ثبت شد!"
            
        elif ad_type == 'document' and message.document:
            file_id = message.document.file_id
            caption = message.caption or ""
            success = save_ad_to_db('document', content=caption, file_id=file_id)
            reply_text = "✅ فایل با موفقیت ثبت شد!"
        
        if success:
            bot.send_message(message.chat.id, reply_text, reply_markup=get_main_keyboard())
            clear_user_state(message.from_user.id)
        else:
            bot.reply_to(message, "❌ خطا در ثبت تبلیغ. لطفاً دوباره تلاش کنید.")
            
    except Exception as e:
        logger.error(f"❌ خطا در پردازش تبلیغ: {e}")
        log_error_to_db("ad_save_error", str(e), str(message.from_user.id))
        bot.reply_to(message, "❌ خطایی رخ داد. لطفاً دوباره تلاش کنید.")

# ==================== افزودن گروه ====================
@bot.message_handler(func=lambda m: m.text == "👥 افزودن گروه")
@admin_only
def add_group_start(message):
    """شروع افزودن گروه"""
    set_user_state(message.from_user.id, 'waiting_group_username')
    bot.send_message(
        message.chat.id,
        "🔗 لطفاً یوزرنیم گروه را با @ وارد کنید:\n"
        "مثال: @mygroup\n\n"
        "⚠️ نکته: ربات باید در گروه ادمین باشد."
    )

@bot.message_handler(func=lambda m: get_user_state(m.from_user.id) == 'waiting_group_username')
@admin_only
def process_group_username(message):
    """پردازش یوزرنیم گروه"""
    username = message.text.strip()
    
    try:
        # دریافت اطلاعات گروه
        chat_id, title = get_chat_id_from_username(username)
        
        if not chat_id:
            bot.reply_to(
                message, 
                "❌ گروه مورد نظر یافت نشد.\n"
                "مطمئن شوید:\n"
                "1. یوزرنیم صحیح است\n"
                "2. ربات در گروه عضو است"
            )
            return
        
        # بررسی ادمین بودن ربات
        if not check_bot_admin(chat_id):
            bot.reply_to(
                message,
                f"❌ ربات در گروه {title} ادمین نیست!\n"
                "لطفاً ابتدا ربات را به عنوان ادمین به گروه اضافه کنید."
            )
            return
        
        # ذخیره در دیتابیس
        if add_group_to_db(chat_id, username, title):
            bot.reply_to(
                message,
                f"✅ گروه با موفقیت اضافه شد!\n\n"
                f"📌 نام: {title}\n"
                f"🆔 آیدی: {chat_id}\n"
                f"🔗 یوزرنیم: {username}"
            )
            clear_user_state(message.from_user.id)
        else:
            bot.reply_to(message, "❌ خطا در ذخیره گروه. لطفاً دوباره تلاش کنید.")
            
    except Exception as e:
        logger.error(f"❌ خطا در پردازش گروه: {e}")
        log_error_to_db("add_group_error", str(e), username)
        bot.reply_to(message, "❌ خطا در ارتباط با سرور. لطفاً دوباره تلاش کنید.")

# ==================== لیست گروه‌ها ====================
@bot.message_handler(func=lambda m: m.text == "📋 لیست گروه‌ها")
@admin_only
def list_groups(message):
    """نمایش لیست گروه‌ها"""
    groups = get_all_groups_from_db()
    
    if not groups:
        bot.send_message(
            message.chat.id,
            "📭 هیچ گروه فعالی وجود ندارد.\n"
            "با دکمه '👥 افزودن گروه' گروه جدید اضافه کنید."
        )
        return
    
    text = "📋 **لیست گروه‌های فعال**\n"
    text += "═" * 25 + "\n\n"
    
    for i, group in enumerate(groups, 1):
        text += f"**{i}.** {group['title']}\n"
        text += f"   🆔 آیدی: `{group['chat_id']}`\n"
        text += f"   🔗 یوزرنیم: {group['username']}\n"
        text += "─" * 20 + "\n"
    
    text += f"\n📊 **تعداد کل:** {len(groups)} گروه"
    
    # ارسال در چند بخش اگر طولانی شد
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            bot.send_message(message.chat.id, text[i:i+4000], parse_mode='Markdown')
    else:
        bot.send_message(message.chat.id, text, parse_mode='Markdown')

# ==================== تنظیم زمان ====================
@bot.message_handler(func=lambda m: m.text == "⏱ تنظیم زمان")
@admin_only
def schedule_settings(message):
    """تنظیمات زمان‌بندی"""
    settings = get_settings_from_db()
    
    if not settings:
        bot.reply_to(message, "❌ خطا در دریافت تنظیمات.")
        return
    
    text = "⚙️ **تنظیمات فعلی**\n"
    text += "═" * 25 + "\n\n"
    text += f"⏱ **فاصله ارسال:** {settings['interval_minutes']} دقیقه\n"
    text += f"📊 **تعداد ارسال:** "
    
    if settings['max_sends'] == 0:
        text += "نامحدود\n"
    else:
        text += f"{settings['current_sends']}/{settings['max_sends']}\n"
    
    text += f"▶️ **وضعیت:** {'فعال' if settings['is_running'] else 'غیرفعال'}\n\n"
    text += "لطفاً گزینه مورد نظر را انتخاب کنید:"
    
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add("⏱ تنظیم فاصله", "📊 تنظیم تعداد")
    keyboard.add("🔙 بازگشت")
    
    bot.send_message(message.chat.id, text, reply_markup=keyboard, parse_mode='Markdown')
    set_user_state(message.from_user.id, 'waiting_schedule_option')

@bot.message_handler(func=lambda m: get_user_state(m.from_user.id) == 'waiting_schedule_option')
@admin_only
def process_schedule_option(message):
    """پردازش گزینه تنظیمات"""
    if message.text == "🔙 بازگشت":
        clear_user_state(message.from_user.id)
        bot.send_message(message.chat.id, "🔙 بازگشت به منوی اصلی", reply_markup=get_main_keyboard())
        return
    
    if message.text == "⏱ تنظیم فاصله":
        set_user_state(message.from_user.id, 'waiting_interval')
        bot.send_message(
            message.chat.id,
            "⏱ لطفاً فاصله ارسال را به دقیقه وارد کنید:\n"
            "(مثال: 5 برای ارسال هر 5 دقیقه)"
        )
    
    elif message.text == "📊 تنظیم تعداد":
        set_user_state(message.from_user.id, 'waiting_max_sends')
        bot.send_message(
            message.chat.id,
            "📊 لطفاً تعداد دفعات ارسال را وارد کنید:\n"
            "(0 برای ارسال نامحدود)"
        )
    
    else:
        bot.reply_to(message, "❌ گزینه نامعتبر است.")

@bot.message_handler(func=lambda m: get_user_state(m.from_user.id) == 'waiting_interval')
@admin_only
def process_interval(message):
    """پردازش فاصله زمانی"""
    try:
        interval = int(message.text)
        if interval < 1:
            bot.reply_to(message, "❌ فاصله ارسال باید حداقل 1 دقیقه باشد.")
            return
        
        if update_settings_in_db(interval=interval):
            bot.reply_to(message, f"✅ فاصله ارسال به {interval} دقیقه تنظیم شد.")
            clear_user_state(message.from_user.id)
            bot.send_message(message.chat.id, "🔙 بازگشت به منوی اصلی", reply_markup=get_main_keyboard())
        else:
            bot.reply_to(message, "❌ خطا در تنظیم فاصله.")
            
    except ValueError:
        bot.reply_to(message, "❌ لطفاً یک عدد صحیح وارد کنید.")

@bot.message_handler(func=lambda m: get_user_state(m.from_user.id) == 'waiting_max_sends')
@admin_only
def process_max_sends(message):
    """پردازش تعداد ارسال"""
    try:
        max_sends = int(message.text)
        if max_sends < 0:
            bot.reply_to(message, "❌ تعداد ارسال نمی‌تواند منفی باشد.")
            return
        
        if update_settings_in_db(max_sends=max_sends):
            if max_sends == 0:
                bot.reply_to(message, "✅ تعداد ارسال به حالت نامحدود تنظیم شد.")
            else:
                bot.reply_to(message, f"✅ تعداد ارسال به {max_sends} بار تنظیم شد.")
            
            clear_user_state(message.from_user.id)
            bot.send_message(message.chat.id, "🔙 بازگشت به منوی اصلی", reply_markup=get_main_keyboard())
        else:
            bot.reply_to(message, "❌ خطا در تنظیم تعداد.")
            
    except ValueError:
        bot.reply_to(message, "❌ لطفاً یک عدد صحیح وارد کنید.")

# ==================== شروع و توقف ارسال ====================
@bot.message_handler(func=lambda m: m.text == "▶️ شروع ارسال")
@admin_only
def start_sending(message):
    """شروع ارسال خودکار"""
    # بررسی وجود تبلیغ
    ad = get_active_ad_from_db()
    if not ad:
        bot.send_message(
            message.chat.id,
            "❌ **خطا در شروع ارسال**\n\n"
            "هیچ تبلیغ فعالی یافت نشد.\n"
            "لطفاً ابتدا با دکمه '📤 ثبت تبلیغ' یک تبلیغ ثبت کنید."
        )
        return
    
    # بررسی وجود گروه
    groups = get_all_groups_from_db()
    if not groups:
        bot.send_message(
            message.chat.id,
            "❌ **خطا در شروع ارسال**\n\n"
            "هیچ گروه فعالی یافت نشد.\n"
            "لطفاً ابتدا با دکمه '👥 افزودن گروه' گروه اضافه کنید."
        )
        return
    
    # شروع ارسال
    if update_settings_in_db(is_running=True):
        bot.send_message(
            message.chat.id,
            "✅ **ارسال خودکار شروع شد**\n\n"
            f"📌 تعداد گروه‌ها: {len(groups)}\n"
            f"📝 نوع تبلیغ: {ad['message_type']}\n"
            "⚡ ربات طبق تنظیمات عمل خواهد کرد."
        )
        logger.info("✅ ارسال خودکار شروع شد")
    else:
        bot.send_message(message.chat.id, "❌ خطا در شروع ارسال.")

@bot.message_handler(func=lambda m: m.text == "⛔ توقف ارسال")
@admin_only
def stop_sending(message):
    """توقف ارسال خودکار"""
    if update_settings_in_db(is_running=False):
        bot.send_message(
            message.chat.id,
            "⛔ **ارسال خودکار متوقف شد**\n\n"
            "برای شروع مجدد از دکمه '▶️ شروع ارسال' استفاده کنید."
        )
        logger.info("⛔ ارسال خودکار متوقف شد")
    else:
        bot.send_message(message.chat.id, "❌ خطا در توقف ارسال.")

# ==================== وضعیت ====================
@bot.message_handler(func=lambda m: m.text == "📊 وضعیت")
@admin_only
def show_status(message):
    """نمایش وضعیت کامل ربات"""
    settings = get_settings_from_db()
    ad = get_active_ad_from_db()
    groups = get_all_groups_from_db()
    
    if not settings:
        bot.reply_to(message, "❌ خطا در دریافت وضعیت.")
        return
    
    text = "📊 **وضعیت ربات**\n"
    text += "═" * 25 + "\n\n"
    
    # وضعیت گروه‌ها
    text += f"👥 **گروه‌ها:** {len(groups)} گروه فعال\n"
    
    # وضعیت تبلیغ
    if ad:
        text += f"📝 **تبلیغ:** ✅ فعال ({ad['message_type']})\n"
    else:
        text += f"📝 **تبلیغ:** ❌ ثبت نشده\n"
    
    # تنظیمات زمان
    text += f"⏱ **فاصله ارسال:** {settings['interval_minutes']} دقیقه\n"
    
    # تعداد ارسال
    if settings['max_sends'] == 0:
        text += f"📨 **ارسال شده:** {settings['current_sends']} (نامحدود)\n"
    else:
        text += f"📨 **ارسال شده:** {settings['current_sends']}/{settings['max_sends']}\n"
    
    # وضعیت اجرا
    status_emoji = "✅" if settings['is_running'] else "⏸"
    status_text = "در حال ارسال" if settings['is_running'] else "متوقف"
    text += f"⚡ **وضعیت:** {status_emoji} {status_text}\n"
    
    if settings['last_send_time']:
        text += f"🕐 **آخرین ارسال:** {settings['last_send_time']}\n"
    
    bot.send_message(message.chat.id, text, parse_mode='Markdown')

# ==================== بازگشت ====================
@bot.message_handler(func=lambda m: m.text == "🔙 بازگشت")
def back_to_main(message):
    """بازگشت به منوی اصلی"""
    clear_user_state(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "🔙 بازگشت به منوی اصلی",
        reply_markup=get_main_keyboard()
    )

# ==================== هندلر اضافه کردن گروه با کامند ====================
@bot.message_handler(commands=['addgroup'])
@admin_only
def add_group_by_command(message):
    """اضافه کردن گروه با دستور /addgroup آیدی_گروه یا یوزرنیم"""
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "❌ فرمت صحیح: /addgroup @یوزرنیم یا /addgroup -100123456789")
            return
        
        group_identifier = parts[1].strip()
        
        # تشخیص اینکه یوزرنیم است یا آیدی عددی
        if group_identifier.startswith('@') or not group_identifier.replace('-', '').isdigit():
            # یوزرنیم
            chat_id, title = get_chat_id_from_username(group_identifier)
        else:
            # آیدی عددی
            chat_id = int(group_identifier)
            try:
                chat = bot.get_chat(chat_id)
                title = chat.title
                group_identifier = f"@{chat.username}" if chat.username else str(chat_id)
            except Exception as e:
                bot.reply_to(message, f"❌ خطا در دریافت اطلاعات گروه: {e}")
                return
        
        if not chat_id:
            bot.reply_to(message, "❌ گروه مورد نظر یافت نشد.")
            return
        
        # بررسی ادمین بودن ربات
        if not check_bot_admin(chat_id):
            bot.reply_to(
                message,
                f"❌ ربات در گروه {title} ادمین نیست!\n"
                "لطفاً ابتدا ربات را ادمین کنید."
            )
            return
        
        # ذخیره در دیتابیس
        if add_group_to_db(chat_id, group_identifier, title):
            bot.reply_to(
                message,
                f"✅ گروه با موفقیت اضافه شد!\n\n"
                f"📌 نام: {title}\n"
                f"🆔 آیدی: {chat_id}\n"
                f"🔗 شناسه: {group_identifier}"
            )
        else:
            bot.reply_to(message, "❌ خطا در ذخیره گروه.")
            
    except Exception as e:
        logger.error(f"❌ خطا در addgroup: {e}")
        bot.reply_to(message, f"❌ خطا: {e}")

# ==================== هندلر پیش‌فرض ====================
@bot.message_handler(func=lambda m: True)
def default_handler(message):
    """هندلر پیش‌فرض برای پیام‌های ناشناخته"""
    if message.from_user.id == ADMIN_ID:
        bot.reply_to(
            message,
            "❓ دستور نامشخص. لطفاً از دکمه‌های منو استفاده کنید.",
            reply_markup=get_main_keyboard()
        )

# ==================== سیستم ارسال خودکار ====================
def auto_sender_worker():
    """کارگر پس‌زمینه برای ارسال خودکار"""
    logger.info("🔄 سیستم ارسال خودکار راه‌اندازی شد")
    
    while True:
        try:
            # دریافت تنظیمات
            settings = get_settings_from_db()
            
            if settings and settings['is_running']:
                # دریافت تبلیغ فعال
                ad = get_active_ad_from_db()
                
                # دریافت گروه‌های فعال
                groups = get_all_groups_from_db()
                
                if ad and groups:
                    logger.info(f"📨 شروع ارسال به {len(groups)} گروه")
                    
                    for group in groups:
                        try:
                            chat_id = int(group['chat_id'])
                            
                            # ارسال بر اساس نوع پیام
                            if ad['message_type'] == 'text':
                                bot.send_message(chat_id, ad['content'])
                            elif ad['message_type'] == 'photo':
                                bot.send_photo(chat_id, ad['file_id'], caption=ad['content'] or '')
                            elif ad['message_type'] == 'video':
                                bot.send_video(chat_id, ad['file_id'], caption=ad['content'] or '')
                            elif ad['message_type'] == 'document':
                                bot.send_document(chat_id, ad['file_id'], caption=ad['content'] or '')
                            
                            logger.info(f"✅ ارسال به {chat_id} موفق")
                            time.sleep(2)  # تاخیر بین ارسال‌ها
                            
                        except Exception as e:
                            logger.error(f"❌ خطا در ارسال به {group['chat_id']}: {e}")
                            log_error_to_db("send_error", str(e), group['chat_id'])
                            
                            # اگر ربات از گروه حذف شده
                            if "chat not found" in str(e).lower() or "bot was kicked" in str(e).lower():
                                remove_group_from_db(group['chat_id'])
                    
                    # افزایش شمارنده
                    increment_send_count_in_db()
                    
                    # بررسی محدودیت تعداد
                    if settings['max_sends'] > 0 and settings['current_sends'] + 1 >= settings['max_sends']:
                        update_settings_in_db(is_running=False)
                        logger.info("⛔ محدودیت تعداد رسید، ارسال متوقف شد")
            
            # خواب بر اساس تنظیمات
            sleep_time = (settings['interval_minutes'] * 60) if settings else 300
            time.sleep(sleep_time)
            
        except Exception as e:
            logger.error(f"❌ خطا در auto_sender: {e}")
            log_error_to_db("auto_sender_error", str(e))
            time.sleep(60)

# ==================== مسیرهای Flask ====================
@app.route('/webhook', methods=['POST'])
def webhook():
    """دریافت آپدیت‌های تلگرام"""
    if request.headers.get('content-type') == 'application/json':
        try:
            json_string = request.get_data().decode('utf-8')
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])
            return 'OK', 200
        except Exception as e:
            logger.error(f"❌ خطا در پردازش webhook: {e}")
            return 'OK', 200
    return 'OK', 200

@app.route('/')
def health_check():
    """بررسی سلامت ربات"""
    try:
        bot_info = bot.get_me()
        groups_count = get_group_count()
        settings = get_settings_from_db()
        
        return jsonify({
            'status': 'running',
            'bot': bot_info.username,
            'groups': groups_count,
            'is_running': settings['is_running'] if settings else False,
            'time': datetime.now().isoformat()
        }), 200
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/set_webhook', methods=['GET'])
def set_webhook():
    """تنظیم webhook"""
    try:
        bot.remove_webhook()
        time.sleep(1)
        webhook_url = f"{WEBHOOK_URL}/webhook"
        bot.set_webhook(url=webhook_url)
        
        return jsonify({
            'status': 'success',
            'webhook_url': webhook_url,
            'message': '✅ Webhook با موفقیت تنظیم شد'
        }), 200
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/webhook_info', methods=['GET'])
def webhook_info():
    """دریافت اطلاعات webhook"""
    try:
        info = bot.get_webhook_info()
        return jsonify(dict(info))
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/db_test', methods=['GET'])
def db_test():
    """تست دیتابیس"""
    try:
        groups = get_all_groups_from_db()
        ad = get_active_ad_from_db()
        settings = get_settings_from_db()
        
        return jsonify({
            'groups_count': len(groups),
            'groups': [dict(g) for g in groups],
            'ad': dict(ad) if ad else None,
            'settings': dict(settings) if settings else None
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/add_group_direct/<username>', methods=['GET'])
def add_group_direct(username):
    """اضافه کردن مستقیم گروه با یوزرنیم (برای تست)"""
    try:
        # دریافت chat_id
        chat = bot.get_chat(f"@{username}")
        chat_id = chat.id
        title = chat.title
        
        # ذخیره در دیتابیس
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO groups (chat_id, username, title, is_active)
                VALUES (?, ?, ?, 1)
            ''', (str(chat_id), f"@{username}", title))
            
        return jsonify({
            'success': True,
            'chat_id': chat_id,
            'title': title,
            'message': f'✅ گروه {title} با موفقیت اضافه شد'
        }), 200
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400

@app.route('/reset_db', methods=['GET'])
def reset_db_route():
    """ریست کردن دیتابیس"""
    try:
        if reset_database():
            return jsonify({'success': True, 'message': '✅ دیتابیس با موفقیت ریست شد'})
        else:
            return jsonify({'success': False, 'message': '❌ خطا در ریست دیتابیس'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== اجرای اصلی ====================
if __name__ == '__main__':
    try:
        # راه‌اندازی دیتابیس
        logger.info("🔄 راه‌اندازی دیتابیس...")
        reset_database()
        
        # تست دیتابیس
        test_groups = get_all_groups_from_db()
        logger.info(f"✅ دیتابیس آماده است. {len(test_groups)} گروه در دیتابیس")
        
        # شروع ارسال خودکار در پس‌زمینه
        sender_thread = threading.Thread(target=auto_sender_worker, daemon=True)
        sender_thread.start()
        logger.info("✅ سیستم ارسال خودکار راه‌اندازی شد")
        
        # تنظیم webhook
        try:
            bot.remove_webhook()
            time.sleep(1)
            webhook_url = f"{WEBHOOK_URL}/webhook"
            bot.set_webhook(url=webhook_url)
            logger.info(f"✅ Webhook تنظیم شد: {webhook_url}")
            
            # نمایش اطلاعات webhook
            webhook_info = bot.get_webhook_info()
            logger.info(f"📊 وضعیت webhook: {webhook_info.url}")
            
        except Exception as e:
            logger.error(f"❌ خطا در تنظیم webhook: {e}")
        
        # اجرای Flask
        port = int(os.environ.get('PORT', 5000))
        logger.info(f"🚀 ربات روی پورت {port} اجرا می‌شود")
        app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
        
    except Exception as e:
        logger.error(f"❌ خطای بحرانی در اجرا: {e}")
        sys.exit(1)
