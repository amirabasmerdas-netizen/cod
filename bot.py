"""
ربات تلگرام ارسال خودکار تبلیغات - نسخه نهایی
طراحی شده با رویکرد Async First و معماری مقاوم
"""

import os
import sys
import logging
import asyncio
import sqlite3
import json
import time
import signal
from datetime import datetime, timedelta
from functools import wraps
from contextlib import contextmanager, asynccontextmanager
from typing import Optional, Dict, List, Any, Set
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import hmac
from collections import OrderedDict
import weakref

from flask import Flask, request, jsonify, abort
import aiohttp
import asyncio
from aiohttp import ClientTimeout, TCPConnector
import telebot
from telebot.types import Update, ReplyKeyboardMarkup, KeyboardButton
import aiosqlite

# ==================== تنظیمات پیشرفته ====================

LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=getattr(logging, LOG_LEVEL)
)
logger = logging.getLogger(__name__)

# اعتبارسنجی متغیرهای محیطی
BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN or len(BOT_TOKEN) < 40:
    logger.error("BOT_TOKEN معتبر نیست!")
    sys.exit(1)

WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
if not WEBHOOK_URL:
    logger.error("WEBHOOK_URL تنظیم نشده است!")
    sys.exit(1)

try:
    ADMIN_ID = int(os.environ.get('ADMIN_ID', '0'))
    if ADMIN_ID == 0:
        raise ValueError
except ValueError:
    logger.error("ADMIN_ID باید یک عدد صحیح باشد!")
    sys.exit(1)

WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32])

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
    error_count: int = 0
    last_error: Optional[str] = None
    last_check: Optional[float] = None
    
    def should_check_admin(self) -> bool:
        """بررسی اینکه آیا باید دوباره ادمین بودن چک شود"""
        if not self.last_check:
            return True
        return time.time() - self.last_check > 3600  # هر 1 ساعت

@dataclass
class Advertisement:
    id: int
    message_type: MessageType
    content: Optional[str] = None
    file_id: Optional[str] = None
    is_active: bool = True

@dataclass
class ScheduleConfig:
    interval_minutes: int = 5
    max_sends: int = 0
    current_sends: int = 0
    is_running: bool = False
    last_send_time: Optional[datetime] = None

# ==================== دیتابیس Async با connection pool ====================

class AsyncDatabasePool:
    """Pool مدیریت اتصالات دیتابیس Async"""
    
    def __init__(self, db_path: str, max_connections: int = 5):
        self.db_path = db_path
        self.max_connections = max_connections
        self._pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
        self._initialized = False
        self._lock = asyncio.Lock()
    
    async def initialize(self):
        """ایجاد pool"""
        if self._initialized:
            return
        
        async with self._lock:
            if self._initialized:
                return
            
            for i in range(self.max_connections):
                conn = await aiosqlite.connect(
                    self.db_path,
                    timeout=30,
                    check_same_thread=False
                )
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA synchronous=NORMAL")
                await conn.execute("PRAGMA cache_size=-2000")  # 2MB cache
                conn.row_factory = aiosqlite.Row
                await self._pool.put(conn)
            
            self._initialized = True
            logger.info(f"پول دیتابیس با {self.max_connections} کانکشن راه‌اندازی شد")
    
    @asynccontextmanager
    async def acquire(self):
        """دریافت کانکشن از pool"""
        if not self._initialized:
            await self.initialize()
        
        conn = await self._pool.get()
        try:
            yield conn
        finally:
            await self._pool.put(conn)
    
    async def close_all(self):
        """بستن همه کانکشن‌ها"""
        while not self._pool.empty():
            conn = await self._pool.get()
            await conn.close()

# ==================== مدل‌های دیتابیس ====================

