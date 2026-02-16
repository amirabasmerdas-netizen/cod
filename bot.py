"""
ربات تلگرام ارسال خودکار تبلیغات - نسخه بهبود یافته
طراحی شده برای دیپلوی روی Render.com
"""

import os
import sys
import logging
import sqlite3
import json
import asyncio
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from contextlib import contextmanager

from flask import Flask, request, jsonify
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import requests

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

@contextmanager
def get_db():
    """مدیریت context دیتابیس"""
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
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
                    is_active BOOLEAN DEFAULT 1,
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
                    is_active BOOLEAN DEFAULT 1
                )
            ''')
            
            # جدول تنظیمات زمان‌بندی
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS schedule_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    interval_minutes INTEGER DEFAULT 5,
                    max_sends INTEGER DEFAULT 0,
                    current_sends INTEGER DEFAULT 0,
                    is_running BOOLEAN DEFAULT 0,
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
            
            conn.commit()
            logger.info("دیتابیس با موفقیت راه‌اندازی شد")
    except Exception as e:
        logger.error(f"خطا در راه‌اندازی دیتابیس: {e}")

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
    """دریافت chat_id از یوزرنیم گروه با مدیریت خطا"""
    try:
        # حذف @ از ابتدای یوزرنیم اگر وجود داشته باشد
        username = username.strip().lstrip('@')
        
        if not username:
            logger.warning("یوزرنیم خالی است")
            return None, None
        
        # تلاش برای دریافت اطلاعات گروه
        logger.info(f"در حال دریافت اطلاعات گروه @{username}")
        chat = bot.get_chat(f"@{username}")
        logger.info(f"اطلاعات گروه دریافت شد: ID={chat.id}, Title={chat.title}")
        return chat.id, chat.title
    except telebot.apihelper.ApiTelegramException as e:
        logger.error(f"خطای API تلگرام برای {username}: {e.result.status_code} - {e.result.text}")
        if e.result.status_code == 400:
            return None, None  # گروه یافت نشد
        elif e.result.status_code == 403:
            return None, None  # دسترسی نداریم
        else:
            return None, None
    except Exception as e:
        logger.error(f"خطای غیرمنتظره در دریافت chat_id برای {username}: {e}")
        return None, None

def check_bot_admin(chat_id):
    """بررسی اینکه ربات در گروه ادمین است"""
    try:
        bot_info = bot.get_me()
        bot_member = bot.get_chat_member(chat_id, bot_info.id)
        is_admin = bot_member.status in ['administrator', 'creator']
        logger.info(f"بررسی ادمین در گروه {chat_id}: {is_admin} (وضعیت: {bot_member.status})")
        return is_admin
    except telebot.apihelper.ApiTelegramException as e:
        logger.error(f"خطای API در بررسی ادمین گروه {chat_id}: {e.result.status_code}")
        if e.result.status_code == 400:
            return False  # گروه وجود ندارد
        elif e.result.status_code == 403:
            return False  # ربات در گروه نیست
        else:
            return False
    except Exception as e:
        logger.error(f"خطا در بررسی وضعیت ادمین در گروه {chat_id}: {e}")
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
    return keyboard

def save_advertisement(message_type, content=None, file_id=None):
    """ذخیره تبلیغ در دیتابیس"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO advertisements (message_type, content, file_id)
                VALUES (?, ?, ?)
            ''', (message_type, content, file_id))
            conn.commit()
            ad_id = cursor.lastrowid
            logger.info(f"تبلیغ جدید با ID {ad_id} ذخیره شد")
            return ad_id
    except Exception as e:
        logger.error(f"خطا در ذخیره تبلیغ: {e}")
        return None

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
        logger.error(f"خطا در دریافت تبلیغ فعال: {e}")
        return None

