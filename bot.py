"""
ربات تلگرام ارسال خودکار تبلیغات - نسخه Enterprise
طراحی شده برای دیپلوی روی Render.com با معماری مقاوم و بدون مشکل
"""

import os
import sys
import logging
import sqlite3
import json
import asyncio
import threading
import time
import queue
import signal
from datetime import datetime, timedelta
from functools import wraps
from contextlib import contextmanager
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, asdict
from enum import Enum
import hashlib
import hmac

from flask import Flask, request, jsonify, abort
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==================== تنظیمات پیشرفته ====================

# سطح‌بندی لاگ
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=getattr(logging, LOG_LEVEL)
)
logger = logging.getLogger(__name__)

# متغیرهای محیطی با اعتبارسنجی
BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN or len(BOT_TOKEN) < 40:
    logger.error("BOT_TOKEN معتبر نیست!")
    sys.exit(1)

WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
if not WEBHOOK_URL:
    logger.error("WEBHOOK_URL تنظیم نشده است!")
    sys.exit(1)

ADMIN_ID = os.environ.get('ADMIN_ID')
if not ADMIN_ID:
    logger.error("ADMIN_ID تنظیم نشده است!")
    sys.exit(1)

WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32])

try:
    ADMIN_ID = int(ADMIN_ID)
except ValueError:
    logger.error("ADMIN_ID باید عدد باشد!")
    sys.exit(1)

# ==================== راه‌اندازی سشن HTTP با Retry ====================

def create_requests_session():
    """ایجاد سشن HTTP با قابلیت Retry"""
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

# ==================== مدل‌های داده ====================

class MessageType(Enum):
    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    DOCUMENT = "document"

@dataclass
class Group:
    chat_id: int
    username: str
    title: str
    is_active: bool = True
    added_at: Optional[str] = None
    last_error: Optional[str] = None
    error_count: int = 0

@dataclass
class Advertisement:
    id: int
    message_type: MessageType
    content: Optional[str] = None
    file_id: Optional[str] = None
    created_at: Optional[str] = None
    is_active: bool = True

@dataclass
class ScheduleConfig:
    interval_minutes: int = 5
    max_sends: int = 0
    current_sends: int = 0
    is_running: bool = False
    last_send_time: Optional[str] = None
    updated_at: Optional[str] = None

# ==================== مدیریت دیتابیس با قفل ====================

