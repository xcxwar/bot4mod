# -*- coding: utf-8 -*-
"""
Khabarino فارسی 🇮🇷📰
- فقط منابع خبری فارسی و ایرانی
- هیچ RSS خارجی وارد سیستم نمی‌شود
- هشتگ‌ها خودکار فارسی و متناسب با موضوع خبر ساخته می‌شوند
- فقط ۱ عکس اصلی و حداکثر ۱ ویدئوی مرتبط قابل انتشار است
- تبلیغات/لوگو/بنرهای مشکوک رسانه‌ای فیلتر می‌شوند
- ارسال رسانه‌ها بدون تأخیر مصنوعی انجام می‌شود
"""

import hashlib
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

import requests
import feedparser
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BALE_BOT_TOKEN", "").strip()
CHANNEL_ID = "@khabar_fory_ir"

POST_DELAY = 0
SCAN_DELAY = 600  # بررسی منابع هر ۱۰ دقیقه
FETCH_WORKERS = 12
SEND_WORKERS = 8  # ارسال موازی خبرهای جدید
MAX_ITEMS_PER_SOURCE = 8
MAX_IMAGES = 1
MAX_MEDIA_PER_NEWS = 2  # حداکثر ۱ عکس اصلی + ۱ ویدئو
REQUEST_TIMEOUT = 10
MEDIA_TIMEOUT = 20
MEDIA_MAX_BYTES = 25 * 1024 * 1024
ALLOW_VIDEO = True
SOURCE_FAIL_LIMIT = 3
DB_FILE = "khabarino.db"
SIGNATURE = "🇮🇷 @KhabarinoNews | بله"

# فقط منابع فارسی؛ منابع انگلیسی/خارجی عمداً حذف شده‌اند.
SOURCES = [
    ("ایسنا", "https://www.isna.ir/rss"),
    ("مهر", "https://www.mehrnews.com/rss"),
    ("تابناک", "https://www.tabnak.ir/fa/rss/allnews"),
    ("خبرآنلاین", "https://www.khabaronline.ir/rss"),
    ("باشگاه خبرنگاران", "https://www.yjc.ir/fa/rss/allnews"),
    ("عصر ایران", "https://www.asriran.com/fa/rss/allnews"),
    ("ایلنا", "https://www.ilna.ir/rss"),
    ("انتخاب", "https://www.entekhab.ir/fa/rss/allnews"),
    ("ایرنا سیاسی", "https://www.irna.ir/rss/tp/1"),
    ("ایرنا اقتصادی", "https://www.irna.ir/rss/tp/2"),
    ("ایرنا اجتماعی", "https://www.irna.ir/rss/tp/3"),
    ("ایرنا ورزشی", "https://www.irna.ir/rss/tp/5"),
    ("ایرنا علم و فناوری", "https://www.irna.ir/rss/tp/6"),
    ("ایرنا فرهنگی", "https://www.irna.ir/rss/tp/7"),
    ("تسنیم", "https://www.tasnimnews.com/fa/rss/feed/0/8/0/%D9%85%D9%87%D9%85%D8%AA%D8%B1%DB%8C%D9%86-%D8%AE%D8%A8%D8%A7%D8%B1-%D8%AA%D8%B3%D9%86%DB%8C%D9%85"),
    ("تسنیم سیاسی", "https://www.tasnimnews.com/fa/rss/feed/0/8/0/%D8%B3%DB%8C%D8%A7%D8%B3%DB%8C"),
    ("تسنیم اقتصادی", "https://www.tasnimnews.com/fa/rss/feed/0/8/0/%D8%A7%D9%82%D8%AA%D8%B5%D8%A7%D8%AF%DB%8C"),
    ("تسنیم اجتماعی", "https://www.tasnimnews.com/fa/rss/feed/0/8/0/%D8%A7%D8%AC%D8%AA%D9%85%D8%A7%D8%B9%DB%8C"),
    ("تسنیم ورزشی", "https://www.tasnimnews.com/fa/rss/feed/0/8/0/%D9%88%D8%B1%D8%B2%D8%B4%DB%8C"),
    ("اقتصاد آنلاین", "https://www.eghtesadonline.com/fa/rss/allnews"),
    ("خبر فوری", "https://www.khabarfoori.com/feed/"),
    ("همشهری", "https://www.hamshahrionline.ir/rss"),
    ("کیهان", "https://kayhan.ir/fa/rss"),
    ("شرق", "https://www.sharghdaily.com/rss"),
    ("میزان", "https://www.mizanonline.ir/fa/rss"),
    ("ورزش سه", "https://www.varzesh3.com/rss"),
    ("طرفداری", "https://www.tarafdari.com/rss.xml"),
    ("فوتبال ۳۶۰", "https://football360.ir/feed/"),
    ("اخبار ورزشی", "https://www.akhbarvarzeshi.com/rss"),
    ("زومیت", "https://www.zoomit.ir/feed/"),
    ("دیجیاتو", "https://digiato.com/feed"),
    ("گجت نیوز", "https://gadgetnews.net/feed/"),
    ("سخت‌افزار", "https://sakhtafzarmag.com/feed/"),
    ("ترنجی", "https://toranji.ir/feed/"),
    ("ایت ایران", "https://itiran.com/feed/"),
]