class DatabaseModels:
    """مدیریت کوئری‌های دیتابیس"""
    
    def __init__(self, pool: AsyncDatabasePool):
        self.pool = pool
    
    async def init_tables(self):
        """ایجاد جداول با ساختار بهینه"""
        async with self.pool.acquire() as conn:
            # جدول گروه‌ها
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS groups (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL,
                    title TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1,
                    error_count INTEGER DEFAULT 0,
                    last_error TEXT,
                    last_check REAL,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # ایندکس‌ها
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_groups_active ON groups(is_active)')
            
            # جدول تبلیغات
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS advertisements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_type TEXT NOT NULL,
                    content TEXT,
                    file_id TEXT,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # جدول تنظیمات
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS schedule_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    interval_minutes INTEGER DEFAULT 5,
                    max_sends INTEGER DEFAULT 0,
                    current_sends INTEGER DEFAULT 0,
                    is_running INTEGER DEFAULT 0,
                    last_send_time TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # درج تنظیمات پیش‌فرض
            await conn.execute('''
                INSERT OR IGNORE INTO schedule_settings (id, interval_minutes, max_sends, is_running)
                VALUES (1, 5, 0, 0)
            ''')
            
            await conn.commit()
    
    async def get_active_groups(self) -> List[Group]:
        """دریافت گروه‌های فعال"""
        async with self.pool.acquire() as conn:
            cursor = await conn.execute('''
                SELECT * FROM groups 
                WHERE is_active = 1 
                ORDER BY added_at DESC
            ''')
            rows = await cursor.fetchall()
            return [
                Group(
                    chat_id=row['chat_id'],
                    username=row['username'],
                    title=row['title'],
                    is_active=bool(row['is_active']),
                    error_count=row['error_count'],
                    last_error=row['last_error'],
                    last_check=row['last_check']
                )
                for row in rows
            ]
    
    async def update_group_error(self, chat_id: int, error: str):
        """به‌روزرسانی خطای گروه با logic هوشمند"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                UPDATE groups 
                SET error_count = error_count + 1,
                    last_error = ?,
                    is_active = CASE 
                        WHEN error_count >= 5 THEN 0 
                        ELSE is_active 
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
            ''', (error[:200], chat_id))
            await conn.commit()
    
    async def reset_group_errors(self, chat_id: int):
        """ریست خطاهای گروه بعد از موفقیت"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                UPDATE groups 
                SET error_count = 0,
                    last_error = NULL,
                    last_check = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE chat_id = ?
            ''', (time.time(), chat_id))
            await conn.commit()
    
    async def get_active_advertisement(self) -> Optional[Advertisement]:
        """دریافت آخرین تبلیغ فعال"""
        async with self.pool.acquire() as conn:
            cursor = await conn.execute('''
                SELECT * FROM advertisements 
                WHERE is_active = 1 
                ORDER BY created_at DESC 
                LIMIT 1
            ''')
            row = await cursor.fetchone()
            if row:
                return Advertisement(
                    id=row['id'],
                    message_type=MessageType(row['message_type']),
                    content=row['content'],
                    file_id=row['file_id'],
                    is_active=bool(row['is_active'])
                )
            return None
    
    async def get_schedule_config(self) -> ScheduleConfig:
        """دریافت تنظیمات زمان‌بندی"""
        async with self.pool.acquire() as conn:
            cursor = await conn.execute('SELECT * FROM schedule_settings WHERE id = 1')
            row = await cursor.fetchone()
            if row:
                return ScheduleConfig(
                    interval_minutes=row['interval_minutes'],
                    max_sends=row['max_sends'],
                    current_sends=row['current_sends'],
                    is_running=bool(row['is_running']),
                    last_send_time=row['last_send_time']
                )
            return ScheduleConfig()
    
    async def increment_send_count(self) -> int:
        """افزایش تعداد ارسال و برگرداندن مقدار جدید"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                UPDATE schedule_settings 
                SET current_sends = current_sends + 1,
                    last_send_time = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
            ''')
            await conn.commit()
            
            cursor = await conn.execute('SELECT current_sends FROM schedule_settings WHERE id = 1')
            row = await cursor.fetchone()
            return row['current_sends']
    
    async def reset_send_count(self):
        """ریست تعداد ارسال"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                UPDATE schedule_settings 
                SET current_sends = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
            ''')
            await conn.commit()
    
    async def update_schedule_running(self, is_running: bool):
        """به‌روزرسانی وضعیت اجرا"""
        async with self.pool.acquire() as conn:
            await conn.execute('''
                UPDATE schedule_settings 
                SET is_running = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
            ''', (1 if is_running else 0,))
            await conn.commit()

# ==================== کش با محدودیت حافظه ====================

class LRUCache:
    """کش با استراتژی LRU برای جلوگیری از Memory Leak"""
    
    def __init__(self, max_size: int = 1000, ttl: int = 300):
        self.max_size = max_size
        self.ttl = ttl
        self._cache: OrderedDict = OrderedDict()
        self._timestamps: Dict[str, float] = {}
    
    def get(self, key: str):
        """دریافت مقدار از کش"""
        if key not in self._cache:
            return None
        
        # بررسی TTL
        if time.time() - self._timestamps[key] > self.ttl:
            self.delete(key)
            return None
        
        # حرکت به انتها (اخرین استفاده)
        self._cache.move_to_end(key)
        return self._cache[key]
    
    def set(self, key: str, value: Any):
        """ذخیره در کش با حذف قدیمی‌ترین در صورت نیاز"""
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self.max_size:
                # حذف قدیمی‌ترین
                oldest = next(iter(self._cache))
                self.delete(oldest)
        
        self._cache[key] = value
        self._timestamps[key] = time.time()
    
    def delete(self, key: str):
        """حذف از کش"""
        if key in self._cache:
            del self._cache[key]
        if key in self._timestamps:
            del self._timestamps[key]
    
    def clear(self):
        """پاک کردن کامل کش"""
        self._cache.clear()
        self._timestamps.clear()

# ==================== صف پیام با backpressure ====================

class AsyncMessageQueue:
    """صف پیام با قابلیت backpressure و ذخیره‌سازی موقت"""
    
    def __init__(self, bot_token: str, max_size: int = 1000, max_concurrent: int = 5):
        self.bot_token = bot_token
        self.max_size = max_size
        self.max_concurrent = max_concurrent
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_size)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active_tasks: Set[asyncio.Task] = set()
        self._running = False
        self._stats = {
            'sent': 0,
            'failed': 0,
            'rate_limited': 0
        }
        
        # Session aiohttp برای ارسال درخواست‌ها
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def start(self):
        """شروع پردازشگر صف"""
        if self._running:
            return
        
        self._running = True
        self._session = aiohttp.ClientSession(
            timeout=ClientTimeout(total=30),
            connector=TCPConnector(limit=100)
        )
        
        # شروع workerها
        for i in range(self.max_concurrent):
            task = asyncio.create_task(self._worker(f"worker-{i}"))
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)
        
        logger.info(f"صف پیام با {self.max_concurrent} worker شروع به کار کرد")
    
    async def stop(self):
        """توقف صف با پردازش باقیمانده"""
        self._running = False
        
        # انتظار برای خالی شدن صف
        if not self._queue.empty():
            logger.info(f"در حال پردازش {self._queue.qsize()} پیام باقیمانده...")
            await self._queue.join()
        
        # لغو workerها
        for task in self._active_tasks:
            task.cancel()
        
        if self._session:
            await self._session.close()
        
        logger.info("صف پیام متوقف شد")
    
    async def add_message(self, chat_id: int, message_type: MessageType, 
                          content: str = None, file_id: str = None) -> bool:
        """افزودن پیام به صف با backpressure"""
        if self._queue.qsize() >= self.max_size:
            logger.warning(f"صف پر است. پیام برای {chat_id} رد شد.")
            return False
        
        try:
            await asyncio.wait_for(
                self._queue.put({
                    'chat_id': chat_id,
                    'type': message_type.value,
                    'content': content,
                    'file_id': file_id,
                    'retries': 0
                }),
                timeout=1.0
            )
            return True
        except asyncio.TimeoutError:
            logger.error("Timeout در افزودن به صف")
            return False
    
    async def _worker(self, name: str):
        """worker برای پردازش پیام‌ها"""
        while self._running:
            try:
                # دریافت پیام با timeout
                message = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                
                # پردازش با semaphore برای کنترل همزمانی
                async with self._semaphore:
                    await self._send_message(message)
                
                self._queue.task_done()
                
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"خطا در worker {name}: {e}")
                self._queue.task_done()
    
    async def _send_message(self, message: Dict):
        """ارسال واقعی پیام با مدیریت خطا"""
        url = f"https://api.telegram.org/bot{self.bot_token}/"
        
        try:
            if message['type'] == MessageType.TEXT.value:
                url += "sendMessage"
                data = {
                    'chat_id': message['chat_id'],
                    'text': message['content']
                }
            elif message['type'] == MessageType.PHOTO.value:
                url += "sendPhoto"
                data = {
                    'chat_id': message['chat_id'],
                    'photo': message['file_id'],
                    'caption': message['content']
                }
            # ... سایر انواع پیام
            
            async with self._session.post(url, json=data) as response:
                result = await response.json()
                
                if not result.get('ok'):
                    error_code = result.get('error_code')
                    
                    if error_code == 429:  # Too Many Requests
                        self._stats['rate_limited'] += 1
                        retry_after = result.get('parameters', {}).get('retry_after', 5)
                        logger.warning(f"Rate limited. توقف {retry_after} ثانیه")
                        
                        # تلاش مجدد با backoff
                        if message['retries'] < 3:
                            message['retries'] += 1
                            await asyncio.sleep(retry_after)
                            await self._queue.put(message)
                        return
                    
                    elif error_code in [400, 403, 404]:
                        # خطای دائمی
                        self._stats['failed'] += 1
                        logger.error(f"خطای دائمی برای {message['chat_id']}: {result}")
                        return
                    
                    else:
                        self._stats['failed'] += 1
                        logger.error(f"خطای ناشناخته: {result}")
                
                else:
                    self._stats['sent'] += 1
                    
        except asyncio.TimeoutError:
            logger.error(f"Timeout در ارسال به {message['chat_id']}")
            if message['retries'] < 3:
                message['retries'] += 1
                await self._queue.put(message)
        except Exception as e:
            logger.error(f"خطا در ارسال: {e}")
            self._stats['failed'] += 1
    
    def get_stats(self) -> Dict:
        """دریافت آمار صف"""
        return {
            **self._stats,
            'queue_size': self._queue.qsize(),
            'active_workers': len(self._active_tasks)
        }

# ==================== زمان‌بندی Async ====================

class AsyncScheduler:
    """زمان‌بندی با معماری Async و قابلیت Resume"""
    
    def __init__(self, db: DatabaseModels, queue: AsyncMessageQueue, bot_token: str):
        self.db = db
        self.queue = queue
        self.bot_token = bot_token
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._lock = asyncio.Lock()
        self._admin_cache = LRUCache(max_size=100, ttl=3600)  # کش 1 ساعته
    
    async def start(self):
        """شروع زمان‌بندی با قابلیت Resume"""
        async with self._lock:
            if self._running:
                logger.warning("زمان‌بندی از قبل در حال اجراست")
                return
            
            # بررسی وضعیت قبلی از دیتابیس
            config = await self.db.get_schedule_config()
            
            self._running = True
            self._task = asyncio.create_task(self._run())
            
            # اگر قبلاً در حال اجرا بود، ادامه بده
            if config.is_running:
                logger.info("Resume زمان‌بندی از جلسه قبل")
            
            logger.info("زمان‌بندی شروع شد")
    
    async def stop(self):
        """توقف زمان‌بندی"""
        async with self._lock:
            if not self._running:
                return
            
            self._running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            
            await self.db.update_schedule_running(False)
            logger.info("زمان‌بندی متوقف شد")
    
    async def _run(self):
        """حلقه اصلی زمان‌بندی"""
        while self._running:
            try:
                # دریافت تنظیمات
                config = await self.db.get_schedule_config()
                
                if not config.is_running:
                    await asyncio.sleep(5)
                    continue
                
                # بررسی محدودیت تعداد
                if config.max_sends > 0 and config.current_sends >= config.max_sends:
                    logger.info("به حداکثر تعداد ارسال رسیدیم")
                    await self.stop()
                    continue
                
                # دریافت تبلیغ فعال
                ad = await self.db.get_active_advertisement()
                if not ad:
                    logger.warning("تبلیغ فعالی وجود ندارد")
                    await asyncio.sleep(60)
                    continue
                
                # دریافت گروه‌ها
                groups = await self.db.get_active_groups()
                if not groups:
                    logger.warning("گروه فعالی وجود ندارد")
                    await asyncio.sleep(60)
                    continue
                
                # ارسال به گروه‌ها
                for group in groups:
                    if not self._running:
                        break
                    
                    try:
                        # بررسی ادمین بودن با کش
                        is_admin = await self._check_admin_cached(group.chat_id)
                        
                        if not is_admin:
                            logger.warning(f"ربات در گروه {group.chat_id} ادمین نیست")
                            await self.db.update_group_error(group.chat_id, "ربات ادمین نیست")
                            continue
                        
                        # ریست خطاها در صورت موفقیت
                        if group.error_count > 0:
                            await self.db.reset_group_errors(group.chat_id)
                        
                        # افزودن به صف
                        success = await self.queue.add_message(
                            chat_id=group.chat_id,
                            message_type=ad.message_type,
                            content=ad.content,
                            file_id=ad.file_id
                        )
                        
                        if not success:
                            logger.warning(f"صف پر است. گروه {group.chat_id} در نوبت بعدی")
                        
                    except Exception as e:
                        logger.error(f"خطا در پردازش گروه {group.chat_id}: {e}")
                        await self.db.update_group_error(group.chat_id, str(e))
                
                # افزایش شمارنده
                new_count = await self.db.increment_send_count()
                logger.info(f"دوره ارسال کامل شد. ارسال‌ها: {new_count}")
                
                # انتظار تا دوره بعدی
                await asyncio.sleep(config.interval_minutes * 60)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"خطای بحرانی در زمان‌بندی: {e}")
                await asyncio.sleep(60)
    
    async def _check_admin_cached(self, chat_id: int) -> bool:
        """بررسی ادمین بودن با کش"""
        cache_key = f"admin_{chat_id}"
        cached = self._admin_cache.get(cache_key)
        
        if cached is not None:
            return cached
        
        try:
            # استفاده از API تلگرام
            url = f"https://api.telegram.org/bot{self.bot_token}/getChatMember"
            params = {
                'chat_id': chat_id,
                'user_id': (await self._get_bot_id())
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    result = await response.json()
                    
                    if result.get('ok'):
                        status = result['result']['status']
                        is_admin = status in ['administrator', 'creator']
                        self._admin_cache.set(cache_key, is_admin)
                        return is_admin
                    
                    return False
                    
        except Exception as e:
            logger.error(f"خطا در بررسی ادمین {chat_id}: {e}")
            return False
    
    async def _get_bot_id(self) -> int:
        """دریافت آیدی ربات با کش"""
        cache_key = "bot_id"
        cached = self._admin_cache.get(cache_key)
        
        if cached is not None:
            return cached
        
        url = f"https://api.telegram.org/bot{self.bot_token}/getMe"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                result = await response.json()
                bot_id = result['result']['id']
                self._admin_cache.set(cache_key, bot_id)
                return bot_id

# ==================== مدیریت وضعیت کاربر با LRU ====================

class UserStateManager:
    """مدیریت وضعیت کاربر با LRU Cache"""
    
    def __init__(self, max_users: int = 1000, timeout: int = 300):
        self._states = LRUCache(max_size=max_users, ttl=timeout)
    
    def get(self, user_id: int) -> Dict:
        """دریافت وضعیت کاربر"""
        return self._states.get(str(user_id)) or {'state': None, 'data': {}}
    
    def set(self, user_id: int, state: str, data: Dict = None):
        """تنظیم وضعیت کاربر"""
        self._states.set(str(user_id), {
            'state': state,
            'data': data or {},
            'updated_at': time.time()
        })
    
    def clear(self, user_id: int):
        """پاک کردن وضعیت کاربر"""
        self._states.delete(str(user_id))

# ==================== ربات اصلی ====================

class TelegramBot:
    """کلاس اصلی ربات با معماری Async"""
    
    def __init__(self, token: str, db_pool: AsyncDatabasePool):
        self.token = token
        self.db_pool = db_pool
        self.db = DatabaseModels(db_pool)
        self.queue = AsyncMessageQueue(token)
        self.scheduler = AsyncScheduler(self.db, self.queue, token)
        self.user_states = UserStateManager()
        self._update_queue: asyncio.Queue = asyncio.Queue()
        self._processing_task: Optional[asyncio.Task] = None
    
    async def initialize(self):
        """راه‌اندازی اولیه"""
        # ایجاد جداول دیتابیس
        await self.db.init_tables()
        
        # شروع صف پیام
        await self.queue.start()
        
        # Resume scheduler اگر قبلاً فعال بوده
        config = await self.db.get_schedule_config()
        if config.is_running:
            await self.scheduler.start()
        
        logger.info("ربات با موفقیت راه‌اندازی شد")
    
    async def shutdown(self):
        """خاموشی تمیز"""
        logger.info("در حال خاموش کردن ربات...")
        
        # توقف scheduler
        await self.scheduler.stop()
        
        # توقف صف و پردازش پیام‌های باقیمانده
        await self.queue.stop()
        
        # بستن دیتابیس
        await self.db_pool.close_all()
        
        logger.info("ربات خاموش شد")
    
    async def process_update(self, update: Update):
        """پردازش آپدیت دریافتی از تلگرام"""
        try:
            if update.message:
                await self._handle_message(update.message)
            elif update.callback_query:
                await self._handle_callback(update.callback_query)
        except Exception as e:
            logger.error(f"خطا در پردازش آپدیت: {e}")
    
    async def _handle_message(self, message):
        """هندلر پیام‌ها"""
        if message.from_user.id != ADMIN_ID:
            await self._send_message(message.chat.id, "⛔ شما اجازه استفاده از این ربات را ندارید.")
            return
        
        # دریافت وضعیت کاربر
        state = self.user_states.get(message.from_user.id)
        
        # پردازش بر اساس وضعیت
        if state['state'] == 'waiting_group':
            await self._handle_add_group(message)
        elif state['state'] == 'waiting_ad_content':
            await self._handle_ad_content(message)
        else:
            await self._handle_command(message)
    
    async def _send_message(self, chat_id: int, text: str, keyboard=None):
        """ارسال پیام از طریق صف"""
        await self.queue.add_message(chat_id, MessageType.TEXT, text)
    
    async def _handle_command(self, message):
        """پردازش دستورات اصلی"""
        text = message.text
        
        if text == "👥 افزودن گروه":
            self.user_states.set(message.from_user.id, 'waiting_group')
            await self._send_message(
                message.chat.id,
                "👥 لطفاً یوزرنیم گروه را با @ وارد کنید:"
            )
        
        elif text == "📊 وضعیت سیستم":
            stats = self.queue.get_stats()
            groups = await self.db.get_active_groups()
            config = await self.db.get_schedule_config()
            
            status = f"""