class DatabaseManager:
    """مدیریت دیتابیس با قفل‌گذاری مناسب برای چند نخی"""
    
    _instance = None
    _lock = threading.RLock()
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialize()
            return cls._instance
    
    def _initialize(self):
        self.database = 'bot_data.db'
        self.connection_pool = queue.Queue(maxsize=10)
        self._init_pool()
        self._init_tables()
    
    def _init_pool(self):
        """ایجاد pool از کانکشن‌ها"""
        for _ in range(5):
            conn = sqlite3.connect(self.database, check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")  # حالت WAL برای همزمانی بهتر
            conn.execute("PRAGMA busy_timeout=5000")  # timeout 5 ثانیه
            self.connection_pool.put(conn)
    
    @contextmanager
    def get_connection(self):
        """دریافت کانکشن از pool با context manager"""
        conn = self.connection_pool.get()
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"خطای دیتابیس: {e}")
            raise
        finally:
            self.connection_pool.put(conn)
    
    def _init_tables(self):
        """ایجاد جداول با ساختار بهینه"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # جدول گروه‌ها با ایندکس
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    title TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    error_count INTEGER DEFAULT 0,
                    last_error TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_groups_chat_id ON groups(chat_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_groups_is_active ON groups(is_active)')
            
            # جدول تبلیغات
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS advertisements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_type TEXT NOT NULL,
                    content TEXT,
                    file_id TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_ads_is_active ON advertisements(is_active)')
            
            # جدول تنظیمات
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS schedule_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    interval_minutes INTEGER DEFAULT 5,
                    max_sends INTEGER DEFAULT 0,
                    current_sends INTEGER DEFAULT 0,
                    is_running BOOLEAN DEFAULT 0,
                    last_send_time TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # درج تنظیمات پیش‌فرض
            cursor.execute('INSERT OR IGNORE INTO schedule_settings (id) VALUES (1)')
            
            # جدول لاگ خطاها
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS error_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    error_type TEXT,
                    error_message TEXT,
                    chat_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
    
    def log_error(self, error_type: str, error_message: str, chat_id: Optional[int] = None):
        """ثبت خطا در دیتابیس"""
        try:
            with self.get_connection() as conn:
                conn.execute(
                    'INSERT INTO error_logs (error_type, error_message, chat_id) VALUES (?, ?, ?)',
                    (error_type, error_message, chat_id)
                )
        except Exception as e:
            logger.error(f"خطا در ثبت لاگ: {e}")

# ==================== مدیریت وضعیت کاربر با Redis-like کش ====================

class UserStateManager:
    """مدیریت وضعیت کاربر با timeout و قفل"""
    
    def __init__(self, timeout_seconds: int = 300):
        self.states: Dict[int, Dict[str, Any]] = {}
        self.timeouts: Dict[int, float] = {}
        self.timeout_seconds = timeout_seconds
        self._lock = threading.RLock()
        
        # Thread پاکسازی خودکار
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()
    
    def set(self, user_id: int, state: str, data: Optional[Dict] = None):
        """تنظیم وضعیت کاربر با timeout"""
        with self._lock:
            self.states[user_id] = {
                'state': state,
                'data': data or {},
                'created_at': time.time()
            }
            self.timeouts[user_id] = time.time() + self.timeout_seconds
            logger.debug(f"وضعیت کاربر {user_id} به {state} تغییر کرد")
    
    def get(self, user_id: int) -> Dict[str, Any]:
        """دریافت وضعیت کاربر"""
        with self._lock:
            self._cleanup_user(user_id)
            return self.states.get(user_id, {'state': None, 'data': {}})
    
    def clear(self, user_id: int):
        """پاک کردن وضعیت کاربر"""
        with self._lock:
            self.states.pop(user_id, None)
            self.timeouts.pop(user_id, None)
            logger.debug(f"وضعیت کاربر {user_id} پاک شد")
    
    def _cleanup_user(self, user_id: int):
        """پاک کردن کاربر اگر timeout شده باشد"""
        if user_id in self.timeouts and time.time() > self.timeouts[user_id]:
            self.states.pop(user_id, None)
            self.timeouts.pop(user_id, None)
    
    def _cleanup_loop(self):
        """حلقه پاکسازی خودکار"""
        while True:
            time.sleep(60)  # هر دقیقه
            with self._lock:
                now = time.time()
                expired = [uid for uid, expiry in self.timeouts.items() if now > expiry]
                for uid in expired:
                    self.states.pop(uid, None)
                    self.timeouts.pop(uid, None)
                    logger.debug(f"وضعیت کاربر {uid} به دلیل timeout پاک شد")

# ==================== مدیریت صف ارسال پیام ====================

class MessageQueue:
    """مدیریت صف ارسال پیام با Rate Limiting"""
    
    def __init__(self, bot_instance, max_per_second: int = 20):
        self.bot = bot_instance
        self.queue = queue.Queue()
        self.max_per_second = max_per_second
        self.is_running = False
        self.thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        
    def start(self):
        """شروع پردازشگر صف"""
        with self._lock:
            if not self.is_running:
                self.is_running = True
                self._stop_event.clear()
                self.thread = threading.Thread(target=self._processor, daemon=True)
                self.thread.start()
                logger.info("صف ارسال پیام شروع به کار کرد")
    
    def stop(self):
        """توقف پردازشگر صف"""
        with self._lock:
            self.is_running = False
            self._stop_event.set()
            if self.thread:
                self.thread.join(timeout=5)
            logger.info("صف ارسال پیام متوقف شد")
    
    def add_message(self, chat_id: int, message_type: MessageType, content: str = None, file_id: str = None):
        """افزودن پیام به صف"""
        self.queue.put({
            'chat_id': chat_id,
            'type': message_type,
            'content': content,
            'file_id': file_id,
            'created_at': time.time()
        })
        logger.debug(f"پیام به صف اضافه شد برای {chat_id}")
    
    def _processor(self):
        """پردازشگر اصلی صف با Rate Limiting"""
        last_send_time = 0
        
        while self.is_running and not self._stop_event.is_set():
            try:
                # کنترل نرخ ارسال
                now = time.time()
                if now - last_send_time < 1.0 / self.max_per_second:
                    time.sleep(0.05)
                    continue
                
                # دریافت پیام از صف با timeout
                try:
                    message = self.queue.get(timeout=1)
                except queue.Empty:
                    continue
                
                # ارسال پیام
                try:
                    self._send_message(message)
                    last_send_time = time.time()
                except Exception as e:
                    logger.error(f"خطا در ارسال پیام از صف: {e}")
                    # برگرداندن به صف برای تلاش مجدد
                    if message.get('retry_count', 0) < 3:
                        message['retry_count'] = message.get('retry_count', 0) + 1
                        time.sleep(2 ** message['retry_count'])  # exponential backoff
                        self.queue.put(message)
                
                self.queue.task_done()
                
            except Exception as e:
                logger.error(f"خطا در پردازشگر صف: {e}")
                time.sleep(1)
    
    def _send_message(self, message: Dict):
        """ارسال واقعی پیام"""
        chat_id = message['chat_id']
        msg_type = message['type']
        content = message.get('content')
        file_id = message.get('file_id')
        
        try:
            if msg_type == MessageType.TEXT:
                self.bot.send_message(chat_id, content)
            elif msg_type == MessageType.PHOTO:
                self.bot.send_photo(chat_id, file_id, caption=content)
            elif msg_type == MessageType.VIDEO:
                self.bot.send_video(chat_id, file_id, caption=content)
            elif msg_type == MessageType.DOCUMENT:
                self.bot.send_document(chat_id, file_id, caption=content)
        except telebot.apihelper.ApiTelegramException as e:
            if e.result.status_code == 429:  # Too Many Requests
                retry_after = int(e.result.json().get('parameters', {}).get('retry_after', 5))
                logger.warning(f"Rate limited برای {chat_id}. توقف {retry_after} ثانیه")
                time.sleep(retry_after)
                raise  # برای تلاش مجدد
            elif e.result.status_code in [400, 403, 404]:
                # خطای دائمی، گروه را غیرفعال کن
                db = DatabaseManager()
                with db.get_connection() as conn:
                    conn.execute(
                        'UPDATE groups SET is_active = 0, last_error = ? WHERE chat_id = ?',
                        (str(e), chat_id)
                    )
                logger.error(f"گروه {chat_id} غیرفعال شد: {e}")
            else:
                raise

# ==================== سیستم ارسال خودکار با مدیریت خطا ====================

class AutoScheduler:
    """مدیریت زمان‌بندی ارسال خودکار با قابلیت Resume"""
    
    def __init__(self, bot_instance, message_queue: MessageQueue):
        self.bot = bot_instance
        self.queue = message_queue
        self._running = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self.db = DatabaseManager()
        
    def start(self):
        """شروع زمان‌بندی"""
        with self._lock:
            if self._running:
                logger.warning("زمان‌بندی از قبل در حال اجراست")
                return
            
            self._running = True
            self._paused = False
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._scheduler_loop, daemon=True)
            self._thread.start()
            
            # به‌روزرسانی وضعیت در دیتابیس
            with self.db.get_connection() as conn:
                conn.execute(
                    'UPDATE schedule_settings SET is_running = 1 WHERE id = 1'
                )
            
            logger.info("زمان‌بندی خودکار شروع شد")
    
    def stop(self):
        """توقف زمان‌بندی"""
        with self._lock:
            self._running = False
            self._stop_event.set()
            
            # به‌روزرسانی وضعیت در دیتابیس
            with self.db.get_connection() as conn:
                conn.execute(
                    'UPDATE schedule_settings SET is_running = 0 WHERE id = 1'
                )
            
            logger.info("زمان‌بندی خودکار متوقف شد")
    
    def pause(self):
        """توقف موقت"""
        with self._lock:
            self._paused = True
            logger.info("زمان‌بندی متوقف شد")
    
    def resume(self):
        """ادامه بعد از توقف"""
        with self._lock:
            self._paused = False
            logger.info("زمان‌بندی ادامه یافت")
    
    def _scheduler_loop(self):
        """حلقه اصلی زمان‌بندی"""
        while self._running and not self._stop_event.is_set():
            try:
                if self._paused:
                    time.sleep(1)
                    continue
                
                # دریافت تنظیمات فعلی
                with self.db.get_connection() as conn:
                    settings = conn.execute(
                        'SELECT * FROM schedule_settings WHERE id = 1'
                    ).fetchone()
                
                if not settings or not settings['is_running']:
                    time.sleep(1)
                    continue
                
                # بررسی محدودیت تعداد
                if settings['max_sends'] > 0 and settings['current_sends'] >= settings['max_sends']:
                    logger.info("به حداکثر تعداد ارسال رسیدیم")
                    self.stop()
                    continue
                
                # دریافت تبلیغ فعال
                ad = conn.execute(
                    'SELECT * FROM advertisements WHERE is_active = 1 ORDER BY created_at DESC LIMIT 1'
                ).fetchone()
                
                if not ad:
                    logger.warning("تبلیغ فعالی وجود ندارد")
                    time.sleep(60)
                    continue
                
                # دریافت گروه‌های فعال
                groups = conn.execute(
                    'SELECT * FROM groups WHERE is_active = 1'
                ).fetchall()
                
                if not groups:
                    logger.warning("گروه فعالی وجود ندارد")
                    time.sleep(60)
                    continue
                
                # ارسال به گروه‌ها
                for group in groups:
                    if self._stop_event.is_set():
                        break
                    
                    try:
                        # بررسی ادمین بودن ربات
                        bot_member = self.bot.get_chat_member(
                            group['chat_id'], 
                            self.bot.get_me().id
                        )
                        
                        if bot_member.status not in ['administrator', 'creator']:
                            logger.warning(f"ربات در گروه {group['chat_id']} ادمین نیست")
                            with self.db.get_connection() as conn2:
                                conn2.execute(
                                    'UPDATE groups SET is_active = 0 WHERE chat_id = ?',
                                    (group['chat_id'],)
                                )
                            continue
                        
                        # افزودن به صف ارسال
                        self.queue.add_message(
                            chat_id=group['chat_id'],
                            message_type=MessageType(ad['message_type']),
                            content=ad['content'],
                            file_id=ad['file_id']
                        )
                        
                    except Exception as e:
                        logger.error(f"خطا در پردازش گروه {group['chat_id']}: {e}")
                        self.db.log_error('group_process_error', str(e), group['chat_id'])
                        
                        # افزایش count خطا
                        with self.db.get_connection() as conn2:
                            conn2.execute(
                                '''UPDATE groups 
                                   SET error_count = error_count + 1,
                                       last_error = ?,
                                       is_active = CASE WHEN error_count >= 5 THEN 0 ELSE is_active END
                                   WHERE chat_id = ?''',
                                (str(e)[:200], group['chat_id'])
                            )
                
                # افزایش تعداد ارسال
                with self.db.get_connection() as conn:
                    conn.execute(
                        '''UPDATE schedule_settings 
                           SET current_sends = current_sends + 1,
                               last_send_time = CURRENT_TIMESTAMP
                           WHERE id = 1'''
                    )
                
                # انتظار تا نوبت بعدی
                logger.info(f"دوره ارسال کامل شد. بعدی در {settings['interval_minutes']} دقیقه")
                
                # انتظار هوشمند با قابلیت توقف
                for _ in range(settings['interval_minutes'] * 60):
                    if self._stop_event.is_set():
                        break
                    time.sleep(1)
                
            except Exception as e:
                logger.error(f"خطای بحرانی در زمان‌بندی: {e}")
                self.db.log_error('scheduler_critical', str(e))
                time.sleep(60)

# ==================== نمونه‌سازی کلاس‌های اصلی ====================

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
db_manager = DatabaseManager()
user_states = UserStateManager(timeout_seconds=300)
message_queue = MessageQueue(bot, max_per_second=20)
scheduler = AutoScheduler(bot, message_queue)

app = Flask(__name__)

# ==================== اعتبارسنجی Webhook ====================

def verify_webhook_signature(request):
    """بررسی امضای webhook برای امنیت بیشتر"""
    signature = request.headers.get('X-Telegram-Bot-Api-Secret-Token')
    if not signature:
        logger.warning("درخواست webhook بدون امضا")
        return False
    
    # مقایسه امن برای جلوگیری از timing attack
    return hmac.compare_digest(signature, WEBHOOK_SECRET)

# ==================== دکوریتور ادمین ====================

def admin_only(func):
    @wraps(func)
    def wrapper(message):
        if message.from_user.id != ADMIN_ID:
            bot.reply_to(message, "⛔ شما اجازه استفاده از این دستور را ندارید.")
            return
        return func(message)
    return wrapper

# ==================== کیبورد اصلی ====================

def get_main_keyboard():
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
    keyboard.add(KeyboardButton("📊 وضعیت سیستم"))
    return keyboard

# ==================== هندلر شروع ====================

@bot.message_handler(commands=['start'])
def start_command(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ شما اجازه استفاده از این ربات را ندارید.")
        return
    
    welcome_text = """