BLOCKED_PHRASES = [
    "مرگ بر ایران", "مرگ بر ایرانی", "لعنت به ایران", "نابودی ایران",
    "تجزیه ایران", "تجزیه ی ایران", "ایران باید نابود", "ایرانی ها باید نابود",
]

# محتوای کم‌ارزش/غیرخبری که بهتر است وارد کانال نشود.
LOW_QUALITY_PHRASES = [
    "تبلیغ", "آگهی", "کد تخفیف", "خرید کنید", "فروش ویژه", "فروش فوری",
    "فال روز", "طالع بینی", "دانلود رایگان", "قرعه کشی", "مسابقه پیامکی",
    "لینک دانلود", "وضعیت آب و هوا فردا", "عکس های دیدنی", "تصاویر دیدنی",
    "حواشی اینستاگرامی", "چهره ها", "استوری", "لایو اینستاگرام",
]

AD_WORDS = re.compile(
    r"(تبلیغ|ads?|advert|banner|sponsor|logo|لوگو|بنر|تبلیغات|پوستر|" 
    r"پروموشن|promotion|کد تخفیف|خرید|فروش|shop|store)", re.I
)


def clean(text):
    return re.sub(r"\s+", " ", BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)).strip()


def normalize(text):
    return clean(text).lower().replace("ي", "ی").replace("ك", "ک")


def fingerprint(title, summary):
    s = re.sub(r"[^\w\sآ-ی]", " ", normalize(title + " " + summary))
    return hashlib.sha256(re.sub(r"\s+", " ", s).strip().encode()).hexdigest()


def init_db():
    con = sqlite3.connect(DB_FILE)
    con.execute("CREATE TABLE IF NOT EXISTS news (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, url TEXT UNIQUE NOT NULL, title TEXT NOT NULL, fingerprint TEXT UNIQUE NOT NULL, created_at INTEGER NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS source_health (url TEXT PRIMARY KEY, name TEXT NOT NULL, failures INTEGER DEFAULT 0, disabled INTEGER DEFAULT 0, last_error TEXT DEFAULT '')")
    for name, url in SOURCES:
        con.execute("INSERT OR IGNORE INTO source_health(url,name) VALUES(?,?)", (url, name))
    con.commit()
    return con


def active_sources(con):
    return con.execute("SELECT name,url FROM source_health WHERE disabled=0").fetchall()


def mark_source_ok(con, url):
    con.execute("UPDATE source_health SET failures=0,last_error='' WHERE url=?", (url,))
    con.commit()