def get_all_groups():
    """دریافت لیست تمام گروه‌های فعال"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM groups WHERE is_active = 1')
            groups = cursor.fetchall()
            logger.info(f"تعداد گروه‌های فعال: {len(groups)}")
            return groups
    except Exception as e:
        logger.error(f"خطا در دریافت لیست گروه‌ها: {e}")
        return []

def remove_inactive_group(chat_id):
    """حذف گروه غیرفعال از دیتابیس"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE groups SET is_active = 0 WHERE chat_id = ?', (chat_id,))
            conn.commit()
            logger.info(f"گروه {chat_id} غیرفعال شد")
    except Exception as e:
        logger.error(f"خطا در غیرفعال کردن گروه {chat_id}: {e}")

def get_schedule_settings():
    """دریافت تنظیمات زمان‌بندی"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM schedule_settings LIMIT 1')
            return cursor.fetchone()
    except Exception as e:
        logger.error(f"خطا در دریافت تنظیمات زمان‌بندی: {e}")
        return None

def update_schedule_settings(interval=None, max_sends=None, is_running=None):
    """به‌روزرسانی تنظیمات زمان‌بندی"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            
            if interval is not None:
                cursor.execute('UPDATE schedule_settings SET interval_minutes = ?', (interval,))
            if max_sends is not None:
                cursor.execute('UPDATE schedule_settings SET max_sends = ?', (max_sends,))
            if is_running is not None:
                cursor.execute('UPDATE schedule_settings SET is_running = ?', (is_running,))
            
            cursor.execute('UPDATE schedule_settings SET updated_at = CURRENT_TIMESTAMP')
            conn.commit()
            logger.info("تنظیمات زمان‌بندی به‌روزرسانی شد")
    except Exception as e:
        logger.error(f"خطا در به‌روزرسانی تنظیمات: {e}")

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
            conn.commit()
    except Exception as e:
        logger.error(f"خطا در افزایش تعداد ارسال: {e}")