🤖 ربات ارسال خودکار تبلیغات - نسخه Enterprise

✅ مدیریت هوشمند خطاها
✅ صف ارسال با Rate Limiting
✅ پایداری بالا در Render
✅ Resume خودکار بعد از ریست

از منوی زیر استفاده کنید:
    """
    
    bot.send_message(
        message.chat.id,
        welcome_text,
        reply_markup=get_main_keyboard()
    )

# ==================== هندلر وضعیت سیستم ====================

@bot.message_handler(func=lambda message: message.text == "📊 وضعیت سیستم")
@admin_only
def system_status(message):
    with db_manager.get_connection() as conn:
        groups_count = conn.execute('SELECT COUNT(*) as count FROM groups WHERE is_active = 1').fetchone()['count']
        ads_count = conn.execute('SELECT COUNT(*) as count FROM advertisements WHERE is_active = 1').fetchone()['count']
        settings = conn.execute('SELECT * FROM schedule_settings WHERE id = 1').fetchone()
        errors_today = conn.execute(
            'SELECT COUNT(*) as count FROM error_logs WHERE date(created_at) = date("now")'
        ).fetchone()['count']
    
    status_text = f"""
📊 وضعیت سیستم:

👥 گروه‌های فعال: {groups_count}
📝 تبلیغات فعال: {ads_count}
⚙️ وضعیت ارسال: {'✅ فعال' if settings['is_running'] else '⛔ غیرفعال'}
⏱ فاصله ارسال: {settings['interval_minutes']} دقیقه
📊 تعداد ارسال: {settings['current_sends']}/{settings['max_sends'] if settings['max_sends'] > 0 else '∞'}
⚠️ خطاهای امروز: {errors_today}
📦 صف ارسال: {message_queue.queue.qsize()} پیام
🔄 وضعیت زمان‌بندی: {'▶️ فعال' if scheduler._running else '⏸️ متوقف'}
    """
    
    bot.send_message(message.chat.id, status_text)

# ==================== هندلر افزودن گروه (نسخه نهایی) ====================

@bot.message_handler(func=lambda message: message.text == "👥 افزودن گروه")
@admin_only
def add_group(message):
    user_states.set(message.from_user.id, 'waiting_group_username')
    bot.send_message(
        message.chat.id,
        "👥 لطفاً یوزرنیم گروه را با @ وارد کنید:\nمثال: @mygroup\n\n"
        "⚠️ نکته: ربات باید در گروه ادمین باشد."
    )

@bot.message_handler(func=lambda message: user_states.get(message.from_user.id)['state'] == 'waiting_group_username')
@admin_only
def process_group_username(message):
    username = message.text.strip()
    user_id = message.from_user.id
    
    # اعتبارسنجی یوزرنیم
    if not username.startswith('@') or len(username) < 2:
        bot.reply_to(message, "❌ فرمت یوزرنیم نامعتبر است. باید با @ شروع شود.")
        return
    
    status_msg = bot.send_message(user_id, f"🔍 در حال بررسی گروه {username}...")
    
    try:
        # دریافت اطلاعات گروه با timeout
        chat = bot.get_chat(username)
        
        if not chat:
            bot.edit_message_text(
                f"❌ گروه {username} یافت نشد.",
                user_id,
                status_msg.message_id
            )
            return
        
        # بررسی ادمین بودن ربات
        bot_member = bot.get_chat_member(chat.id, bot.get_me().id)
        
        if bot_member.status not in ['administrator', 'creator']:
            bot.edit_message_text(
                f"❌ ربات در گروه {chat.title} ادمین نیست.\n\n"
                "لطفاً مراحل زیر را انجام دهید:\n"
                "1️⃣ به گروه بروید\n"
                "2️⃣ روی نام ربات کلیک کنید\n"
                "3️⃣ گزینه 'Add to Admin' را بزنید\n"
                "4️⃣ دسترسی‌های لازم را بدهید",
                user_id,
                status_msg.message_id
            )
            return
        
        # ذخیره در دیتابیس
        with db_manager.get_connection() as conn:
            conn.execute('''
                INSERT INTO groups (chat_id, username, title, is_active)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(chat_id) DO UPDATE SET
                    username = excluded.username,
                    title = excluded.title,
                    is_active = 1,
                    error_count = 0,
                    updated_at = CURRENT_TIMESTAMP
            ''', (chat.id, username, chat.title))
        
        bot.edit_message_text(
            f"✅ گروه با موفقیت اضافه شد!\n\n"
            f"📌 عنوان: {chat.title}\n"
            f"🆔 آیدی: {chat.id}\n"
            f"🌐 یوزرنیم: {username}",
            user_id,
            status_msg.message_id
        )
        
    except telebot.apihelper.ApiTelegramException as e:
        error_msg = f"❌ خطای تلگرام: "
        if e.result.status_code == 400:
            error_msg += "گروه یافت نشد یا یوزرنیم اشتباه است."
        elif e.result.status_code == 403:
            error_msg += "دسترسی به گروه وجود ندارد."
        else:
            error_msg += str(e)
        
        bot.edit_message_text(error_msg, user_id, status_msg.message_id)
        db_manager.log_error('telegram_api_error', str(e), None)
        
    except Exception as e:
        logger.error(f"خطای غیرمنتظره: {e}")
        bot.edit_message_text(
            "❌ خطای سیستمی رخ داد. لطفاً دوباره تلاش کنید.",
            user_id,
            status_msg.message_id
        )
        db_manager.log_error('unexpected_error', str(e), None)
    
    finally:
        user_states.clear(user_id)
        bot.send_message(user_id, "بازگشت به منوی اصلی", reply_markup=get_main_keyboard())

# ==================== هندلر شروع ارسال ====================

@bot.message_handler(func=lambda message: message.text == "▶️ شروع ارسال")
@admin_only
def start_sending(message):
    with db_manager.get_connection() as conn:
        # بررسی وجود تبلیغ
        ad = conn.execute(
            'SELECT * FROM advertisements WHERE is_active = 1'
        ).fetchone()
        
        if not ad:
            bot.send_message(message.chat.id, "❌ ابتدا یک تبلیغ ثبت کنید.")
            return
        
        # بررسی وجود گروه
        groups = conn.execute(
            'SELECT COUNT(*) as count FROM groups WHERE is_active = 1'
        ).fetchone()
        
        if groups['count'] == 0:
            bot.send_message(message.chat.id, "❌ حداقل یک گروه اضافه کنید.")
            return
        
        # دریافت تنظیمات
        settings = conn.execute(
            'SELECT * FROM schedule_settings WHERE id = 1'
        ).fetchone()
    
    if settings['is_running']:
        bot.send_message(message.chat.id, "⚠️ ارسال خودکار در حال حاضر فعال است.")
        return
    
    # شروع سرویس‌ها
    message_queue.start()
    scheduler.start()
    
    bot.send_message(
        message.chat.id,
        f"✅ ارسال خودکار شروع شد.\n\n"
        f"⏱ فاصله: {settings['interval_minutes']} دقیقه\n"
        f"📊 حداکثر ارسال: {'نامحدود' if settings['max_sends'] == 0 else settings['max_sends']}"
    )

# ==================== هندلر توقف ارسال ====================

@bot.message_handler(func=lambda message: message.text == "⛔ توقف ارسال")
@admin_only
def stop_sending(message):
    with db_manager.get_connection() as conn:
        settings = conn.execute(
            'SELECT is_running FROM schedule_settings WHERE id = 1'
        ).fetchone()
    
    if not settings or not settings['is_running']:
        bot.send_message(message.chat.id, "⚠️ ارسال خودکار در حال حاضر غیرفعال است.")
        return
    
    scheduler.stop()
    message_queue.stop()
    
    bot.send_message(message.chat.id, "⛔ ارسال خودکار متوقف شد.")

# ==================== Webhook با امنیت ====================

@app.route('/webhook', methods=['POST'])
def webhook():
    """دریافت آپدیت با验证 امضا"""
    # بررسی امضا برای امنیت
    if not verify_webhook_signature(request):
        logger.warning("درخواست webhook با امضای نامعتبر")
        abort(403)
    
    if request.headers.get('content-type') == 'application/json':
        try:
            json_string = request.get_data().decode('utf-8')
            update = telebot.types.Update.de_json(json_string)
            
            # پردازش در Thread جداگانه برای جلوگیری از blocking
            def process():
                try:
                    bot.process_new_updates([update])
                except Exception as e:
                    logger.error(f"خطا در پردازش آپدیت: {e}")
                    db_manager.log_error('webhook_processing', str(e), 
                                        update.message.chat.id if update.message else None)
            
            threading.Thread(target=process, daemon=True).start()
            
            return jsonify({'status': 'ok'}), 200
        except Exception as e:
            logger.error(f"خطا در پردازش webhook: {e}")
            db_manager.log_error('webhook_error', str(e))
            return jsonify({'status': 'error'}), 500
    
    return jsonify({'status': 'bad request'}), 400

@app.route('/')
def health_check():
    """بررسی سلامت با اطلاعات کامل"""
    try:
        bot_info = bot.get_me()
        with db_manager.get_connection() as conn:
            groups_count = conn.execute('SELECT COUNT(*) as count FROM groups WHERE is_active = 1').fetchone()['count']
            ads_count = conn.execute('SELECT COUNT(*) as count FROM advertisements WHERE is_active = 1').fetchone()['count']
            settings = conn.execute('SELECT is_running FROM schedule_settings WHERE id = 1').fetchone()
        
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'bot': {
                'username': bot_info.username,
                'id': bot_info.id
            },
            'stats': {
                'active_groups': groups_count,
                'active_ads': ads_count,
                'scheduler_running': bool(settings['is_running']) if settings else False,
                'queue_size': message_queue.queue.qsize()
            },
            'webhook': {
                'url': f"{WEBHOOK_URL}/webhook",
                'has_secret': True
            }
        }), 200
    except Exception as e:
        logger.error(f"خطا در health check: {e}")
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)
        }), 500

@app.route('/set_webhook', methods=['GET'])
def set_webhook_route():
    """تنظیم webhook با secret token"""
    try:
        webhook_url = f"{WEBHOOK_URL}/webhook"
        bot.remove_webhook()
        time.sleep(1)
        
        # تنظیم webhook با secret token
        result = bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            max_connections=40,
            allowed_updates=['message', 'callback_query']
        )
        
        if result:
            return jsonify({
                'status': 'success',
                'message': f'Webhook set to {webhook_url}',
                'secret_token': WEBHOOK_SECRET[:5] + '...'  # فقط بخشی از token
            }), 200
        else:
            return jsonify({'status': 'error', 'message': 'Failed to set webhook'}), 500
    except Exception as e:
        logger.error(f"خطا در تنظیم webhook: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

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
            'last_error_message': info.last_error_message,
            'last_success_date': info.last_success_date,
            'allowed_updates': info.allowed_updates
        }), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ==================== راه‌اندازی اولیه ====================

def setup():
    """تنظیمات اولیه با قابلیت Resume"""
    try:
        logger.info("در حال راه‌اندازی ربات...")
        
        # بررسی اتصال به تلگرام
        bot_info = bot.get_me()
        logger.info(f"اتصال به تلگرام برقرار شد: @{bot_info.username}")
        
        # تنظیم webhook
        webhook_url = f"{WEBHOOK_URL}/webhook"
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            max_connections=40
        )
        logger.info(f"Webhook تنظیم شد: {webhook_url}")
        
        # Resume وضعیت قبلی
        with db_manager.get_connection() as conn:
            settings = conn.execute(
                'SELECT is_running FROM schedule_settings WHERE id = 1'
            ).fetchone()
            
            if settings and settings['is_running']:
                logger.info("Resume وضعیت ارسال خودکار از جلسه قبل")
                message_queue.start()
                scheduler.start()
        
        logger.info("راه‌اندازی با موفقیت انجام شد")
        
    except Exception as e:
        logger.error(f"خطا در راه‌اندازی: {e}")
        sys.exit(1)

# ==================== مدیریت سیگنال‌ها برای خروج تمیز ====================

def signal_handler(signum, frame):
    """مدیریت سیگنال‌ها برای خروج تمیز"""
    logger.info(f"سیگنال {signum} دریافت شد. در حال خروج تمیز...")
    scheduler.stop()
    message_queue.stop()
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# ==================== اجرای اصلی ====================

if __name__ == '__main__':
    # راه‌اندازی اولیه
    setup()
    
    # اجرای سرور
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