def mark_source_bad(con, name, url, error):
    row = con.execute("SELECT failures FROM source_health WHERE url=?", (url,)).fetchone()
    failures = (row[0] if row else 0) + 1
    disabled = 1 if failures >= SOURCE_FAIL_LIMIT else 0
    con.execute("""INSERT INTO source_health(url,name,failures,disabled,last_error) VALUES(?,?,?,?,?)
        ON CONFLICT(url) DO UPDATE SET name=excluded.name,failures=excluded.failures,disabled=excluded.disabled,last_error=excluded.last_error""",
        (url, name, failures, disabled, str(error)[:500]))
    con.commit()
    print(f"[SOURCE ERROR {failures}/{SOURCE_FAIL_LIMIT}] {name}: {error}")


def is_duplicate(con, url, fp):
    return con.execute("SELECT 1 FROM news WHERE url=? OR fingerprint=? LIMIT 1", (url, fp)).fetchone() is not None


def similar_to_recent(con, title, limit=250):
    """تکراری‌های بازنویسی‌شده را هم تشخیص می‌دهد، نه فقط URL یکسان را."""
    nt = normalize(title)
    rows = con.execute("SELECT title FROM news ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    for (old_title,) in rows:
        no = normalize(old_title)
        if not no or not nt:
            continue
        if nt == no:
            return True
        ratio = SequenceMatcher(None, nt, no).ratio()
        if ratio >= 0.86:
            return True
        a, b = set(nt.split()), set(no.split())
        if len(a) >= 5 and len(b) >= 5 and len(a & b) / max(1, min(len(a), len(b))) >= 0.82:
            return True
    return False


def save_news(con, item):
    try:
        con.execute("INSERT INTO news(source,url,title,fingerprint,created_at) VALUES(?,?,?,?,?)", (item["source"], item["url"], item["title"], item["fp"], int(time.time())))
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def is_blocked_content(title, summary):
    text = normalize(title + " " + summary)
    if any(normalize(x) in text for x in BLOCKED_PHRASES):
        return True
    if any(normalize(x) in text for x in LOW_QUALITY_PHRASES):
        return True
    # تیترهای خیلی کوتاه/بدون محتوای خبری را کنار می‌گذاریم.
    if len(clean(title)) < 18:
        return True
    return False


def looks_persian(text):
    text = clean(text)
    if not text:
        return False
    fa = len(re.findall(r"[آ-ی]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    # خبرهای کاملاً انگلیسی/خارجی رد می‌شوند؛ نام‌های لاتین کوتاه مشکلی ندارند.
    return fa >= 8 and fa >= max(3, latin * 0.25)


def valid_url(value):
    value = str(value or "").strip()
    return value if value.startswith(("http://", "https://")) else ""


def media_is_ad(url, alt="", classes=""):
    blob = f"{url} {alt} {classes}".lower()
    return bool(AD_WORDS.search(blob))


def add_media(found, url, kind, alt="", classes=""):
    url = valid_url(url)
    if not url or media_is_ad(url, alt, classes):
        return
    if kind == "image" and not re.search(r"\.(jpe?g|png|webp|gif)(?:$|[?#])", url, re.I):
        # URLهای بدون پسوند هم می‌توانند تصویر باشند؛ در ادامه دانلود تشخیص داده می‌شود.
        pass
    key = url.split("#")[0]
    if any(x["url"].split("#")[0] == key for x in found):
        return
    found.append({"url": url, "type": kind})


def extract_media(entry, article_url):
    """فقط یک عکس اصلی + یک ویدئوی مرتبط را برمی‌گرداند.
    برای جلوگیری از ارسال بنر/لوگو/عکس‌های تکراری، ابتدا تصویر اصلی OG و سپس
    رسانه‌های RSS بررسی می‌شوند و در نهایت فقط یک مورد از هر نوع انتخاب می‌شود.
    """
    images = []
    videos = []

    def add_unique(target, url, alt="", classes=""):
        url = valid_url(url)
        if not url or media_is_ad(url, alt, classes):
            return
        key = url.split("#")[0].split("?")[0].rstrip("/").lower()
        if any(x.split("#")[0].split("?")[0].rstrip("/").lower() == key for x in target):
            return
        target.append(url)

    # 1) ابتدا صفحه خبر را بررسی می‌کنیم تا OG:image (معمولاً تصویر اصلی خبر) اولویت داشته باشد.
    try:
        r = requests.get(article_url, headers={"User-Agent": "Khabarino/2.0"}, timeout=MEDIA_TIMEOUT)
        r.raise_for_status()
        if "text/html" in (r.headers.get("content-type") or "").lower():
            soup = BeautifulSoup(r.text, "html.parser")

            # تصویر اصلی
            for tag in soup.find_all("meta"):
                prop = str(tag.get("property") or tag.get("name") or "").lower()
                u = tag.get("content")
                if prop in ("og:image", "og:image:url", "twitter:image"):
                    add_unique(images, u)

            # ویدئو
            for tag in soup.find_all("meta"):
                prop = str(tag.get("property") or tag.get("name") or "").lower()
                u = tag.get("content")
                if "video" in prop:
                    add_unique(videos, u)

            # اگر OG پیدا نشد، اولین تصویر واقعی صفحه را به عنوان تصویر اصلی در نظر بگیر.
            if not images:
                for tag in soup.find_all("img"):
                    u = tag.get("src") or tag.get("data-src") or tag.get("data-original")
                    add_unique(images, u, tag.get("alt", ""), " ".join(tag.get("class", [])))
                    if images:
                        break

            # اگر ویدئو از متا پیدا نشد، اولین ویدئوی واقعی صفحه را بگیر.
            if not videos:
                for tag in soup.find_all(["video", "source"]):
                    u = tag.get("src") or tag.get("data-src") or tag.get("data-original")
                    add_unique(videos, u, tag.get("title", ""), " ".join(tag.get("class", [])))
                    if videos:
                        break
    except Exception:
        pass

    # 2) اگر صفحه رسانه اصلی را نداد، RSS را به عنوان fallback بررسی کن.
    if not images or not videos:
        for key in ("media_content", "media_thumbnail", "enclosures"):
            values = getattr(entry, key, None) or []
            if not isinstance(values, list):
                values = [values]
            for item in values:
                if isinstance(item, dict):
                    u = item.get("url") or item.get("href")
                    mime = str(item.get("type") or item.get("mime_type") or "").lower()
                else:
                    u = getattr(item, "url", "") or getattr(item, "href", "")
                    mime = str(getattr(item, "type", "") or "").lower()
                if mime.startswith("video/") or re.search(r"\.(mp4|webm|mov)(?:$|[?#])", str(u), re.I):
                    if not videos:
                        add_unique(videos, u)
                else:
                    if not images:
                        add_unique(images, u)

                if images and videos:
                    break
            if images and videos:
                break

    result = []
    if images:
        result.append({"url": images[0], "type": "image"})
    if ALLOW_VIDEO and videos:
        result.append({"url": videos[0], "type": "video"})
    return result


def category_hashtags(item):
    text = normalize(item["title"] + " " + item.get("summary", ""))
    tags = []
    groups = [
        (("فوتبال", "والیبال", "بسکتبال", "ورزش", "لیگ", "جام جهانی", "المپیک", "کشتی", "تیم ملی", "بازیکن"), ["ورزش"]),
        (("اقتصاد", "بورس", "بانک", "دلار", "یورو", "طلا", "سکه", "بازار", "تورم", "قیمت"), ["اقتصاد"]),
        (("فناوری", "تکنولوژی", "موبایل", "گوشی", "هوش مصنوعی", "اینترنت", "نرم افزار", "سخت افزار"), ["فناوری"]),
        (("سیاسی", "دولت", "مجلس", "وزیر", "رئیس جمهور", "انتخابات", "سیاست"), ["سیاسی"]),
        (("حوادث", "تصادف", "آتش سوزی", "زلزله", "حادثه", "کشف"), ["حوادث"]),
        (("سلامت", "پزشکی", "بیمار", "درمان", "دارو", "بیماری"), ["سلامت"]),
        (("فرهنگ", "هنر", "سینما", "تلویزیون", "موسیقی", "کتاب", "بازیگر"), ["فرهنگ"]),
        (("علم", "دانش", "فضا", "نجوم", "پژوهش"), ["علم"]),
        (("خودرو", "ماشین", "خودروساز", "خودروهای"), ["خودرو"]),
    ]
    for words, out in groups:
        if any(w in text for w in words):
            tags.extend(out)
    if not tags:
        tags.append("اخبار")
    if "اخبار" not in tags:
        tags.append("اخبار")
    return " ".join("#" + x for x in tags[:3])


def fetch_one(source):
    name, url = source
    try:
        r = requests.get(url, headers={"User-Agent": "Khabarino/2.0"}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
        if not feed.entries:
            raise RuntimeError("RSS/Atom feed has no readable entries")
        items = []
        for entry in feed.entries[:MAX_ITEMS_PER_SOURCE]:
            title = clean(getattr(entry, "title", ""))
            summary = clean(getattr(entry, "summary", "") or getattr(entry, "description", ""))
            link = valid_url(getattr(entry, "link", ""))
            if not title or not link or not looks_persian(title + " " + summary):
                continue
            if is_blocked_content(title, summary):
                continue
            items.append({
                "source": name,
                "title": title,
                "summary": summary,
                "url": link,
                "fp": fingerprint(title, summary),
                "media": extract_media(entry, link),
            })
        return items, None
    except Exception as e:
        return [], e


def make_message(item):
    body = item["title"]
    if item["summary"]:
        body += "\n\n" + item["summary"]
    body = body[:3900].strip()
    return f"♦️ {body}\n\n{category_hashtags(item)}\n\n📰 منبع: {item['source']}\n{SIGNATURE}"


def make_caption(item):
    body = item["title"]
    if item["summary"]:
        body += "\n\n" + item["summary"]
    body = body[:850].strip()
    return f"♦️ {body}\n\n{category_hashtags(item)}\n\n📰 {item['source']}\n{SIGNATURE}"


def bale_request(method, payload):
    if not BOT_TOKEN or BOT_TOKEN == "توکن_ربات_اینجا":
        raise RuntimeError("BOT_TOKEN را داخل bot.py وارد کن.")
    r = requests.post(f"https://tapi.bale.ai/bot{BOT_TOKEN}/{method}", json=payload, timeout=20)
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"پاسخ نامعتبر بله: HTTP {r.status_code}")
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Bale API error"))
    return data


def send_to_channel(text):
    return bale_request("sendMessage", {"chat_id": CHANNEL_ID, "text": text})


def upload_media(method, field, filename, data, content_type, caption=""):
    r = requests.post(
        f"https://tapi.bale.ai/bot{BOT_TOKEN}/{method}",
        data={"chat_id": CHANNEL_ID, "caption": caption} if caption else {"chat_id": CHANNEL_ID},
        files={field: (filename, data, content_type)}, timeout=MEDIA_TIMEOUT
    )
    try:
        result = r.json()
    except Exception:
        raise RuntimeError(f"پاسخ نامعتبر بله: HTTP {r.status_code}")
    if not result.get("ok"):
        raise RuntimeError(result.get("description", f"Bale {method} error"))
    return result


def download_media(url):
    r = requests.get(url, headers={"User-Agent": "Khabarino/2.0"}, timeout=MEDIA_TIMEOUT, stream=True)
    r.raise_for_status()
    ctype = (r.headers.get("content-type") or "").lower().split(";")[0].strip()
    data = bytearray()
    for chunk in r.iter_content(64 * 1024):
        if chunk:
            data.extend(chunk)
            if len(data) > MEDIA_MAX_BYTES:
                raise RuntimeError("media too large")
    if not data:
        raise RuntimeError("empty media")
    return bytes(data), ctype


def send_news(item):
    """فقط یک عکس اصلی و یک ویدئوی مرتبط.
    اولین رسانه کپشن کامل خبر را دارد؛ رسانه دوم بدون تکرار کپشن ارسال می‌شود.
    اگر هیچ رسانه‌ای قابل ارسال نباشد، فقط متن خبر ارسال می‌شود.
    """
    media = item.get("media", [])[:MAX_MEDIA_PER_NEWS]
    media_sent = 0

    for idx, media_item in enumerate(media):
        try:
            data, ctype = download_media(media_item["url"])
            caption = make_caption(item) if idx == 0 else ""

            if media_item["type"] == "video" or ctype.startswith("video/"):
                if not ALLOW_VIDEO:
                    continue
                upload_media(
                    "sendVideo", "video", "khabarino.mp4", data,
                    ctype or "video/mp4", caption
                )
            elif media_item["type"] == "image" or ctype.startswith("image/"):
                ext = ".png" if "png" in ctype else ".webp" if "webp" in ctype else ".jpg"
                upload_media(
                    "sendPhoto", "photo", "khabarino" + ext, data,
                    ctype or "image/jpeg", caption
                )
            else:
                continue
            media_sent += 1
        except Exception as e:
            print(f"[MEDIA SKIP] {media_item['url']}: {e}")

    if media_sent == 0:
        send_to_channel(make_message(item))

    return media_sent


def collect_candidates(con):
    candidates = []
    srcs = active_sources(con)
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        jobs = {pool.submit(fetch_one, src): src for src in srcs}
        for future in as_completed(jobs):
            name, url = jobs[future]
            try:
                items, error = future.result()
                if error:
                    mark_source_bad(con, name, url, error)
                    continue
                mark_source_ok(con, url)
                for item in items:
                    if not is_duplicate(con, item["url"], item["fp"]) and not similar_to_recent(con, item["title"]):
                        candidates.append(item)
            except Exception as e:
                mark_source_bad(con, name, url, e)
    unique, seen = [], set()
    for item in candidates:
        if item["url"] in seen or item["fp"] in seen:
            continue
        seen.add(item["url"]); seen.add(item["fp"]); unique.append(item)
    return unique


def main():
    if BOT_TOKEN == "توکن_ربات_اینجا":
        print("[ERROR] BOT_TOKEN را وارد کن.")
        return
    con = init_db()
    print("=" * 60)
    print("Khabarino فارسی 🇮🇷📰")
    print(f"منابع فارسی: {len(SOURCES)} | بررسی هر ۱۰ دقیقه | ارسال موازی با {SEND_WORKERS} کارگر")
    print("ویدئو: روشن | رسانه هر خبر: حداکثر ۱ عکس اصلی + ۱ ویدئو")
    print("=" * 60)
    try:
        while True:
            start = time.time()
            try:
                candidates = collect_candidates(con)
                print(f"[SCAN] {len(candidates)} خبر جدید")
                # خبرهای تازه به‌صورت موازی ارسال می‌شوند؛ عملیات دیتابیس فقط در ترد اصلی است.
                with ThreadPoolExecutor(max_workers=SEND_WORKERS) as pool:
                    jobs = {pool.submit(send_news, item): item for item in candidates}
                    for future in as_completed(jobs):
                        item = jobs[future]
                        try:
                            media_count = future.result()
                            save_news(con, item)
                            print(f"[OK] {item['title']} | media={media_count}")
                        except Exception as e:
                            print(f"[SEND ERROR] {item['source']}: {e}")
                elapsed = round(time.time() - start, 1)
                time.sleep(max(1, SCAN_DELAY - elapsed))
            except Exception as e:
                print(f"[LOOP ERROR] {e}")
                time.sleep(10)
    except KeyboardInterrupt:
        print("\nKhabarino stopped.")
    finally:
        con.close()


if __name__ == "__main__":
    main()