📊 وضعیت سیستم:

👥 گروه‌های فعال: {len(groups)}
📤 پیام‌های ارسالی: {stats['sent']}
❌ خطاها: {stats['failed']}
⚠️ Rate Limited: {stats['rate_limited']}
📦 صف: {stats['queue_size']} پیام

⏱ فاصله: {config.interval_minutes} دقیقه
📊 تعداد ارسال: {config.current_sends}/{config.max_sends if config.max_sends > 0 else '∞'}
▶️ وضعیت: {'فعال' if config.is_running else 'غیرفعال'}
            """
            await self._send_message(message.chat.id, status)
        
        # ... سایر دستورات
    
    async def _handle_add_group(self, message):
        """افزودن گروه جدید"""
        username = message.text.strip()
        
        try:
            # دریافت اطلاعات گروه از API
            url = f"https://api.telegram.org/bot{self.token}/getChat"
            params = {'chat_id': username}
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    result = await response.json()
                    
                    if not result.get('ok'):
                        await self._send_message(
                            message.chat.id,
                            f"❌ گروه {username} یافت نشد."
                        )
                        return
                    
                    chat = result['result']
                    chat_id = chat['id']
                    title = chat.get('title', username)
                    
                    # بررسی ادمین بودن
                    url = f"https://api.telegram.org/bot{self.token}/getChatMember"
                    params = {
                        'chat_id': chat_id,
                        'user_id': (await self._get_bot_id())
                    }
                    
                    async with session.get(url, params=params) as admin_response:
                        admin_result = await admin_response.json()
                        
                        if not admin_result.get('ok'):
                            await self._send_message(
                                message.chat.id,
                                f"❌ خطا در بررسی دسترسی‌ها."
                            )
                            return
                        
                        status = admin_result['result']['status']
                        if status not in ['administrator', 'creator']:
                            await self._send_message(
                                message.chat.id,
                                f"❌ ربات در گروه {title} ادمین نیست."
                            )
                            return
                    
                    # ذخیره در دیتابیس
                    async with self.db_pool.acquire() as conn:
                        await conn.execute('''
                            INSERT OR REPLACE INTO groups 
                            (chat_id, username, title, is_active, last_check)
                            VALUES (?, ?, ?, 1, ?)
                        ''', (chat_id, username, title, time.time()))
                        await conn.commit()
                    
                    await self._send_message(
                        message.chat.id,
                        f"✅ گروه {title} با موفقیت اضافه شد!"
                    )
                    
        except Exception as e:
            logger.error(f"خطا در افزودن گروه: {e}")
            await self._send_message(
                message.chat.id,
                f"❌ خطا: {str(e)}"
            )
        finally:
            self.user_states.clear(message.from_user.id)
    
    async def _get_bot_id(self) -> int:
        """دریافت آیدی ربات"""
        url = f"https://api.telegram.org/bot{self.token}/getMe"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                result = await response.json()
                return result['result']['id']

# ==================== Flask App با Async ====================

app = Flask(__name__)
bot_instance: Optional[TelegramBot] = None
db_pool: Optional[AsyncDatabasePool] = None

def verify_webhook_signature(request):
    """بررسی امضای webhook"""
    signature = request.headers.get('X-Telegram-Bot-Api-Secret-Token')
    if not signature:
        logger.warning("درخواست webhook بدون امضا")
        return False
    return hmac.compare_digest(signature, WEBHOOK_SECRET)

@app.route('/webhook', methods=['POST'])
async def webhook():
    """دریافت آپدیت از تلگرام"""
    if not verify_webhook_signature(request):
        abort(403)
    
    if request.headers.get('content-type') != 'application/json':
        return jsonify({'status': 'bad request'}), 400
    
    try:
        update_data = request.json
        update = Update.de_json(update_data)
        
        if bot_instance:
            # پردازش async بدون blocking
            asyncio.create_task(bot_instance.process_update(update))
        
        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        logger.error(f"خطا در webhook: {e}")
        return jsonify({'status': 'error'}), 500

@app.route('/')
async def health():
    """بررسی سلامت"""
    if not bot_instance:
        return jsonify({'status': 'initializing'}), 503
    
    stats = bot_instance.queue.get_stats()
    groups = await bot_instance.db.get_active_groups()
    config = await bot_instance.db.get_schedule_config()
    
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'stats': {
            'active_groups': len(groups),
            'queue_size': stats['queue_size'],
            'sent_messages': stats['sent'],
            'scheduler_running': config.is_running,
            'current_sends': config.current_sends
        }
    }), 200

@app.route('/set_webhook', methods=['POST'])
async def set_webhook():
    """تنظیم webhook (فقط برای ادمین)"""
    # بررسی IP برای امنیت (فقط localhost یا Render internal)
    if request.remote_addr not in ['127.0.0.1', '::1']:
        abort(403)
    
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
    data = {
        'url': f"{WEBHOOK_URL}/webhook",
        'secret_token': WEBHOOK_SECRET,
        'max_connections': 40,
        'allowed_updates': ['message', 'callback_query']
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=data) as response:
            result = await response.json()
            return jsonify(result), 200

# ==================== راه‌اندازی Async ====================

async def startup():
    """راه‌اندازی Async"""
    global bot_instance, db_pool
    
    # ایجاد پول دیتابیس
    db_pool = AsyncDatabasePool('bot_data.db')
    await db_pool.initialize()
    
    # ایجاد ربات
    bot_instance = TelegramBot(BOT_TOKEN, db_pool)
    await bot_instance.initialize()
    
    logger.info("سیستم با موفقیت راه‌اندازی شد")

async def shutdown():
    """خاموشی"""
    if bot_instance:
        await bot_instance.shutdown()
    if db_pool:
        await db_pool.close_all()
    logger.info("سیستم خاموش شد")

# ==================== مدیریت سیگنال‌ها ====================

def handle_signal(sig, frame):
    """مدیریت سیگنال خاموشی"""
    logger.info(f"سیگنال {sig} دریافت شد")
    asyncio.create_task(shutdown())
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

# ==================== اجرا با Flask و Async ====================

if __name__ == '__main__':
    # راه‌اندازی event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # اجرای startup
    loop.run_until_complete(startup())
    
    try:
        # اجرای Flask با ASGI
        port = int(os.environ.get('PORT', 5000))
        from asgiref.wsgi import WsgiToAsgi
        asgi_app = WsgiToAsgi(app)
        
        import uvicorn
        uvicorn.run(
            asgi_app,
            host='0.0.0.0',
            port=port,
            loop='asyncio',
            log_level=LOG_LEVEL.lower()
        )
    finally:
        # خاموشی
        loop.run_until_complete(shutdown())
        loop.close()
