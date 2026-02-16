"""
ربات تلگرام ارسال خودکار تبلیغات
قابل دیپلوی روی Render.com
ساخته شده با Flask + Telebot + Webhook
"""

import os
import json
import time
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from threading import Thread
from functools import wraps

from flask import Flask, request, jsonify
import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, Message
from telebot.apihelper import ApiTelegramException

# ==================== تنظیمات اولیه ====================
# دریافت متغیرهای محیطی
BOT_TOKEN = os.environ.get('BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
ADMIN_ID = int(os.environ.get('ADMIN_ID', 0))

if not all([BOT_TOKEN, WEBHOOK_URL, ADMIN_ID]):
    raise ValueError("لطفاً تمام متغیرهای محیطی را تنظیم کنید!")

# تنظیم لاگینگ
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ایجاد اپلیکیشن Flask و ربات
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

# ==================== دیتابیس ====================
def get_db():
    """ایجاد اتصال به دیتابیس SQLite"""
    conn = sqlite3.connect('bot_data.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_database():
    """ایجاد جداول دیتابیس"""
    conn = get_db()
    cursor = conn.cursor()
    
    # جدول گروه‌ها
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT UNIQUE,
            username TEXT,
            title TEXT,
            added_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT 1
        )
    ''')
    
    # جدول تبلیغات
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            content TEXT,
            file_id TEXT,
            created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # جدول تنظیمات زمان‌بندی
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            interval_minutes INTEGER DEFAULT 5,
            total_count INTEGER DEFAULT 0,
            sent_count INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT 0,
            last_sent TIMESTAMP,
            updated_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # درج تنظیمات پیش‌فرض
    cursor.execute('SELECT COUNT(*) as count FROM schedule')
    if cursor.fetchone()['count'] == 0:
        cursor.execute('INSERT INTO schedule (interval_minutes, total_count) VALUES (5, 0)')
    
    conn.commit()
    conn.close()
    logger.info("دیتابیس راه‌اندازی شد")

# ==================== تابع‌های کمکی ====================
def admin_only(func):
    """دکوراتور برای محدود کردن دسترسی به ادمین"""
    @wraps(func)
    def wrapper(message):
        if message.from_user.id != ADMIN_ID:
            bot.reply_to(message, "⛔ شما اجازه استفاده از این دستور را ندارید!")
            return
        return func(message)
    return wrapper

def get_main_keyboard():
    """ایجاد کیبورد اصلی"""
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        KeyboardButton("📤 ثبت تبلیغ"),
        KeyboardButton("👥 افزودن گروه"),
        KeyboardButton("📋 لیست گروه‌ها"),
        KeyboardButton("⏱ تنظیم زمان ارسال"),
        KeyboardButton("▶️ شروع ارسال"),
        KeyboardButton("⛔ توقف ارسال"),
        KeyboardButton("📊 وضعیت")
    ]
    keyboard.add(*buttons)
    return keyboard

def get_chat_id_from_username(username):
    """دریافت chat_id از روی یوزرنیم گروه"""
    try:
        chat = bot.get_chat(username)
        return str(chat.id), chat.title
    except Exception as e:
        logger.error(f"خطا در دریافت chat_id برای {username}: {e}")
        return None, None

# ==================== مدیریت وضعیت کاربران ====================
user_states = {}
user_data = {}

def set_user_state(user_id, state, data=None):
    """تنظیم وضعیت کاربر"""
    user_states[user_id] = state
    if data:
        if user_id not in user_data:
            user_data[user_id] = {}
        user_data[user_id].update(data)

def get_user_state(user_id):
    """دریافت وضعیت کاربر"""
    return user_states.get(user_id)

def clear_user_state(user_id):
    """پاک کردن وضعیت کاربر"""
    if user_id in user_states:
        del user_states[user_id]
    if user_id in user_data:
        del user_data[user_id]

# ==================== هندلرهای ربات ====================
@bot.message_handler(commands=['start'])
def start_command(message):
    """هندلر دستور start"""
    if message.from_user.id == ADMIN_ID:
        bot.reply_to(
            message,
            "✨ به ربات مدیریت تبلیغات خوش آمدید!\n\n"
            "از منوی زیر برای مدیریت ربات استفاده کنید:",
            reply_markup=get_main_keyboard()
        )
    else:
        bot.reply_to(message, "🤖 این ربات برای مدیریت تبلیغات خودکار طراحی شده است.")

@bot.message_handler(func=lambda msg: msg.text == "📤 ثبت تبلیغ")
@admin_only
def register_ad(message):
    """شروع فرآیند ثبت تبلیغ"""
    set_user_state(message.from_user.id, "waiting_for_ad_type")
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.add("متن", "عکس", "ویدیو", "فایل", "لغو")
    bot.reply_to(
        message,
        "📝 نوع تبلیغ را انتخاب کنید:",
        reply_markup=keyboard
    )

@bot.message_handler(func=lambda msg: msg.text == "👥 افزودن گروه")
@admin_only
def add_group(message):
    """شروع فرآیند افزودن گروه"""
    set_user_state(message.from_user.id, "waiting_for_group_username")
    bot.reply_to(
        message,
        "🔗 لطفاً یوزرنیم گروه را با @ وارد کنید:\n"
        "مثال: @mygroup\n\n"
        "توجه: ربات باید در گروه ادمین باشد!"
    )

@bot.message_handler(func=lambda msg: msg.text == "📋 لیست گروه‌ها")
@admin_only
def list_groups(message):
    """نمایش لیست گروه‌ها"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM groups WHERE is_active = 1 ORDER BY added_date DESC')
    groups = cursor.fetchall()
    conn.close()
    
    if not groups:
        bot.reply_to(message, "📭 هیچ گروهی ثبت نشده است!")
        return
    
    text = "📋 لیست گروه‌های فعال:\n\n"
    for group in groups:
        text += f"🔹 {group['title']}\n"
        text += f"   آیدی: {group['chat_id']}\n"
        text += f"   یوزرنیم: {group['username']}\n"
        text += "-" * 20 + "\n"
    
    bot.reply_to(message, text)

@bot.message_handler(func=lambda msg: msg.text == "⏱ تنظیم زمان ارسال")
@admin_only
def set_schedule(message):
    """تنظیم زمان‌بندی ارسال"""
    set_user_state(message.from_user.id, "waiting_for_interval")
    bot.reply_to(
        message,
        "⏱ لطفاً فاصله زمانی ارسال را به دقیقه وارد کنید:\n"
        "(مثال: 5 برای ارسال هر 5 دقیقه)"
    )

@bot.message_handler(func=lambda msg: msg.text == "▶️ شروع ارسال")
@admin_only
def start_sending(message):
    """شروع ارسال خودکار"""
    conn = get_db()
    cursor = conn.cursor()
    
    # بررسی وجود تبلیغ
    cursor.execute('SELECT COUNT(*) as count FROM ads')
    if cursor.fetchone()['count'] == 0:
        bot.reply_to(message, "❌ ابتدا یک تبلیغ ثبت کنید!")
        conn.close()
        return
    
    # بررسی وجود گروه
    cursor.execute('SELECT COUNT(*) as count FROM groups WHERE is_active = 1')
    if cursor.fetchone()['count'] == 0:
        bot.reply_to(message, "❌ حداقل یک گروه فعال ثبت کنید!")
        conn.close()
        return
    
    cursor.execute('UPDATE schedule SET is_active = 1, updated_date = CURRENT_TIMESTAMP')
    conn.commit()
    conn.close()
    
    bot.reply_to(message, "✅ ارسال خودکار شروع شد!")

@bot.message_handler(func=lambda msg: msg.text == "⛔ توقف ارسال")
@admin_only
def stop_sending(message):
    """توقف ارسال خودکار"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('UPDATE schedule SET is_active = 0, updated_date = CURRENT_TIMESTAMP')
    conn.commit()
    conn.close()
    
    bot.reply_to(message, "⏸ ارسال خودکار متوقف شد!")

@bot.message_handler(func=lambda msg: msg.text == "📊 وضعیت")
@admin_only
def show_status(message):
    """نمایش وضعیت ربات"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('SELECT COUNT(*) as count FROM groups WHERE is_active = 1')
    groups_count = cursor.fetchone()['count']
    
    cursor.execute('SELECT COUNT(*) as count FROM ads')
    ads_count = cursor.fetchone()['count']
    
    cursor.execute('SELECT * FROM schedule WHERE id = 1')
    schedule = cursor.fetchone()
    
    conn.close()
    
    status_text = f"📊 وضعیت ربات:\n\n"
    status_text += f"👥 گروه‌های فعال: {groups_count}\n"
    status_text += f"📝 تبلیغات: {ads_count}\n"
    status_text += f"⏱ فاصله ارسال: {schedule['interval_minutes']} دقیقه\n"
    status_text += f"📨 ارسال شده: {schedule['sent_count']}\n"
    status_text += f"⚡ وضعیت: {'✅ فعال' if schedule['is_active'] else '⏸ غیرفعال'}\n"
    
    if schedule['total_count'] > 0:
        status_text += f"🎯 هدف: {schedule['sent_count']}/{schedule['total_count']}\n"
    
    bot.reply_to(message, status_text)

# ==================== هندلرهای مراحل ====================
@bot.message_handler(func=lambda msg: get_user_state(msg.from_user.id) == "waiting_for_ad_type")
@admin_only
def handle_ad_type(message):
    """دریافت نوع تبلیغ"""
    if message.text == "لغو":
        clear_user_state(message.from_user.id)
        bot.reply_to(message, "❌ عملیات لغو شد.", reply_markup=get_main_keyboard())
        return
    
    ad_type_map = {
        "متن": "text",
        "عکس": "photo",
        "ویدیو": "video",
        "فایل": "document"
    }
    
    if message.text not in ad_type_map:
        bot.reply_to(message, "❌ لطفاً یک گزینه معتبر انتخاب کنید!")
        return
    
    set_user_state(
        message.from_user.id,
        "waiting_for_ad_content",
        {"ad_type": ad_type_map[message.text]}
    )
    
    if message.text == "متن":
        bot.reply_to(message, "📝 لطفاً متن تبلیغ را ارسال کنید:")
    else:
        bot.reply_to(message, f"📎 لطفاً {message.text} تبلیغ را ارسال کنید:")

@bot.message_handler(
    content_types=['text', 'photo', 'video', 'document'],
    func=lambda msg: get_user_state(msg.from_user.id) == "waiting_for_ad_content"
)
@admin_only
def handle_ad_content(message):
    """دریافت محتوای تبلیغ"""
    user_id = message.from_user.id
    user_info = user_data.get(user_id, {})
    ad_type = user_info.get('ad_type')
    
    # بررسی تطابق نوع ارسالی با نوع درخواستی
    content_type = None
    content = None
    file_id = None
    
    if message.content_type == 'text' and ad_type == 'text':
        content_type = 'text'
        content = message.text
    elif message.content_type == 'photo' and ad_type == 'photo':
        content_type = 'photo'
        file_id = message.photo[-1].file_id
    elif message.content_type == 'video' and ad_type == 'video':
        content_type = 'video'
        file_id = message.video.file_id
    elif message.content_type == 'document' and ad_type == 'document':
        content_type = 'document'
        file_id = message.document.file_id
    
    if not content_type:
        bot.reply_to(message, "❌ نوع فایل ارسالی با نوع درخواستی مطابقت ندارد!")
        return
    
    # ذخیره در دیتابیس
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO ads (type, content, file_id) VALUES (?, ?, ?)',
        (content_type, content, file_id)
    )
    conn.commit()
    conn.close()
    
    clear_user_state(user_id)
    bot.reply_to(
        message,
        "✅ تبلیغ با موفقیت ثبت شد!",
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(func=lambda msg: get_user_state(msg.from_user.id) == "waiting_for_group_username")
@admin_only
def handle_group_username(message):
    """دریافت یوزرنیم گروه و ثبت آن"""
    username = message.text.strip()
    if not username.startswith('@'):
        bot.reply_to(message, "❌ لطفاً یوزرنیم را با @ وارد کنید!")
        return
    
    # دریافت chat_id و عنوان گروه
    chat_id, title = get_chat_id_from_username(username)
    
    if not chat_id:
        bot.reply_to(
            message,
            "❌ خطا در دریافت اطلاعات گروه!\n"
            "مطمئن شوید:\n"
            "1. یوزرنیم صحیح است\n"
            "2. ربات در گروه عضو است\n"
            "3. ربات در گروه ادمین است"
        )
        return
    
    # بررسی ادمین بودن ربات
    try:
        bot.get_chat_administrators(chat_id)
    except Exception as e:
        bot.reply_to(
            message,
            f"❌ ربات در گروه {username} ادمین نیست!\n"
            "لطفاً ابتدا ربات را ادمین کنید."
        )
        return
    
    # ذخیره در دیتابیس
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'INSERT INTO groups (chat_id, username, title) VALUES (?, ?, ?)',
            (chat_id, username, title)
        )
        conn.commit()
        bot.reply_to(
            message,
            f"✅ گروه {title} با موفقیت ثبت شد!\n"
            f"آیدی گروه: {chat_id}",
            reply_markup=get_main_keyboard()
        )
    except sqlite3.IntegrityError:
        bot.reply_to(message, "❌ این گروه قبلاً ثبت شده است!")
    finally:
        conn.close()
        clear_user_state(message.from_user.id)

@bot.message_handler(func=lambda msg: get_user_state(msg.from_user.id) == "waiting_for_interval")
@admin_only
def handle_interval(message):
    """دریافت فاصله زمانی"""
    try:
        interval = int(message.text)
        if interval < 1:
            raise ValueError()
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE schedule SET interval_minutes = ?, updated_date = CURRENT_TIMESTAMP WHERE id = 1',
            (interval,)
        )
        conn.commit()
        conn.close()
        
        bot.reply_to(
            message,
            f"✅ فاصله زمانی با موفقیت به {interval} دقیقه تنظیم شد!",
            reply_markup=get_main_keyboard()
        )
        clear_user_state(message.from_user.id)
        
    except ValueError:
        bot.reply_to(message, "❌ لطفاً یک عدد معتبر وارد کنید!")

# ==================== سیستم ارسال خودکار ====================
async def send_ad_to_group(chat_id, ad):
    """ارسال تبلیغ به یک گروه خاص"""
    try:
        if ad['type'] == 'text':
            bot.send_message(chat_id, ad['content'])
        elif ad['type'] == 'photo':
            bot.send_photo(chat_id, ad['file_id'], caption=ad['content'] if ad['content'] else '')
        elif ad['type'] == 'video':
            bot.send_video(chat_id, ad['file_id'], caption=ad['content'] if ad['content'] else '')
        elif ad['type'] == 'document':
            bot.send_document(chat_id, ad['file_id'], caption=ad['content'] if ad['content'] else '')
        return True
    except ApiTelegramException as e:
        if e.error_code == 403:  # ربات از گروه حذف شده
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute('UPDATE groups SET is_active = 0 WHERE chat_id = ?', (chat_id,))
            conn.commit()
            conn.close()
            logger.info(f"ربات از گروه {chat_id} حذف شده است")
        else:
            logger.error(f"خطا در ارسال به گروه {chat_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"خطای ناشناخته در ارسال به گروه {chat_id}: {e}")
        return False

async def scheduled_sender():
    """تابع اصلی ارسال خودکار"""
    while True:
        try:
            conn = get_db()
            cursor = conn.cursor()
            
            # دریافت تنظیمات
            cursor.execute('SELECT * FROM schedule WHERE id = 1')
            schedule = cursor.fetchone()
            
            if schedule and schedule['is_active']:
                # دریافت تبلیغات
                cursor.execute('SELECT * FROM ads ORDER BY created_date DESC LIMIT 1')
                ad = cursor.fetchone()
                
                # دریافت گروه‌های فعال
                cursor.execute('SELECT * FROM groups WHERE is_active = 1')
                groups = cursor.fetchall()
                
                if ad and groups:
                    for group in groups:
                        success = await send_ad_to_group(group['chat_id'], ad)
                        if success:
                            await asyncio.sleep(2)  # تاخیر بین ارسال به گروه‌ها
                    
                    # به‌روزرسانی آمار
                    cursor.execute(
                        'UPDATE schedule SET sent_count = sent_count + 1, last_sent = CURRENT_TIMESTAMP WHERE id = 1'
                    )
                    
                    # بررسی پایان تعداد ارسال
                    if schedule['total_count'] > 0 and schedule['sent_count'] + 1 >= schedule['total_count']:
                        cursor.execute('UPDATE schedule SET is_active = 0 WHERE id = 1')
                    
                    conn.commit()
                    logger.info(f"تبلیغ به {len(groups)} گروه ارسال شد")
            
            conn.close()
            
            # انتظار تا ارسال بعدی
            interval = schedule['interval_minutes'] if schedule else 5
            await asyncio.sleep(interval * 60)
            
        except Exception as e:
            logger.error(f"خطا در ارسال خودکار: {e}")
            await asyncio.sleep(60)

def start_background_tasks():
    """شروع تسک‌های پس‌زمینه"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(scheduled_sender())

# ==================== Flask Endpoints ====================
@app.route('/')
def health_check():
    """اندپوینت بررسی سلامت"""
    return jsonify({
        'status': 'active',
        'timestamp': datetime.now().isoformat()
    }), 200

@app.route('/webhook', methods=['POST'])
def webhook():
    """دریافت آپدیت‌های تلگرام"""
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    return 'Invalid request', 403

@app.route('/set-webhook', methods=['GET'])
def set_webhook():
    """تنظیم وب‌هوک (فقط برای ادمین)"""
    webhook_url = f"{WEBHOOK_URL}/webhook"
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=webhook_url)
    return jsonify({
        'status': 'webhook_set',
        'url': webhook_url
    }), 200

# ==================== اجرای اصلی ====================
if __name__ == '__main__':
    # راه‌اندازی دیتابیس
    init_database()
    
    # تنظیم وب‌هوک
    webhook_url = f"{WEBHOOK_URL}/webhook"
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=webhook_url)
    logger.info(f"وب‌هوک تنظیم شد: {webhook_url}")
    
    # شروع تسک پس‌زمینه در یک ترد جداگانه
    Thread(target=start_background_tasks, daemon=True).start()
    
    # اجرای Flask
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