def reset_send_count():
    """ریست تعداد ارسال‌ها"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE schedule_settings SET current_sends = 0')
            conn.commit()
    except Exception as e:
        logger.error(f"خطا در ریست تعداد ارسال: {e}")

# ==================== مدیریت وضعیت‌های کاربر ====================
user_states = {}

def set_user_state(user_id, state, data=None):
    """تنظیم وضعیت کاربر"""
    user_states[user_id] = {'state': state, 'data': data or {}}
    logger.info(f"وضعیت کاربر {user_id} به {state} تغییر کرد")

def get_user_state(user_id):
    """دریافت وضعیت کاربر"""
    return user_states.get(user_id, {'state': None, 'data': {}})

def clear_user_state(user_id):
    """پاک کردن وضعیت کاربر"""
    if user_id in user_states:
        del user_states[user_id]
        logger.info(f"وضعیت کاربر {user_id} پاک شد")

# ==================== سیستم ارسال خودکار ====================
class AutoSendBot:
    """کلاس مدیریت ارسال خودکار"""
    
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.task = None
        self.is_running = False
        self.loop = None
        self.stop_event = threading.Event()
        
    def start(self):
        """شروع ارسال خودکار"""
        if self.is_running:
            logger.warning("ارسال خودکار از قبل در حال اجراست")
            return
        
        self.is_running = True
        self.stop_event.clear()
        self.loop = asyncio.new_event_loop()
        self.task = threading.Thread(target=self._run_loop, daemon=True)
        self.task.start()
        logger.info("ارسال خودکار شروع شد")
    
    def _run_loop(self):
        """اجرای حلقه asyncio در یک ترد جداگانه"""
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._auto_send_loop())
    
    def stop(self):
        """توقف ارسال خودکار"""
        self.is_running = False
        self.stop_event.set()
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        logger.info("ارسال خودکار متوقف شد")
    
    async def _auto_send_loop(self):
        """حلقه اصلی ارسال خودکار"""
        while self.is_running and not self.stop_event.is_set():
            try:
                settings = get_schedule_settings()
                
                if not settings:
                    logger.error("تنظیمات زمان‌بندی یافت نشد")
                    await asyncio.sleep(10)
                    continue
                
                if not settings['is_running']:
                    await asyncio.sleep(5)
                    continue
                
                # بررسی محدودیت تعداد ارسال
                if settings['max_sends'] > 0 and settings['current_sends'] >= settings['max_sends']:
                    update_schedule_settings(is_running=False)
                    logger.info("تعداد ارسال‌ها به حداکثر رسید، ارسال متوقف شد")
                    continue
                
                # دریافت تبلیغ فعال
                ad = get_active_advertisement()
                if not ad:
                    logger.warning("تبلیغ فعالی وجود ندارد")
                    await asyncio.sleep(settings['interval_minutes'] * 60)
                    continue
                
                # دریافت گروه‌های فعال
                groups = get_all_groups()
                if not groups:
                    logger.warning("گروه فعالی وجود ندارد")
                    await asyncio.sleep(settings['interval_minutes'] * 60)
                    continue
                
                # ارسال به تمام گروه‌ها
                for group in groups:
                    try:
                        chat_id = int(group['chat_id'])
                        
                        # بررسی ادمین بودن ربات
                        if not check_bot_admin(chat_id):
                            logger.warning(f"ربات در گروه {chat_id} ادمین نیست")
                            remove_inactive_group(chat_id)
                            continue
                        
                        # ارسال بر اساس نوع پیام
                        if ad['message_type'] == 'text':
                            self.bot.send_message(chat_id, ad['content'])
                            logger.info(f"متن به {chat_id} ارسال شد")
                        elif ad['message_type'] == 'photo':
                            self.bot.send_photo(chat_id, ad['file_id'], caption=ad['content'] or "")
                            logger.info(f"عکس به {chat_id} ارسال شد")
                        elif ad['message_type'] == 'video':
                            self.bot.send_video(chat_id, ad['file_id'], caption=ad['content'] or "")
                            logger.info(f"ویدیو به {chat_id} ارسال شد")
                        elif ad['message_type'] == 'document':
                            self.bot.send_document(chat_id, ad['file_id'], caption=ad['content'] or "")
                            logger.info(f"فایل به {chat_id} ارسال شد")
                        
                        # تاخیر برای جلوگیری از Flood
                        await asyncio.sleep(2)
                        
                    except telebot.apihelper.ApiTelegramException as e:
                        logger.error(f"خطای API در ارسال به گروه {group['chat_id']}: {e.result.status_code}")
                        if e.result.status_code in [400, 403, 404]:
                            remove_inactive_group(group['chat_id'])
                    except Exception as e:
                        logger.error(f"خطا در ارسال به گروه {group['chat_id']}: {e}")
                
                # افزایش تعداد ارسال‌ها
                increment_send_count()
                
                # انتظار تا نوبت بعدی
                logger.info(f"ارسال دوره‌ای کامل شد. بعدی در {settings['interval_minutes']} دقیقه")
                await asyncio.sleep(settings['interval_minutes'] * 60)
                
            except Exception as e:
                logger.error(f"خطا در حلقه ارسال خودکار: {e}")
                await asyncio.sleep(60)

# نمونه‌سازی از کلاس ارسال خودکار
auto_sender = AutoSendBot(bot)

# ==================== هندلرهای ربات ====================
@bot.message_handler(commands=['start'])
def start_command(message):
    """دستور شروع"""
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ شما اجازه استفاده از این ربات را ندارید.")
        return
    
    welcome_text = """
🤖 به ربات ارسال خودکار تبلیغات خوش آمدید!

از طریق منوی زیر می‌توانید ربات را مدیریت کنید:

📤 ثبت تبلیغ - ثبت تبلیغ جدید
👥 افزودن گروه - اضافه کردن گروه جدید
📋 لیست گروه‌ها - مشاهده گروه‌های فعال
⏱ تنظیم زمان ارسال - تنظیم فاصله و تعداد ارسال
▶️ شروع ارسال - شروع ارسال خودکار
⛔ توقف ارسال - توقف ارسال خودکار
    """
    
    bot.send_message(
        message.chat.id,
        welcome_text,
        reply_markup=get_main_keyboard()
    )

# ==================== هندلر ثبت تبلیغ ====================
@bot.message_handler(func=lambda message: message.text == "📤 ثبت تبلیغ")
@admin_only
def add_advertisement(message):
    """شروع فرآیند ثبت تبلیغ"""
    set_user_state(message.from_user.id, 'waiting_ad_type')
    
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        KeyboardButton("متن"),
        KeyboardButton("عکس"),
        KeyboardButton("ویدیو"),
        KeyboardButton("فایل")
    )
    keyboard.add(KeyboardButton("🔙 بازگشت"))
    
    bot.send_message(
        message.chat.id,
        "لطفاً نوع تبلیغ را انتخاب کنید:",
        reply_markup=keyboard
    )

@bot.message_handler(func=lambda message: get_user_state(message.from_user.id)['state'] == 'waiting_ad_type')
@admin_only
def process_ad_type(message):
    """پردازش نوع تبلیغ"""
    if message.text == "🔙 بازگشت":
        clear_user_state(message.from_user.id)
        bot.send_message(message.chat.id, "عملیات لغو شد.", reply_markup=get_main_keyboard())
        return
    
    ad_type_map = {
        "متن": "text",
        "عکس": "photo",
        "ویدیو": "video",
        "فایل": "document"
    }
    
    if message.text not in ad_type_map:
        bot.reply_to(message, "❌ لطفاً یک گزینه معتبر انتخاب کنید.")
        return
    
    set_user_state(
        message.from_user.id, 
        'waiting_ad_content', 
        {'type': ad_type_map[message.text]}
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
        if ad_type == 'text' and message.text:
            ad_id = save_advertisement('text', content=message.text)
            if ad_id:
                bot.send_message(message.chat.id, "✅ تبلیغ با موفقیت ثبت شد!", reply_markup=get_main_keyboard())
            else:
                bot.send_message(message.chat.id, "❌ خطا در ثبت تبلیغ!", reply_markup=get_main_keyboard())
        
        elif ad_type == 'photo' and message.photo:
            file_id = message.photo[-1].file_id
            caption = message.caption or ""
            ad_id = save_advertisement('photo', content=caption, file_id=file_id)
            if ad_id:
                bot.send_message(message.chat.id, "✅ عکس با موفقیت ثبت شد!", reply_markup=get_main_keyboard())
            else:
                bot.send_message(message.chat.id, "❌ خطا در ثبت عکس!", reply_markup=get_main_keyboard())
        
        elif ad_type == 'video' and message.video:
            file_id = message.video.file_id
            caption = message.caption or ""
            ad_id = save_advertisement('video', content=caption, file_id=file_id)
            if ad_id:
                bot.send_message(message.chat.id, "✅ ویدیو با موفقیت ثبت شد!", reply_markup=get_main_keyboard())
            else:
                bot.send_message(message.chat.id, "❌ خطا در ثبت ویدیو!", reply_markup=get_main_keyboard())
        
        elif ad_type == 'document' and message.document:
            file_id = message.document.file_id
            caption = message.caption or ""
            ad_id = save_advertisement('document', content=caption, file_id=file_id)
            if ad_id:
                bot.send_message(message.chat.id, "✅ فایل با موفقیت ثبت شد!", reply_markup=get_main_keyboard())
            else:
                bot.send_message(message.chat.id, "❌ خطا در ثبت فایل!", reply_markup=get_main_keyboard())
        
        else:
            bot.reply_to(message, "❌ نوع فایل ارسالی با انتخاب شما مطابقت ندارد. لطفاً دوباره تلاش کنید.")
            return
        
        clear_user_state(message.from_user.id)
        
    except Exception as e:
        logger.error(f"خطا در ثبت تبلیغ: {e}")
        bot.reply_to(message, "❌ خطایی در ثبت تبلیغ رخ داد. لطفاً دوباره تلاش کنید.")

# ==================== هندلر افزودن گروه (نسخه بهبود یافته) ====================
@bot.message_handler(func=lambda message: message.text == "👥 افزودن گروه")
@admin_only
def add_group(message):
    """شروع فرآیند افزودن گروه"""
    set_user_state(message.from_user.id, 'waiting_group_username')
    bot.send_message(
        message.chat.id,
        "👥 لطفاً یوزرنیم گروه را با @ وارد کنید:\nمثال: @mygroup"
    )

@bot.message_handler(func=lambda message: get_user_state(message.from_user.id)['state'] == 'waiting_group_username')
@admin_only
def process_group_username(message):
    """پردازش یوزرنیم گروه با دیباگ کامل"""
    username = message.text.strip()
    user_id = message.from_user.id
    
    try:
        # ارسال پیام وضعیت
        status_msg = bot.send_message(user_id, f"🔍 در حال بررسی گروه {username}...")
        
        # دریافت chat_id از یوزرنیم
        chat_id, title = get_chat_id_from_username(username)
        
        if not chat_id:
            bot.edit_message_text(
                chat_id=user_id,
                message_id=status_msg.message_id,
                text=f"❌ گروه {username} یافت نشد.\n\n"
                     "دلایل احتمالی:\n"
                     "1️⃣ یوزرنیم اشتباه است\n"
                     "2️⃣ گروه خصوصی است\n"
                     "3️⃣ ربات به گروه اضافه نشده"
            )
            return
        
        # به‌روزرسانی پیام وضعیت
        bot.edit_message_text(
            chat_id=user_id,
            message_id=status_msg.message_id,
            text=f"✅ گروه پیدا شد: {title}\n🔍 در حال بررسی دسترسی‌های ربات..."
        )
        
        # بررسی ادمین بودن ربات
        if not check_bot_admin(chat_id):
            bot.edit_message_text(
                chat_id=user_id,
                message_id=status_msg.message_id,
                text=f"❌ ربات در گروه {title} ادمین نیست.\n\n"
                     "مراحل زیر را انجام دهید:\n"
                     "1️⃣ به گروه بروید\n"
                     "2️⃣ روی نام ربات کلیک کنید\n"
                     "3️⃣ گزینه 'Add to Admin' را بزنید\n"
                     "4️⃣ دسترسی‌های لازم را بدهید\n"
                     "5️⃣ دوباره تلاش کنید"
            )
            return
        
        # به‌روزرسانی پیام وضعیت
        bot.edit_message_text(
            chat_id=user_id,
            message_id=status_msg.message_id,
            text="💾 در حال ذخیره اطلاعات در دیتابیس..."
        )
        
        # ذخیره در دیتابیس
        with get_db() as conn:
            cursor = conn.cursor()
            
            # بررسی وجود گروه
            cursor.execute('SELECT * FROM groups WHERE chat_id = ?', (chat_id,))
            existing = cursor.fetchone()
            
            if existing:
                cursor.execute('''
                    UPDATE groups 
                    SET username = ?, title = ?, is_active = 1 
                    WHERE chat_id = ?
                ''', (username, title, chat_id))
                action = "به‌روزرسانی"
            else:
                cursor.execute('''
                    INSERT INTO groups (chat_id, username, title, is_active)
                    VALUES (?, ?, ?, 1)
                ''', (chat_id, username, title))
                action = "ثبت"
            
            conn.commit()
            
            # تأیید ذخیره شدن
            cursor.execute('SELECT * FROM groups WHERE chat_id = ?', (chat_id,))
            if cursor.fetchone():
                bot.edit_message_text(
                    chat_id=user_id,
                    message_id=status_msg.message_id,
                    text=f"✅ اطلاعات گروه با موفقیت {action} شد!\n\n"
                         f"📌 عنوان: {title}\n"
                         f"🆔 آیدی: {chat_id}\n"
                         f"🌐 یوزرنیم: @{username.lstrip('@')}"
                )
            else:
                bot.edit_message_text(
                    chat_id=user_id,
                    message_id=status_msg.message_id,
                    text="❌ خطا: اطلاعات در دیتابیس ذخیره نشد!"
                )
            
    except telebot.apihelper.ApiTelegramException as e:
        logger.error(f"خطای API تلگرام: {e}")
        bot.send_message(user_id, f"❌ خطای تلگرام: {e.result.status_code} - {e.result.text}")
    except Exception as e:
        logger.error(f"خطای غیرمنتظره در پردازش گروه: {e}")
        bot.send_message(user_id, f"❌ خطای سیستمی: {str(e)}")
    finally:
        clear_user_state(user_id)
        # ارسال کیبورد اصلی
        bot.send_message(user_id, "بازگشت به منوی اصلی", reply_markup=get_main_keyboard())

# ==================== هندلر لیست گروه‌ها ====================
@bot.message_handler(func=lambda message: message.text == "📋 لیست گروه‌ها")
@admin_only
def list_groups(message):
    """نمایش لیست گروه‌ها"""
    groups = get_all_groups()
    
    if not groups:
        bot.send_message(message.chat.id, "📭 هیچ گروه فعالی وجود ندارد.\nبرای افزودن گروه از دکمه '👥 افزودن گروه' استفاده کنید.")
        return
    
    text = "📋 لیست گروه‌های فعال:\n\n"
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    for i, group in enumerate(groups, 1):
        text += f"{i}. {group['title']}\n"
        text += f"   🆔 آیدی: {group['chat_id']}\n"
        text += f"   🌐 یوزرنیم: {group['username']}\n\n"
        
        # دکمه حذف برای هر گروه
        keyboard.add(InlineKeyboardButton(
            f"❌ حذف {group['title']}",
            callback_data=f"delete_group_{group['chat_id']}"
        ))
    
    bot.send_message(message.chat.id, text, reply_markup=keyboard)

@bot.callback_query_handler(func=lambda call: call.data.startswith('delete_group_'))
def delete_group_callback(call):
    """حذف گروه از طریق دکمه"""
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ شما اجازه این کار را ندارید!")
        return
    
    chat_id = call.data.replace('delete_group_', '')
    
    try:
        remove_inactive_group(chat_id)
        bot.answer_callback_query(call.id, "✅ گروه با موفقیت حذف شد!")
        bot.edit_message_text(
            "✅ گروه از لیست حذف شد.",
            call.message.chat.id,
            call.message.message_id
        )
    except Exception as e:
        logger.error(f"خطا در حذف گروه: {e}")
        bot.answer_callback_query(call.id, "❌ خطا در حذف گروه!")

# ==================== هندلر تنظیم زمان ارسال ====================
@bot.message_handler(func=lambda message: message.text == "⏱ تنظیم زمان ارسال")
@admin_only
def schedule_settings_handler(message):
    """تنظیمات زمان‌بندی"""
    settings = get_schedule_settings()
    
    if not settings:
        bot.send_message(message.chat.id, "❌ خطا در دریافت تنظیمات!")
        return
    
    text = "⚙️ تنظیمات فعلی:\n\n"
    text += f"⏱ فاصله ارسال: {settings['interval_minutes']} دقیقه\n"
    text += f"📊 تعداد ارسال: "
    
    if settings['max_sends'] == 0:
        text += "نامحدود\n"
    else:
        text += f"{settings['current_sends']}/{settings['max_sends']}\n"
    
    text += f"▶️ وضعیت: {'فعال' if settings['is_running'] else 'غیرفعال'}\n\n"
    text += "لطفاً گزینه مورد نظر را انتخاب کنید:"
    
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(
        KeyboardButton("⏱ تنظیم فاصله"),
        KeyboardButton("📊 تنظیم تعداد")
    )
    keyboard.add(KeyboardButton("🔙 بازگشت"))
    
    bot.send_message(message.chat.id, text, reply_markup=keyboard)
    set_user_state(message.from_user.id, 'waiting_schedule_option')

@bot.message_handler(func=lambda message: get_user_state(message.from_user.id)['state'] == 'waiting_schedule_option')
@admin_only
def process_schedule_option(message):
    """پردازش گزینه تنظیم زمان"""
    if message.text == "🔙 بازگشت":
        clear_user_state(message.from_user.id)
        bot.send_message(message.chat.id, "بازگشت به منوی اصلی", reply_markup=get_main_keyboard())
        return
    
    if message.text == "⏱ تنظیم فاصله":
        set_user_state(message.from_user.id, 'waiting_interval')
        bot.send_message(message.chat.id, "⏱ لطفاً فاصله ارسال را به دقیقه وارد کنید (عدد صحیح، مثلاً 5):")
    
    elif message.text == "📊 تنظیم تعداد":
        set_user_state(message.from_user.id, 'waiting_max_sends')
        bot.send_message(message.chat.id, "📊 لطفاً تعداد دفعات ارسال را وارد کنید (0 برای نامحدود):")
    
    else:
        bot.reply_to(message, "❌ لطفاً یک گزینه معتبر انتخاب کنید.")

@bot.message_handler(func=lambda message: get_user_state(message.from_user.id)['state'] == 'waiting_interval')
@admin_only
def process_interval(message):
    """پردازش فاصله ارسال"""
    try:
        interval = int(message.text)
        if interval < 1:
            bot.reply_to(message, "❌ فاصله ارسال باید حداقل 1 دقیقه باشد.")
            return
        
        update_schedule_settings(interval=interval)
        bot.reply_to(message, f"✅ فاصله ارسال به {interval} دقیقه تنظیم شد.")
        clear_user_state(message.from_user.id)
        bot.send_message(message.chat.id, "بازگشت به منوی اصلی", reply_markup=get_main_keyboard())
        
    except ValueError:
        bot.reply_to(message, "❌ لطفاً یک عدد صحیح وارد کنید.")

@bot.message_handler(func=lambda message: get_user_state(message.from_user.id)['state'] == 'waiting_max_sends')
@admin_only
def process_max_sends(message):
    """پردازش تعداد ارسال"""
    try:
        max_sends = int(message.text)
        if max_sends < 0:
            bot.reply_to(message, "❌ تعداد ارسال نمی‌تواند منفی باشد.")
            return
        
        reset_send_count()
        update_schedule_settings(max_sends=max_sends)
        
        if max_sends == 0:
            bot.reply_to(message, "✅ تعداد ارسال به حالت نامحدود تنظیم شد.")
        else:
            bot.reply_to(message, f"✅ تعداد ارسال به {max_sends} بار تنظیم شد.")
        
        clear_user_state(message.from_user.id)
        bot.send_message(message.chat.id, "بازگشت به منوی اصلی", reply_markup=get_main_keyboard())
        
    except ValueError:
        bot.reply_to(message, "❌ لطفاً یک عدد صحیح وارد کنید.")

# ==================== هندلر شروع و توقف ارسال ====================
@bot.message_handler(func=lambda message: message.text == "▶️ شروع ارسال")
@admin_only
def start_sending(message):
    """شروع ارسال خودکار"""
    settings = get_schedule_settings()
    
    # بررسی وجود تبلیغ
    if not get_active_advertisement():
        bot.send_message(message.chat.id, "❌ ابتدا یک تبلیغ ثبت کنید.")
        return
    
    # بررسی وجود گروه
    if not get_all_groups():
        bot.send_message(message.chat.id, "❌ حداقل یک گروه اضافه کنید.")
        return
    
    if settings and settings['is_running']:
        bot.send_message(message.chat.id, "⚠️ ارسال خودکار در حال حاضر فعال است.")
        return
    
    update_schedule_settings(is_running=True)
    auto_sender.start()
    
    bot.send_message(
        message.chat.id,
        f"✅ ارسال خودکار شروع شد.\n\n"
        f"⏱ فاصله: {settings['interval_minutes']} دقیقه\n"
        f"📊 حداکثر ارسال: {'نامحدود' if settings['max_sends'] == 0 else settings['max_sends']}"
    )

@bot.message_handler(func=lambda message: message.text == "⛔ توقف ارسال")
@admin_only
def stop_sending(message):
    """توقف ارسال خودکار"""
    settings = get_schedule_settings()
    
    if not settings or not settings['is_running']:
        bot.send_message(message.chat.id, "⚠️ ارسال خودکار در حال حاضر غیرفعال است.")
        return
    
    update_schedule_settings(is_running=False)
    auto_sender.stop()
    
    bot.send_message(message.chat.id, "⛔ ارسال خودکار متوقف شد.")

# ==================== هندلر بازگشت ====================
@bot.message_handler(func=lambda message: message.text == "🔙 بازگشت")
def back_to_main(message):
    """بازگشت به منوی اصلی"""
    clear_user_state(message.from_user.id)
    bot.send_message(message.chat.id, "بازگشت به منوی اصلی", reply_markup=get_main_keyboard())

# ==================== هندلر پیش‌فرض ====================
@bot.message_handler(func=lambda message: True)
def default_handler(message):
    """هندلر پیش‌فرض برای پیام‌های ناشناخته"""
    if message.from_user.id == ADMIN_ID:
        bot.reply_to(
            message, 
            "❓ دستور نامشخص. لطفاً از دکمه‌های منو استفاده کنید.",
            reply_markup=get_main_keyboard()
        )

# ==================== Webhook و Flask ====================
@app.route('/webhook', methods=['POST'])
def webhook():
    """دریافت آپدیت‌های تلگرام"""
    if request.headers.get('content-type') == 'application/json':
        try:
            json_string = request.get_data().decode('utf-8')
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])
            logger.info(f"آپدیت دریافت شد: {update.update_id}")
            return jsonify({'status': 'ok'}), 200
        except Exception as e:
            logger.error(f"خطا در پردازش webhook: {e}")
            return jsonify({'status': 'error', 'message': str(e)}), 500
    return jsonify({'status': 'bad request'}), 400

@app.route('/')
def health_check():
    """بررسی سلامت ربات"""
    try:
        bot_info = bot.get_me()
        return jsonify({
            'status': 'running',
            'timestamp': datetime.now().isoformat(),
            'bot_info': {
                'username': bot_info.username,
                'id': bot_info.id
            },
            'webhook': WEBHOOK_URL
        }), 200
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/set_webhook', methods=['GET'])
def set_webhook_route():
    """تنظیم webhook"""
    try:
        webhook_url = f"{WEBHOOK_URL}/webhook"
        bot.remove_webhook()
        time.sleep(1)
        result = bot.set_webhook(url=webhook_url)
        
        if result:
            return jsonify({
                'status': 'success',
                'message': f'Webhook set to {webhook_url}'
            }), 200
        else:
            return jsonify({
                'status': 'error',
                'message': 'Failed to set webhook'
            }), 500
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
        return jsonify({
            'url': info.url,
            'has_custom_certificate': info.has_custom_certificate,
            'pending_update_count': info.pending_update_count,
            'max_connections': info.max_connections,
            'last_error_date': info.last_error_date,
            'last_error_message': info.last_error_message
        }), 200
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

# ==================== راه‌اندازی اولیه ====================
def setup_bot():
    """تنظیمات اولیه ربات"""
    try:
        # مقداردهی اولیه دیتابیس
        init_database()
        
        # بررسی توکن ربات
        bot_info = bot.get_me()
        logger.info(f"ربات با موفقیت راه‌اندازی شد: @{bot_info.username}")
        
        # تنظیم webhook
        webhook_url = f"{WEBHOOK_URL}/webhook"
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook تنظیم شد: {webhook_url}")
        
        # بررسی webhook
        webhook_info = bot.get_webhook_info()
        logger.info(f"اطلاعات webhook: {webhook_info.url}")
        
    except Exception as e:
        logger.error(f"خطا در راه‌اندازی اولیه: {e}")

# ==================== اجرای اصلی ====================
if __name__ == '__main__':
    # تنظیمات اولیه
    setup_bot()
    
    # اجرای سرور Flask
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
