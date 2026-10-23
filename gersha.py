import os
import sqlite3
import asyncio
import time
import html
import re

import requests
import feedparser

from bale import (
    Bot,
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from openai import OpenAI


# =========================================================
# 🔐 تنظیمات
# =========================================================

# =========================================================
# تنظیمات تک‌فایلی — مقادیر را فقط روی گوشی خودت وارد کن
# توکن/کلید را در گروه یا چت عمومی منتشر نکن.
# =========================================================
# برای امنیت، کلیدها در فایل کد ذخیره نمی‌شوند.
# می‌توانی در Pydroid متغیرهای محیطی BALE_TOKEN و GAPGPT_API_KEY را تنظیم کنی؛
# یا مقادیر خودت را فقط در همین دو مقدار محلی جایگزین کن و فایل را منتشر نکن.
BALE_TOKEN = os.getenv("BALE_TOKEN", "").strip()
GAPGPT_API_KEY = os.getenv("GAPGPT_API_KEY", "").strip()

GAPGPT_BASE_URL = "https://api.gapgpt.app/v1"

DEFAULT_MODEL = "gpt-4o-mini"

# آیدی عددی خودت
ADMIN_IDS = {
    "955311935"
}


# =========================================================
# ⚙️ محدودیت
# =========================================================

MAX_AI_MESSAGES = 6
LIMIT_SECONDS = 4 * 60 * 60

MAX_HISTORY = 20


# =========================================================
# 🗃️ DATABASE
# =========================================================

DB_FILE = "bot_v3.db"

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    first_name TEXT,
    username TEXT,
    model TEXT DEFAULT 'gpt-4o-mini',
    created_at REAL,
    last_seen REAL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    role TEXT,
    content TEXT,
    created_at REAL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    created_at REAL
)
""")

db.commit()


# =========================================================
# 🔎 بررسی تنظیمات پیش از اجرا
# =========================================================

if not BALE_TOKEN or BALE_TOKEN == "PASTE_VALID_BALE_TOKEN_HERE":
    raise SystemExit(
        "❌ توکن بله تنظیم نشده است. فایل config.py را باز کنید و توکن واقعیِ مخصوص بله را قرار دهید."
    )

if not GAPGPT_API_KEY or GAPGPT_API_KEY == "PASTE_GAPGPT_KEY_HERE":
    raise SystemExit(
        "❌ کلید GapGPT تنظیم نشده است. مقدار GAPGPT_API_KEY را در config.py بررسی کنید."
    )

# =========================================================
# 🤖 GAPGPT
# =========================================================

ai = OpenAI(
    api_key=GAPGPT_API_KEY,
    base_url=GAPGPT_BASE_URL
)


# =========================================================
# 🕐 زمان
# =========================================================

def now():
    return time.time()


# =========================================================
# 👤 اطلاعات کاربر
# =========================================================

def get_uid(message):
    try:
        return str(message.author.id)
    except Exception:
        return ""


def register_user(message):

    uid = get_uid(message)

    if not uid:
        return

    try:
        first_name = message.author.first_name or ""
    except Exception:
        first_name = ""

    try:
        username = message.author.username or ""
    except Exception:
        username = ""

    db.execute("""
    INSERT INTO users
    (
        user_id,
        first_name,
        username,
        model,
        created_at,
        last_seen
    )
    VALUES (?, ?, ?, ?, ?, ?)

    ON CONFLICT(user_id)
    DO UPDATE SET
        first_name = excluded.first_name,
        username = excluded.username,
        last_seen = excluded.last_seen
    """, (
        uid,
        first_name,
        username,
        DEFAULT_MODEL,
        now(),
        now()
    ))

    db.commit()


# =========================================================
# 🤖 MODEL
# =========================================================

def get_model(uid):

    row = db.execute("""
    SELECT model
    FROM users
    WHERE user_id = ?
    """, (uid,)).fetchone()

    if row:
        return row["model"]

    return DEFAULT_MODEL


def set_model(uid, model):

    db.execute("""
    UPDATE users
    SET model = ?
    WHERE user_id = ?
    """, (
        model,
        uid
    ))

    db.commit()


# =========================================================
# 🧠 MEMORY
# =========================================================

def save_message(
    uid,
    role,
    content
):

    db.execute("""
    INSERT INTO messages
    (
        user_id,
        role,
        content,
        created_at
    )
    VALUES (?, ?, ?, ?)
    """, (
        uid,
        role,
        content,
        now()
    ))

    db.commit()


def get_history(uid):

    rows = db.execute("""
    SELECT role, content
    FROM messages
    WHERE user_id = ?
    ORDER BY id DESC
    LIMIT ?
    """, (
        uid,
        MAX_HISTORY
    )).fetchall()

    rows = list(reversed(rows))

    return [
        {
            "role": row["role"],
            "content": row["content"]
        }
        for row in rows
    ]


def clear_memory(uid):

    db.execute("""
    DELETE FROM messages
    WHERE user_id = ?
    """, (uid,))

    db.commit()


# =========================================================
# ⏱️ 6 پیام در 4 ساعت
# =========================================================

def get_usage(uid):

    cutoff = now() - LIMIT_SECONDS

    row = db.execute("""
    SELECT COUNT(*) AS total
    FROM usage
    WHERE user_id = ?
    AND created_at >= ?
    """, (
        uid,
        cutoff
    )).fetchone()

    return int(row["total"])


def can_use_ai(uid):

    return get_usage(uid) < MAX_AI_MESSAGES


def add_usage(uid):

    db.execute("""
    INSERT INTO usage
    (
        user_id,
        created_at
    )
    VALUES (?, ?)
    """, (
        uid,
        now()
    ))

    db.commit()


def remove_last_usage(uid):

    row = db.execute("""
    SELECT id
    FROM usage
    WHERE user_id = ?
    ORDER BY id DESC
    LIMIT 1
    """, (uid,)).fetchone()

    if row:

        db.execute("""
        DELETE FROM usage
        WHERE id = ?
        """, (
            row["id"],
        ))

        db.commit()


def usage_text(uid):

    used = get_usage(uid)

    remaining = max(
        0,
        MAX_AI_MESSAGES - used
    )

    return (
        "🤖 سهمیه هوش مصنوعی\n\n"
        f"📨 مصرف‌شده: {used}/{MAX_AI_MESSAGES}\n"
        f"🟢 باقی‌مانده: {remaining}\n\n"
        "⏱️ محدودیت: ۶ پیام در هر ۴ ساعت"
    )


# =========================================================
# 🧠 AI
# =========================================================

async def ask_ai(
    uid,
    user_text
):

    if not can_use_ai(uid):

        return (
            "⛔ سهمیه شما تمام شده.\n\n"
            "در هر ۴ ساعت حداکثر "
            "۶ پیام هوش مصنوعی مجاز است."
        )

    model = get_model(uid)

    history = get_history(uid)

    messages = [
        {
            "role": "system",
            "content": (
                "تو یک دستیار هوش مصنوعی فارسی هستی. "
                "طبیعی، دقیق و دوستانه پاسخ بده. "
                "اگر کاربر فارسی صحبت کرد، فارسی جواب بده."
            )
        }
    ]

    messages.extend(history)

    messages.append({
        "role": "user",
        "content": user_text
    })

    # قبل از درخواست واقعی
    add_usage(uid)

    try:

        response = await asyncio.to_thread(
            ai.chat.completions.create,
            model=model,
            messages=messages,
            temperature=0.7,
            max_tokens=2000
        )

        answer = response.choices[0].message.content

        if not answer:
            answer = "❌ مدل پاسخی برنگرداند."

        save_message(
            uid,
            "user",
            user_text
        )

        save_message(
            uid,
            "assistant",
            answer
        )

        return answer

    except Exception as e:

        # درخواست شکست خورد → سهمیه برگردد
        remove_last_usage(uid)

        print(
            "GAPGPT ERROR:",
            repr(e)
        )

        return (
            "❌ اتصال به هوش مصنوعی با مشکل مواجه شد.\n\n"
            "مدل یا کلید GapGPT را بررسی کن."
        )


# =========================================================
# 🪟 منوی اصلی
# =========================================================

def main_menu():
    """منوی مرتب و جمع‌وجور با دکمه‌های دو ستونه."""
    keyboard = InlineKeyboardMarkup()
    buttons = [
        ("✨ شروع گفتگو با هوش مصنوعی", "ai", 1),
        ("🧠 حافظه گفتگو", "memory", 1),
        ("📰 آخرین اخبار", "news", 2),
        ("🌤️ آب‌وهوا", "weather", 2),
        ("🔎 جستجوی وب", "search", 3),
        ("📊 سهمیه من", "usage", 3),
        ("⚙️ تنظیمات مدل", "settings", 4),
    ]
    for label, data, row in buttons:
        keyboard.add(InlineKeyboardButton(text=label, callback_data=data), row=row)
    return keyboard


# =========================================================
# ⚙️ منوی تنظیمات
# =========================================================

def settings_menu():

    keyboard = InlineKeyboardMarkup()

    keyboard.add(
        InlineKeyboardButton(
            text="🤖 GPT-4o Mini",
            callback_data="model:gpt-4o-mini"
        ),
        row=1
    )

    keyboard.add(
        InlineKeyboardButton(
            text="🧠 DeepSeek",
            callback_data="model:deepseek-chat"
        ),
        row=2
    )

    keyboard.add(
        InlineKeyboardButton(
            text="🔙 بازگشت",
            callback_data="home"
        ),
        row=3
    )

    return keyboard


# =========================================================
# 👑 ADMIN
# =========================================================

def is_admin(uid):
    # پشتیبانی از شناسه عددی (روش مطمئن) و نام کاربری ثبت‌شده.
    if str(uid) in {str(x) for x in ADMIN_IDS}:
        return True
    row = db.execute("SELECT username FROM users WHERE user_id = ?", (str(uid),)).fetchone()
    username = (row["username"] or "").lstrip("@").lower() if row else ""
    return username in {str(x).lstrip("@").lower() for x in ADMIN_IDS}


def admin_menu():

    keyboard = InlineKeyboardMarkup()

    keyboard.add(
        InlineKeyboardButton(
            text="📊 آمار",
            callback_data="admin_stats"
        ),
        row=1
    )

    keyboard.add(
        InlineKeyboardButton(
            text="📢 ارسال همگانی",
            callback_data="admin_broadcast"
        ),
        row=2
    )

    keyboard.add(
        InlineKeyboardButton(
            text="🔙 بازگشت",
            callback_data="home"
        ),
        row=3
    )

    return keyboard


# =========================================================
# 📊 STATS
# =========================================================

def get_stats():

    users = db.execute("""
    SELECT COUNT(*) AS c
    FROM users
    """).fetchone()["c"]

    messages = db.execute("""
    SELECT COUNT(*) AS c
    FROM messages
    """).fetchone()["c"]

    ai_today = db.execute("""
    SELECT COUNT(*) AS c
    FROM usage
    WHERE created_at >= ?
    """, (
        now() - 86400,
    )).fetchone()["c"]

    active = db.execute("""
    SELECT COUNT(*) AS c
    FROM users
    WHERE last_seen >= ?
    """, (
        now() - 86400,
    )).fetchone()["c"]

    return (
        "📊 آمار ربات\n\n"
        f"👥 کل کاربران: {users}\n"
        f"💬 کل پیام‌های AI: {messages}\n"
        f"🤖 درخواست AI در ۲۴ ساعت: {ai_today}\n"
        f"🟢 کاربران فعال ۲۴ ساعت: {active}"
    )


# =========================================================
# 📰 NEWS
# =========================================================

NEWS_SOURCES = [

    (
        "ایسنا",
        "https://www.isna.ir/rss"
    ),

    (
        "مهر",
        "https://www.mehrnews.com/rss"
    ),

    (
        "خبرآنلاین",
        "https://www.khabaronline.ir/rss"
    ),

    (
        "عصر ایران",
        "https://www.asriran.com/fa/rss/allnews"
    ),

    (
        "تابناک",
        "https://www.tabnak.ir/fa/rss/allnews"
    ),

    (
        "باشگاه خبرنگاران",
        "https://www.yjc.ir/fa/rss/allnews"
    ),

    (
        "ایرنا",
        "https://www.irna.ir/rss"
    ),

    (
        "انتخاب",
        "https://www.entekhab.ir/fa/rss/allnews"
    ),

    (
        "ایلنا",
        "https://www.ilna.ir/rss"
    ),

    (
        "تجارت نیوز",
        "https://tejaratnews.com/feed"
    ),

    (
        "زومیت",
        "https://www.zoomit.ir/feed/"
    ),

    (
        "دیجیاتو",
        "https://digiato.com/feed"
    ),

    (
        "گجت نیوز",
        "https://gadgetnews.net/feed/"
    ),

    (
        "ورزش سه",
        "https://www.varzesh3.com/rss"
    ),

    (
        "اقتصاد آنلاین",
        "https://www.eghtesadonline.com/fa/rss"
    ),

]


def clean_html(text):

    if not text:
        return ""

    text = html.unescape(text)

    text = re.sub(
        r"<[^>]*>",
        "",
        text
    )

    return text.strip()


def get_news():

    results = []

    seen = set()

    for source, url in NEWS_SOURCES:

        try:

            r = requests.get(
                url,
                timeout=8,
                headers={
                    "User-Agent":
                    "Mozilla/5.0"
                }
            )

            if r.status_code != 200:
                continue

            feed = feedparser.parse(
                r.content
            )

            for item in feed.entries[:6]:

                title = clean_html(
                    getattr(
                        item,
                        "title",
                        ""
                    )
                )

                link = getattr(
                    item,
                    "link",
                    ""
                )

                if not title:
                    continue

                key = link or title

                if key in seen:
                    continue

                seen.add(key)

                results.append(
                    (
                        title,
                        link,
                        source
                    )
                )

                if len(results) >= 12:
                    return results

        except Exception as e:

            print(
                "NEWS ERROR:",
                source,
                repr(e)
            )

    return results


async def news_text():

    news = await asyncio.to_thread(
        get_news
    )

    if not news:

        return (
            "❌ فعلاً خبری دریافت نشد."
        )

    result = "📰 آخرین اخبار\n\n"

    for i, (title, link, source) in enumerate(
        news,
        1
    ):

        result += (
            f"{i}. {title}\n"
            f"🗞 منبع: {source}\n"
        )

        if link:
            result += f"🔗 {link}\n"

        result += "\n"

    return result


# =========================================================
# 🌤 WEATHER
# =========================================================

def get_weather(city):

    try:

        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={
                "name": city,
                "count": 1,
                "language": "fa",
                "format": "json"
            },
            timeout=10
        ).json()

        places = geo.get(
            "results",
            []
        )

        if not places:
            return "❌ شهر پیدا نشد."

        place = places[0]

        weather = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude":
                    place["latitude"],

                "longitude":
                    place["longitude"],

                "current": (
                    "temperature_2m,"
                    "relative_humidity_2m,"
                    "apparent_temperature,"
                    "wind_speed_10m"
                ),

                "daily": (
                    "temperature_2m_max,"
                    "temperature_2m_min,"
                    "precipitation_probability_max"
                ),

                "forecast_days": 3,

                "timezone": "auto"
            },
            timeout=10
        ).json()

        current = weather.get(
            "current",
            {}
        )

        daily = weather.get(
            "daily",
            {}
        )

        mins = daily.get(
            "temperature_2m_min",
            ["?"]
        )

        maxs = daily.get(
            "temperature_2m_max",
            ["?"]
        )

        rain = daily.get(
            "precipitation_probability_max",
            ["?"]
        )

        return (
            f"🌤️ آب‌وهوای {place.get('name', city)}\n\n"
            f"🌡 دما: "
            f"{current.get('temperature_2m', '?')}°C\n"
            f"🤔 احساس‌شده: "
            f"{current.get('apparent_temperature', '?')}°C\n"
            f"💧 رطوبت: "
            f"{current.get('relative_humidity_2m', '?')}%\n"
            f"💨 باد: "
            f"{current.get('wind_speed_10m', '?')} km/h\n\n"
            f"📅 پیش‌بینی امروز:\n"
            f"{mins[0]}° تا {maxs[0]}°\n"
            f"🌧 احتمال بارش: {rain[0]}%"
        )

    except Exception as e:

        print(
            "WEATHER ERROR:",
            repr(e)
        )

        return (
            "❌ دریافت آب‌وهوا ناموفق بود."
        )


# =========================================================
# 🔎 SEARCH
# =========================================================

def search_web(query):

    try:

        r = requests.get(
            "https://html.duckduckgo.com/html/",
            params={
                "q": query
            },
            headers={
                "User-Agent":
                "Mozilla/5.0"
            },
            timeout=10
        )

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            r.text,
            "html.parser"
        )

        results = []

        for item in soup.select(
            ".result"
        )[:6]:

            a = item.select_one(
                ".result__a"
            )

            snippet = item.select_one(
                ".result__snippet"
            )

            if not a:
                continue

            results.append({
                "title":
                    a.get_text(
                        " ",
                        strip=True
                    ),

                "url":
                    a.get(
                        "href",
                        ""
                    ),

                "snippet":
                    snippet.get_text(
                        " ",
                        strip=True
                    )
                    if snippet else ""
            })

        return results

    except Exception as e:

        print(
            "SEARCH ERROR:",
            repr(e)
        )

        return []


async def search_text(query):

    results = await asyncio.to_thread(
        search_web,
        query
    )

    if not results:

        return "❌ نتیجه‌ای پیدا نشد."

    text = (
        f"🔎 نتایج جستجو برای:\n"
        f"«{query}»\n\n"
    )

    for i, item in enumerate(
        results,
        1
    ):

        text += (
            f"{i}. {item['title']}\n"
            f"{item['snippet']}\n"
            f"🔗 {item['url']}\n\n"
        )

    return text


# =========================================================
# 📢 BROADCAST
# =========================================================

async def broadcast(text):

    users = db.execute("""
    SELECT user_id
    FROM users
    """).fetchall()

    sent = 0
    failed = 0

    for user in users:

        try:

            await bot.send_message(
                user["user_id"],
                text
            )

            sent += 1

            await asyncio.sleep(
                0.1
            )

        except Exception as e:

            print(
                "BROADCAST ERROR:",
                repr(e)
            )

            failed += 1

    return sent, failed


# =========================================================
# 🤖 BOT
# =========================================================

bot = Bot(
    token=BALE_TOKEN
)


# =========================================================
# 🚀 STARTUP
# =========================================================

@bot.event
async def on_before_ready():

    try:
        await bot.delete_webhook()
    except Exception:
        pass


@bot.event
async def on_ready():

    print(
        "================================"
    )

    print(
        "🤖 گرشا AI V3"
    )

    print(
        "✅ Bot is ready!"
    )

    print(
        "================================"
    )


# =========================================================
# ✨ تایپ تدریجی و دکمه‌های واکنش
# =========================================================
def reaction_menu():
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton(text="👍 مفید بود", callback_data="react:up"), row=1)
    keyboard.add(InlineKeyboardButton(text="❤️ دوست داشتم", callback_data="react:love"), row=1)
    keyboard.add(InlineKeyboardButton(text="🔥 عالی بود", callback_data="react:fire"), row=1)
    keyboard.add(InlineKeyboardButton(text="👎 مفید نبود", callback_data="react:down"), row=1)
    return keyboard


async def reveal_answer(waiting, answer):
    """پاسخ را با چند ویرایش سریع نمایش می‌دهد؛ اگر edit پشتیبانی نشود، پاسخ عادی ارسال می‌شود."""
    answer = str(answer or "پاسخی دریافت نشد.")
    limit = 3300
    first = answer[:limit]
    try:
        # گام‌های نسبتاً بزرگ برای حس تایپ بدون کند کردن گفتگو
        step = 180
        for end in range(step, len(first), step):
            await waiting.edit(first[:end] + " ▌")
            await asyncio.sleep(0.045)
        await waiting.edit(first, components=reaction_menu())
    except Exception:
        try:
            await waiting.edit(first)
        except Exception:
            pass
    for start in range(limit, len(answer), limit):
        part = answer[start:start + limit]
        try:
            await bot.send_message(waiting.channel.id, part)
        except Exception:
            # نسخه‌های مختلف کتابخانه ممکن است API ارسال متفاوتی داشته باشند.
            break


# =========================================================
# 💬 MESSAGE
# =========================================================

@bot.event
async def on_message(message: Message):

    try:

        uid = get_uid(message)
        if not uid:
            return

        register_user(message)

        if not uid:
            return

        # مهم:
        # در python-bale-bot متن پیام content است.
        text = message.content

        if not text:
            return

        text = text.strip()

        # =====================================================
        # START
        # =====================================================

        if text == "/start":

            await message.reply(
                "╭━━━ ✦ 𝐆𝐄𝐑𝐒𝐇𝐀 𝐀𝐈 ✦ ━━━╮\n"
                "سلام رفیق! 👋✨\n\n"
                "به دستیار هوشمند گرشا خوش اومدی.\n"
                "اینجا می‌تونی گفتگو کنی، جستجو کنی، خبر بخونی و آب‌وهوا رو ببینی.\n\n"
                "⚡ سریع • 🧠 هوشمند • 💬 دوستانه\n"
                "╰━━━━━━━━━━━━━━━━━━━━╯\n\n"
                "👇 از منوی زیر شروع کن:",
                components=main_menu()
            )

            return


        # =====================================================
        # MENU
        # =====================================================

        if text == "/menu":

            await message.reply(
                "🪟 منوی اصلی:",
                components=main_menu()
            )

            return


        # =====================================================
        # USAGE
        # =====================================================

        if text == "/usage":

            await message.reply(
                usage_text(uid)
            )

            return


        # =====================================================
        # MEMORY
        # =====================================================

        if text == "/memory":

            clear_memory(uid)

            await message.reply(
                "🧹 حافظه گفتگوی شما پاک شد."
            )

            return


        # =====================================================
        # ADMIN
        # =====================================================

        if text == "/admin":

            if not is_admin(uid):

                await message.reply(
                    "⛔ دسترسی ندارید."
                )

                return

            await message.reply(
                "👑 پنل مدیریت",
                components=admin_menu()
            )

            return


        # =====================================================
        # STATS
        # =====================================================

        if text == "/stats":

            if not is_admin(uid):

                await message.reply(
                    "⛔ دسترسی ندارید."
                )

                return

            await message.reply(
                get_stats()
            )

            return


        # =====================================================
        # BROADCAST
        # =====================================================

        if text.startswith("/broadcast"):

            if not is_admin(uid):

                await message.reply(
                    "⛔ دسترسی ندارید."
                )

                return

            broadcast_text = text[
                len("/broadcast"):
            ].strip()

            if not broadcast_text:

                await message.reply(
                    "مثال:\n\n"
                    "/broadcast سلام 👋"
                )

                return

            waiting = await message.reply(
                "📢 ارسال همگانی شروع شد..."
            )

            sent, failed = await broadcast(
                broadcast_text
            )

            try:
                await waiting.delete()
            except Exception:
                pass

            await message.reply(
                "✅ ارسال تمام شد.\n\n"
                f"📨 موفق: {sent}\n"
                f"❌ ناموفق: {failed}"
            )

            return


        # =====================================================
        # WEATHER
        # =====================================================

        if text.startswith("/weather"):

            city = text[
                len("/weather"):
            ].strip()

            if not city:

                await message.reply(
                    "مثال:\n"
                    "/weather تهران"
                )

                return

            waiting = await message.reply(
                "🌤️ در حال دریافت آب‌وهوا..."
            )

            result = await asyncio.to_thread(
                get_weather,
                city
            )

            try:
                await waiting.delete()
            except Exception:
                pass

            await message.reply(
                result
            )

            return


        # =====================================================
        # NEWS
        # =====================================================

        if text == "/news":

            waiting = await message.reply(
                "📰 در حال دریافت اخبار..."
            )

            result = await news_text()

            try:
                await waiting.delete()
            except Exception:
                pass

            await message.reply(
                result
            )

            return


        # =====================================================
        # SEARCH
        # =====================================================

        if text.startswith("/search"):

            query = text[
                len("/search"):
            ].strip()

            if not query:

                await message.reply(
                    "مثال:\n"
                    "/search اخبار فناوری"
                )

                return

            waiting = await message.reply(
                "🔎 در حال جستجو..."
            )

            result = await search_text(
                query
            )

            try:
                await waiting.delete()
            except Exception:
                pass

            await message.reply(
                result
            )

            return


        # =====================================================
        # AI
        # =====================================================

        if not can_use_ai(uid):

            await message.reply(
                usage_text(uid)
                +
                "\n\n⛔ سهمیه فعلی تمام شده."
            )

            return

        # پیام انتظار
        waiting = await message.reply(
            "✦ گرشا داره فکر می‌کنه... ✨\n▌"
        )

        answer = await ask_ai(
            uid,
            text
        )

        # پاسخ به‌صورت تایپ سریع در همان پیام انتظار ظاهر می‌شود.
        await reveal_answer(waiting, answer)

    except Exception as e:

        print(
            "MESSAGE ERROR:",
            repr(e)
        )


# =========================================================
# 🖱️ CALLBACK
# =========================================================

@bot.event
async def on_callback(
    callback: CallbackQuery
):

    try:

        uid = str(
            callback.from_user.id
        )

        data = callback.data or ""

        msg = callback.message

        if data.startswith("react:"):
            labels = {
                "react:up": "👍 ممنون از بازخوردت!",
                "react:love": "❤️ خوشحالم که خوشت اومد!",
                "react:fire": "🔥 دمت گرم، ممنون!",
                "react:down": "🙏 ممنون که گفتی؛ سعی می‌کنم بهتر بشم.",
            }
            try:
                await callback.answer(labels.get(data, "ممنون از بازخوردت!"))
            except Exception:
                pass
            return

        # =====================================================
        # HOME
        # =====================================================

        if data == "home":

            await msg.edit(
                "🪟 منوی اصلی:",
                components=main_menu()
            )

            return


        # =====================================================
        # AI
        # =====================================================

        if data == "ai":

            await msg.edit(
                "🤖 هوش مصنوعی گرشا\n\n"
                "پیامت رو بفرست تا جواب بدم.\n\n"
                + usage_text(uid),
                components=main_menu()
            )

            return


        # =====================================================
        # MEMORY
        # =====================================================

        if data == "memory":

            await msg.edit(
                "🧠 حافظه فعال است.\n\n"
                "گفتگوهای اخیر برای ادامه بهتر مکالمه "
                "ذخیره می‌شوند.\n\n"
                "برای پاک کردن:\n"
                "/memory",
                components=main_menu()
            )

            return


        # =====================================================
        # NEWS
        # =====================================================

        if data == "news":

            await msg.edit(
                "📰 در حال دریافت اخبار..."
            )

            result = await news_text()

            await msg.edit(
                result,
                components=main_menu()
            )

            return


        # =====================================================
        # WEATHER
        # =====================================================

        if data == "weather":

            await msg.edit(
                "🌤️ برای دریافت آب‌وهوا بنویس:\n\n"
                "/weather تهران\n\n"
                "مثلاً:\n"
                "/weather مشهد",
                components=main_menu()
            )

            return


        # =====================================================
        # SEARCH
        # =====================================================

        if data == "search":

            await msg.edit(
                "🔎 برای جستجو بنویس:\n\n"
                "/search آخرین اخبار فناوری",
                components=main_menu()
            )

            return


        # =====================================================
        # USAGE
        # =====================================================

        if data == "usage":

            await msg.edit(
                usage_text(uid),
                components=main_menu()
            )

            return


        # =====================================================
        # SETTINGS
        # =====================================================

        if data == "settings":

            await msg.edit(
                "⚙️ تنظیمات مدل\n\n"
                f"مدل فعلی:\n"
                f"{get_model(uid)}\n\n"
                "یک مدل انتخاب کن:",
                components=settings_menu()
            )

            return


        # =====================================================
        # MODEL
        # =====================================================

        if data.startswith("model:"):

            model = data.split(
                ":",
                1
            )[1]

            set_model(
                uid,
                model
            )

            await msg.edit(
                "✅ مدل تغییر کرد.\n\n"
                f"مدل فعلی:\n{model}",
                components=main_menu()
            )

            return


        # =====================================================
        # ADMIN STATS
        # =====================================================

        if data == "admin_stats":

            if not is_admin(uid):
                return

            await msg.edit(
                get_stats(),
                components=admin_menu()
            )

            return


        # =====================================================
        # ADMIN BROADCAST
        # =====================================================

        if data == "admin_broadcast":

            if not is_admin(uid):
                return

            await msg.edit(
                "📢 ارسال همگانی\n\n"
                "بنویس:\n\n"
                "/broadcast متن پیام",
                components=admin_menu()
            )

            return

    except Exception as e:

        print(
            "CALLBACK ERROR:",
            repr(e)
        )


# =========================================================
# ▶️ RUN
# =========================================================

print("🚀 Starting GerSha AI — Enhanced Menu (Pydroid 3)...")
try:
    bot.run()
except Exception as exc:
    msg = str(exc)
    if "401" in msg or "Unauthorized" in msg:
        print("\n❌ بله توکن را رد کرد (401 Unauthorized). این خطا با نصب کتابخانه حل نمی‌شود.")
        print("توکن باید از بخش مدیریت ربات‌های خودِ پیام‌رسان بله گرفته شده باشد؛ توکن تلگرام در بله کار نمی‌کند.")
        print("توکن جدید را فقط در config.py وارد کنید و آن را در چت عمومی نفرستید.")
    else:
        print("\n❌ خطای اجرای ربات:", repr(exc))
    raise
