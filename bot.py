# -*- coding: utf-8 -*-
"""
Bale Video Downloader — ربات دانلود ویدیو برای بله
اجرا در Pydroid 3

نصب در ترمینال Pydroid:
pip install requests yt-dlp requests-toolbelt

توکن ربات را در BOT_TOKEN قرار دهید.
برای ارسال ویدیو، API بله محدودیت حجم دارد؛ این نسخه فایل‌های بزرگ‌تر از
MAX_UPLOAD_MB را ارسال نمی‌کند. فقط YouTube، Instagram، TikTok و Pinterest پشتیبانی می‌شوند.
"""

import os
import re
import time
import json
import shutil
import logging
import threading
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
import yt_dlp

# -------------------- تنظیمات --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_BASE = "https://tapi.bale.ai/bot" + BOT_TOKEN
WORK_DIR = Path(__file__).resolve().parent / "linkdownloader_files"
WORK_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_MB = 49
MAX_DOWNLOAD_MB = 500
ALLOWED_HOSTS = (
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "instagram.com", "tiktok.com", "pinterest.com", "pin.it",
)
POLL_TIMEOUT = 25
REQUEST_TIMEOUT = 30
USER_COOLDOWN_SECONDS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("LinkDownloader")

session = requests.Session()
pending = {}        # chat_id -> اطلاعات لینک و فرمت‌ها
last_request = {}   # chat_id -> timestamp
active_downloads = set()
progress_context = {}  # chat_id -> progress message_id
progress_last_update = {}
cancel_requested = set()
state_lock = threading.Lock()


# -------------------- API بله --------------------
def api(method, data=None, files=None, timeout=REQUEST_TIMEOUT):
    url = f"{API_BASE}/{method}"
    response = session.post(url, data=data or {}, files=files, timeout=timeout)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("description", f"خطا در {method}"))
    return result.get("result")


def send_message(chat_id, text, keyboard=None):
    data = {"chat_id": str(chat_id), "text": text}
    if keyboard:
        data["reply_markup"] = json.dumps(
            {"inline_keyboard": keyboard}, ensure_ascii=False
        )
    return api("sendMessage", data=data)


def delete_message(chat_id, message_id):
    if message_id is None:
        return False
    try:
        api("deleteMessage", data={"chat_id": str(chat_id), "message_id": str(message_id)})
        return True
    except Exception as exc:
        log.debug("deleteMessage failed: %s", exc)
        return False


def react_eyes(chat_id, message_id):
    """Best effort; availability depends on the Bale Bot API version."""
    if message_id is None:
        return False
    try:
        api("setMessageReaction", data={
            "chat_id": str(chat_id),
            "message_id": str(message_id),
            "reaction": json.dumps([{"type": "emoji", "emoji": "👀"}], ensure_ascii=False)
        })
        return True
    except Exception as exc:
        log.debug("👀 reaction unsupported by this Bale API: %s", exc)
        return False


def send_media_preview(chat_id, thumbnail_url, caption, keyboard=None):
    """Send thumbnail and metadata in a single photo message when supported."""
    if thumbnail_url:
        try:
            data = {"chat_id": str(chat_id), "photo": thumbnail_url, "caption": caption[:1000]}
            if keyboard:
                data["reply_markup"] = json.dumps(
                    {"inline_keyboard": keyboard}, ensure_ascii=False
                )
            return api("sendPhoto", data=data)
        except Exception as exc:
            log.info("Photo preview unavailable; using text fallback: %s", exc)
    return send_message(chat_id, caption, keyboard)


def edit_message(chat_id, message_id, text, keyboard=None):
    data = {
        "chat_id": str(chat_id),
        "message_id": str(message_id),
        "text": text
    }
    if keyboard:
        data["reply_markup"] = json.dumps({"inline_keyboard": keyboard}, ensure_ascii=False)
    try:
        return api("editMessageText", data=data)
    except Exception as exc:
        log.debug("editMessageText failed: %s", exc)
        return None


def progress_bar(percent, width=12):
    percent = max(0, min(100, int(percent)))
    filled = int(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def progress_text(stage, percent=None, downloaded=None, total=None, speed=None, eta=None):
    lines = [f"در حال انجام: {stage}"]
    if percent is not None:
        lines.append(f"{progress_bar(percent)}  {percent:.1f}%")
    if downloaded is not None:
        def mb(value):
            return f"{value / (1024 * 1024):.2f} MB"
        if total:
            lines.append(f"حجم: {mb(downloaded)} از {mb(total)}")
        else:
            lines.append(f"دریافت‌شده: {mb(downloaded)}")
    if speed:
        lines.append(f"سرعت: {speed}")
    if eta:
        lines.append(f"زمان باقی‌مانده: حدود {eta} ثانیه")
    lines.append("Bale Video Downloader")
    return "\n".join(lines)


def make_progress_message(chat_id, initial_text):
    try:
        result = send_message(chat_id, initial_text, [[
            {"text": "لغو", "callback_data": f"cancel:{chat_id}"}
        ]])
        if isinstance(result, dict):
            return result.get("message_id")
    except Exception:
        log.exception("Could not create progress message")
    return None


def update_progress(chat_id, message_id, text):
    if message_id is not None:
        edit_message(chat_id, message_id, text, [[
            {"text": "لغو", "callback_data": f"cancel:{chat_id}"}
        ]])


def answer_callback(callback_id, text=""):
    try:
        api("answerCallbackQuery", {
            "callback_query_id": str(callback_id),
            "text": text[:180]
        })
    except Exception as exc:
        log.debug("answerCallbackQuery: %s", exc)


def send_file(chat_id, path, caption="دانلود با موفقیت انجام شد", progress_message_id=None):
    path = Path(path)
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        send_message(
            chat_id,
            f"فایل دانلود شد اما حجم آن {size_mb:.1f} مگابایت است و از حد "
            f"ارسال تنظیم‌شدهٔ این ربات ({MAX_UPLOAD_MB} مگابایت) بیشتر است.\n"
            "کیفیت پایین‌تری انتخاب کن یا لینک دیگری بفرست."
        )
        return False

    if progress_message_id is not None:
        update_progress(chat_id, progress_message_id,
                        f"آمادهٔ ارسال به بله\nحجم فایل: {size_mb:.2f} MB\n"
                        "در حال بارگذاری فایل…\nلطفاً صبر کن.")

    suffix = path.suffix.lower()
    method = "sendVideo" if suffix in {".mp4", ".m4v", ".webm", ".mov"} else (
        "sendAudio" if suffix in {".mp3", ".m4a", ".aac", ".ogg", ".wav", ".opus"}
        else "sendDocument"
    )
    field = {"sendVideo": "video", "sendAudio": "audio",
             "sendDocument": "document"}[method]
    # برای نمایش درصد واقعی آپلود، requests-toolbelt لازم است.
    # اگر نصب نباشد، آپلود عادی انجام می‌شود و فقط مرحلهٔ ارسال نمایش داده می‌شود.
    try:
        from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
        with path.open("rb") as file_obj:
            encoder = MultipartEncoder(fields={
                "chat_id": str(chat_id),
                "caption": caption[:900],
                field: (path.name, file_obj, "application/octet-stream"),
            })
            last_ui_update = [0.0]

            def upload_callback(monitor):
                now = time.time()
                if now - last_ui_update[0] < 1.5 and monitor.bytes_read < monitor.len:
                    return
                last_ui_update[0] = now
                percent = monitor.bytes_read * 100 / max(1, monitor.len)
                update_progress(
                    chat_id, progress_message_id,
                    progress_text("در حال ارسال فایل به بله", percent,
                                  monitor.bytes_read, monitor.len)
                )

            monitor = MultipartEncoderMonitor(encoder, upload_callback)
            response = session.post(
                f"{API_BASE}/{method}",
                data=monitor,
                headers={"Content-Type": monitor.content_type},
                timeout=240
            )
            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError(result.get("description", "ارسال فایل به بله ناموفق بود"))
    except ImportError:
        with path.open("rb") as file_obj:
            api(method, data={
                "chat_id": str(chat_id),
                "caption": caption[:900]
            }, files={field: (path.name, file_obj)}, timeout=240)
    return True


# -------------------- اعتبارسنجی لینک --------------------
def platform_for_url(url):
    """Return the supported platform for an allowlisted URL, otherwise None."""
    try:
        parsed = urlparse((url or "").strip())
        host = (parsed.hostname or "").lower().rstrip(".")
    except (ValueError, AttributeError):
        return None
    if parsed.scheme not in ("http", "https") or not host:
        return None
    if host == "youtu.be" or host == "youtube-nocookie.com" or host.endswith(".youtube-nocookie.com") or host == "youtube.com" or host.endswith(".youtube.com"):
        return "youtube"
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "instagram"
    if host == "tiktok.com" or host.endswith(".tiktok.com"):
        return "tiktok"
    if host == "pinterest.com" or host.endswith(".pinterest.com") or host == "pin.it":
        return "pinterest"
    return None


def validate_url(url):
    """Validate URL syntax and strictly allow only the four supported platforms."""
    url = (url or "").strip()
    if not url or len(url) > 2048:
        return False, "لینک خالی است یا بیش از حد طولانی است."
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        # Accessing .port also validates malformed ports.
        port = parsed.port
    except ValueError:
        return False, "ساختار لینک معتبر نیست."
    if parsed.scheme not in ("http", "https") or not host:
        return False, "لطفاً لینک یکی از YouTube، Instagram، TikTok یا Pinterest را بفرست."
    if parsed.username or parsed.password:
        return False, "لینک‌های دارای نام کاربری یا رمز پذیرفته نمی‌شوند."
    platform = platform_for_url(url)
    if not platform:
        return False, "این سایت پشتیبانی نمی‌شود؛ فقط YouTube، Instagram، TikTok و Pinterest مجاز هستند."
    if port not in (None, 80, 443):
        return False, "پورت سفارشی در لینک پذیرفته نمی‌شود."
    return True, url


# -------------------- استخراج کیفیت‌های واقعی --------------------
def inspect_media(url):
    options = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "noplaylist": True, "socket_timeout": 25, "retries": 2,
        "extractor_retries": 2,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    # Include video-only formats too; separate audio/video may require FFmpeg.
    by_height = {}
    for item in info.get("formats") or []:
        if item.get("vcodec") in (None, "none") or not item.get("height"):
            continue
        ext = (item.get("ext") or "").lower()
        if ext not in ("mp4", "webm", "mov", "m4v"):
            continue
        height = int(item["height"])
        score = (
            1 if item.get("acodec") not in (None, "none") else 0,
            item.get("tbr") or 0,
            item.get("filesize") or item.get("filesize_approx") or 0
        )
        current = by_height.get(height)
        if current is None or score > current["_score"]:
            by_height[height] = {
                "height": height, "ext": ext,
                "filesize": item.get("filesize") or item.get("filesize_approx"),
                "format_id": item.get("format_id"),
                "has_audio": item.get("acodec") not in (None, "none"),
                "tbr": item.get("tbr") or 0, "_score": score
            }

    heights = sorted(by_height, reverse=True)
    if not heights:
        raise RuntimeError("این لینک کیفیت ویدیویی قابل‌دسترسی ندارد.")

    # Up to four real resolutions, not four invented labels.
    choices = []
    for height in heights[:4]:
        item = by_height[height]
        item.pop("_score", None)
        item["format_selector"] = (
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/best"
        )
        choices.append(item)
    return info, choices


def size_text(value):
    if not value:
        return "حجم نامشخص"
    return f"{value / (1024 * 1024):.1f} MB"


def quality_keyboard(chat_id, choices):
    rows = []
    row = []
    for i, fmt in enumerate(choices):
        label = f'{fmt["height"]}p'
        if fmt["height"] == 720:
            label += " HD"
        elif fmt["height"] >= 1080:
            label += " Full HD"
        if fmt.get("filesize"):
            label += f' · {size_text(fmt["filesize"])}'
        row.append({"text": label, "callback_data": f'quality:{chat_id}:{i}'})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "لغو", "callback_data": f"cancel:{chat_id}"}])
    return rows


# -------------------- دانلود --------------------
def is_youtube_url(url):
    """مسیر دانلود فعلی یوتیوب را جدا نگه می‌دارد تا منطق سالم آن تغییر نکند."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com") or host == "youtube-nocookie.com" or host.endswith(".youtube-nocookie.com")


def safe_filename(name, fallback="download"):
    name = unquote(str(name or fallback))
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name)
    name = name.strip(" .")
    return (name or fallback)[:120]


def download_with_ytdlp(chat_id, url, fmt=None, title=""):
    """
    دانلود با استخراج تازه در لحظهٔ دانلود.
    شناسه‌های فرمت ممکن است بین مرحلهٔ بررسی و دانلود تغییر کنند؛ بنابراین
    به‌جای تکیه بر URL/format_id قدیمی، دوباره اطلاعات منبع خوانده می‌شود.
    """
    prefix = f"{chat_id}_{int(time.time())}_"
    outtmpl = str(WORK_DIR / (prefix + "%(title).80s.%(ext)s"))

    # یک دور عادی و سپس تلاش با کلاینت‌های جایگزین یوتیوب.
    attempts = [
        {},
        {"extractor_args": {"youtube": {"player_client": ["android", "web"]}}},
    ]
    last_error = None
    progress_message_id = progress_context.get(chat_id)

    def hook(status):
        if chat_id in cancel_requested:
            raise yt_dlp.utils.DownloadError("دانلود به درخواست کاربر لغو شد.")
        if status.get("status") == "downloading":
            now = time.time()
            previous = progress_last_update.get(chat_id, 0)
            if now - previous < 2.0:
                return
            progress_last_update[chat_id] = now
            downloaded = status.get("downloaded_bytes") or 0
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            percent = (downloaded / total * 100) if total else None
            speed_value = status.get("speed")
            speed = f"{speed_value / (1024 * 1024):.2f} MB/s" if speed_value else None
            eta = status.get("eta")
            update_progress(chat_id, progress_message_id,
                            progress_text("در حال دانلود ویدیو", percent, downloaded, total, speed, eta))
        elif status.get("status") == "finished":
            update_progress(chat_id, progress_message_id,
                            "✅ دانلود کامل شد.\n🔧 در حال آماده‌سازی فایل برای ارسال…")

    for extra in attempts:
        try:
            common = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "socket_timeout": 25,
                "retries": 3,
                "fragment_retries": 3,
                "file_access_retries": 2,
                "max_filesize": MAX_DOWNLOAD_MB * 1024 * 1024,
                "overwrites": True,
                "outtmpl": outtmpl,
                **extra,
            }

            # اطلاعات و آدرس‌های موقت را در لحظهٔ دانلود دوباره می‌گیریم.
            info_options = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "noplaylist": True,
                "socket_timeout": 25,
                "retries": 2,
                **extra,
            }
            with yt_dlp.YoutubeDL(info_options) as ydl:
                info = ydl.extract_info(url, download=False)

            formats = info.get("formats") or []
            target_height = fmt.get("height") if fmt else None
            if fmt and fmt.get("format_selector"):
                common["format"] = fmt["format_selector"]
            else:
                muxed = [
                    item for item in formats
                    if item.get("vcodec") not in (None, "none")
                    and item.get("acodec") not in (None, "none")
                    and item.get("ext") in ("mp4", "webm", "mov", "m4v")
                    and (not target_height or item.get("height") == target_height)
                ]
                if not muxed:
                    raise RuntimeError("کیفیت انتخابی در دسترس نیست؛ لینک را دوباره بفرست.")
                best = max(muxed, key=lambda item: (
                    item.get("tbr") or 0, item.get("filesize") or 0
                ))
                common["format"] = best["format_id"]

            with yt_dlp.YoutubeDL(common) as ydl:
                ydl.download([url])

            # فقط فایل‌های ساخته‌شده در همین عملیات را بررسی کن.
            recent = sorted(
                WORK_DIR.glob(prefix + "*"),
                key=lambda item: item.stat().st_mtime,
                reverse=True
            )
            if not recent:
                raise RuntimeError("دانلود تمام شد اما فایل خروجی پیدا نشد.")
            result = recent[0]
            if result.stat().st_size > MAX_DOWNLOAD_MB * 1024 * 1024:
                result.unlink(missing_ok=True)
                raise RuntimeError(
                    f"حجم فایل از سقف دانلود {MAX_DOWNLOAD_MB} مگابایت بیشتر است."
                )
            return result

        except Exception as exc:
            last_error = exc
            # پاک کردن خروجی ناقص پیش از تلاش بعدی
            for partial in WORK_DIR.glob(prefix + "*"):
                try:
                    partial.unlink(missing_ok=True)
                except OSError:
                    pass
            if "403" not in str(exc) and "Forbidden" not in str(exc):
                # خطاهای غیر 403 با تلاش دیگر ممکن است حل شوند، اما بیهوده
                # چند بار درخواست نمی‌زنیم.
                continue
            log.warning("HTTP 403; retrying with fresh extractor settings: %s", exc)

    message = str(last_error or "خطای ناشناخته")
    if "403" in message or "Forbidden" in message:
        raise RuntimeError(
            "یوتیوب درخواست دانلود را با 403 رد کرد، حتی پس از بررسی دوبارهٔ لینک. "
            "yt-dlp را در Pydroid به‌روز کن: pip install -U yt-dlp . "
            "اگر همچنان خطا بود، ممکن است این ویدیو به کوکیِ مجاز یا دسترسی دیگری نیاز داشته باشد؛ "
            "این ربات ورود به حساب یا محدودیت‌های منبع را دور نمی‌زند."
        )
    raise RuntimeError(message)


def download_supported_platform_with_ytdlp(chat_id, url, fmt=None, title=""):
    """دانلود از پلتفرم‌های مجاز غیر از یوتیوب با تلاش مجدد و دانلود قطعه‌ای موازی."""
    prefix = f"{chat_id}_{int(time.time())}_"
    outtmpl = str(WORK_DIR / (prefix + "%(title).80s.%(ext)s"))
    progress_message_id = progress_context.get(chat_id)

    def hook(status):
        if chat_id in cancel_requested:
            raise yt_dlp.utils.DownloadError("دانلود به درخواست کاربر لغو شد.")
        if status.get("status") == "downloading":
            now = time.time()
            previous = progress_last_update.get(chat_id, 0)
            if now - previous < 1.5:
                return
            progress_last_update[chat_id] = now
            downloaded = status.get("downloaded_bytes") or 0
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            percent = (downloaded / total * 100) if total else None
            speed_value = status.get("speed")
            speed = f"{speed_value / (1024 * 1024):.2f} MB/s" if speed_value else None
            eta = status.get("eta")
            update_progress(chat_id, progress_message_id,
                            progress_text("در حال دانلود از پلتفرم", percent, downloaded, total, speed, eta))
        elif status.get("status") == "finished":
            update_progress(chat_id, progress_message_id,
                            "✅ دانلود کامل شد.\n🔧 در حال آماده‌سازی فایل برای ارسال…")

    common = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "concurrent_fragment_downloads": 4,
        "buffersize": 1024 * 1024,
        "max_filesize": MAX_DOWNLOAD_MB * 1024 * 1024,
        "overwrites": True,
        "outtmpl": outtmpl,
        "progress_hooks": [hook],
    }
    try:
        # لینک و فرمت‌ها تازه خوانده می‌شوند تا URLهای موقت و format ID قدیمی استفاده نشوند.
        with yt_dlp.YoutubeDL({
            "quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "socket_timeout": 30, "retries": 3,
            "extractor_retries": 3,
        }) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = info.get("formats") or []
        if fmt and fmt.get("format_selector"):
            common["format"] = fmt["format_selector"]
        else:
            target_height = fmt.get("height") if fmt else None
            muxed = [item for item in formats
                     if item.get("vcodec") not in (None, "none")
                     and item.get("acodec") not in (None, "none")
                     and item.get("ext") in ("mp4", "webm", "mov", "m4v")
                     and (not target_height or item.get("height") == target_height)]
            if muxed:
                best = max(muxed, key=lambda item: (item.get("tbr") or 0, item.get("filesize") or 0))
                common["format"] = best["format_id"]
            else:
                common["format"] = "bestvideo+bestaudio/best"

        with yt_dlp.YoutubeDL(common) as ydl:
            ydl.download([url])

        recent = sorted(WORK_DIR.glob(prefix + "*"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not recent:
            raise RuntimeError("دانلود تمام شد اما فایل خروجی پیدا نشد.")
        result = recent[0]
        if result.stat().st_size > MAX_DOWNLOAD_MB * 1024 * 1024:
            result.unlink(missing_ok=True)
            raise RuntimeError(f"حجم فایل از سقف دانلود {MAX_DOWNLOAD_MB} مگابایت بیشتر است.")
        return result
    except Exception as exc:
        for partial in WORK_DIR.glob(prefix + "*"):
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
        log.exception("Non-YouTube download failed")
        message = str(exc)
        if any(word in message.lower() for word in ("login", "sign in", "private", "authentication", "cookies")):
            raise RuntimeError("این سایت برای این ویدیو ورود یا دسترسی مجاز می‌خواهد؛ ربات محدودیت دسترسی را دور نمی‌زند.") from exc
        raise RuntimeError(message[:450] or "دانلود از این سایت انجام نشد.") from exc


def perform_download(chat_id, url, fmt=None, title=""):
    with state_lock:
        if chat_id in active_downloads:
            send_message(chat_id, "⏳ یک دانلود دیگر برای شما در حال انجام است؛ کمی صبر کن.")
            return
        active_downloads.add(chat_id)
        cancel_requested.discard(chat_id)

    path = None
    progress_message_id = make_progress_message(chat_id, "در حال آماده‌سازی دانلود…")
    progress_context[chat_id] = progress_message_id
    try:
        update_progress(chat_id, progress_message_id, "در حال آماده‌سازی دانلود…")
        platform = platform_for_url(url)
        if not platform:
            raise RuntimeError("این سایت پشتیبانی نمی‌شود.")
        if platform == "youtube":
            path = download_with_ytdlp(chat_id, url, fmt, title)
        else:
            path = download_supported_platform_with_ytdlp(chat_id, url, fmt, title)
        update_progress(chat_id, progress_message_id,
                        f"✅ دانلود کامل شد\n📦 حجم فایل: {path.stat().st_size / (1024 * 1024):.2f} MB\n"
                        "در حال ارسال فایل به بله…")
        sent = send_file(
            chat_id, path, f"✅ {safe_filename(title or path.stem)}\nBale Video Downloader",
            progress_message_id=progress_message_id
        )
        if not sent:
            return
        # وقتی ارسال موفق شد، پیام وضعیت را حذف کن تا فقط فایل باقی بماند.
        if progress_message_id is not None:
            if not delete_message(chat_id, progress_message_id):
                edit_message(chat_id, progress_message_id, "فایل با موفقیت ارسال شد.")
    except Exception as exc:
        log.exception("Download failed")
        message = str(exc)
        if len(message) > 500:
            message = message[:500] + "…"
        error_text = "❌️ دانلود یا ارسال انجام نشد.\n" + message
        if progress_message_id is not None:
            update_progress(chat_id, progress_message_id, error_text)
        else:
            send_message(chat_id, error_text)
    finally:
        if path:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        progress_context.pop(chat_id, None)
        progress_last_update.pop(chat_id, None)
        cancel_requested.discard(chat_id)
        with state_lock:
            active_downloads.discard(chat_id)
            pending.pop(chat_id, None)


# -------------------- دریافت پیام‌ها و دکمه‌ها --------------------
def welcome(chat_id):
    keyboard = [
        [{"text": "YouTube", "callback_data": "platform:youtube"},
         {"text": "Instagram", "callback_data": "platform:instagram"}],
        [{"text": "TikTok", "callback_data": "platform:tiktok"},
         {"text": "Pinterest", "callback_data": "platform:pinterest"}],
        [{"text": "📖 راهنمای استفاده", "callback_data": "help"}],
    ]
    send_message(
        chat_id,
        "🎬 دانلود ویدیو | بله\n━━━━━━━━━━━━━━\n"
        "سلام! به ربات دانلود ویدیو خوش اومدی.\n\n"
        "از دکمه‌های زیر پلتفرم رو انتخاب کن یا لینک ویدیوت رو همین‌جا بفرست.\n"
        "بعد از بررسی، نسخه‌های قابل دریافت همراه با وضوح و حجم تقریبی نمایش داده می‌شن؛ یکی رو انتخاب کن تا فایل در همین گفت‌وگو برات ارسال بشه.\n\n"
        "پشتیبانی: YouTube، Instagram، TikTok و Pinterest\n"
        "لینک سایت‌های دیگه و لینک مستقیم فایل پذیرفته نمی‌شه. بعضی ویدیوها هم ممکنه به‌خاطر محدودیت‌های خود پلتفرم قابل دریافت نباشن.", 
        keyboard
    )


def handle_url(chat_id, url):
    valid, result = validate_url(url)
    if not valid:
        send_message(chat_id, "⚠️ " + result)
        return

    now = time.time()
    previous = last_request.get(chat_id, 0)
    if now - previous < USER_COOLDOWN_SECONDS:
        send_message(chat_id, "چند ثانیه صبر کن و دوباره امتحان کن.")
        return
    last_request[chat_id] = now


    status_id = None
    try:
        status_result = send_message(chat_id, "در حال بررسی لینک و کیفیت‌های موجود…")
        if isinstance(status_result, dict):
            status_id = status_result.get("message_id")

        info, choices = inspect_media(result)
        title = info.get("title") or "ویدیو"
        uploader = info.get("uploader") or info.get("channel")
        duration = info.get("duration")
        views = info.get("view_count")
        details = [f"عنوان: {title[:220]}"]
        if uploader:
            details.append(f"کانال: {str(uploader)[:100]}")
        if duration:
            mins, secs = divmod(int(duration), 60)
            details.append(f"مدت: {mins}:{secs:02d}")
        if views:
            details.append(f"بازدید: {views:,}")
        details.append("\nنسخه‌ای را که می‌خواهی دریافت کنی انتخاب کن:")
        caption = "\n".join(details)

        pending[chat_id] = {"url": result, "choices": choices, "title": title}
        thumbnails = info.get("thumbnails") or []
        thumbnail_url = info.get("thumbnail")
        if not thumbnail_url and thumbnails:
            thumbnail_url = next(
                (item.get("url") for item in reversed(thumbnails) if item.get("url")),
                None
            )
        send_media_preview(chat_id, thumbnail_url, caption, quality_keyboard(chat_id, choices))
        delete_message(chat_id, status_id)
    except Exception as exc:
        log.exception("Inspect failed")
        send_message(
            chat_id,
            "❌ لینک بررسی نشد. ممکن است ویدیو در دسترس نباشد یا پلتفرم موقتاً پاسخ ندهد. لطفاً لینک را بررسی کن و کمی بعد دوباره امتحان کن."
        )
        delete_message(chat_id, status_id)


def handle_update(update):
    callback = update.get("callback_query")
    if callback:
        callback_id = callback.get("id")
        data = callback.get("data", "")
        message = callback.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        sender_id = (callback.get("from") or {}).get("id")
        answer_callback(callback_id)

        if data.startswith("platform:"):
            platform = data.split(":", 1)[1]
            prompts = {
                "youtube": "YouTube\nلینک ویدیو را بفرست تا کیفیت‌های موجود بررسی شوند.",
                "instagram": "Instagram\nلینک پست یا ریلز عمومی را بفرست. پشتیبانی به دسترسی منبع بستگی دارد.",
                "tiktok": "TikTok\nلینک ویدیو را بفرست؛ فقط گزینه‌های قابل‌دریافت نمایش داده می‌شوند.",
                "pinterest": "Pinterest\nلینک پین یا ویدیو را بفرست.",
            }
            if chat_id is not None:
                send_message(chat_id, prompts.get(platform, "لینک را بفرست."))
            return
        if data == "help":
            if chat_id is not None:
                send_message(chat_id,
                    "راهنمای استفاده:\n۱) لینک ویدیو از یکی از چهار پلتفرم پشتیبانی‌شده را بفرست.\n۲) از بین نسخه‌های نمایش‌داده‌شده، وضوح و حجم مناسب را انتخاب کن.\n"
                    "۳) پس از دانلود، فایل در همین چت ارسال می‌شود.\n"
                    "فقط YouTube، Instagram، TikTok و Pinterest پشتیبانی می‌شوند. لینک مستقیم و سایت‌های دیگر مجاز نیستند. بعضی محتواها ممکن است به دلیل محدودیت‌های خود پلتفرم در دسترس نباشند.")
            return
        if data == "cleanup":
            count = 0
            for item in WORK_DIR.iterdir():
                if item.is_file() and not any(
                    item.name.startswith(f"{active_chat}_") for active_chat in active_downloads
                ):
                    try:
                        item.unlink()
                        count += 1
                    except OSError:
                        pass
            if chat_id is not None:
                send_message(chat_id, f"🧹 پاک‌سازی انجام شد؛ {count} فایل موقت حذف شد.")
            return

        parts = data.split(":")
        if len(parts) < 2:
            return
        action = parts[0]
        try:
            target_chat = int(parts[1])
        except ValueError:
            return
        # فقط کاربری که همان پیام را دریافت کرده، می‌تواند عملیات همان چت را انجام دهد.
        if chat_id is None or int(chat_id) != target_chat:
            return
        if sender_id is not None and str(sender_id) != str(chat_id):
            # در گروه‌ها شناسه کاربر و چت متفاوت است؛ در گروه اجازه انتخاب نده.
            if (message.get("chat") or {}).get("type") != "private":
                send_message(chat_id, "برای امنیت، این ربات را در گفت‌وگوی خصوصی استفاده کن.")
                return

        if action == "cancel":
            if target_chat in active_downloads:
                cancel_requested.add(target_chat)
                send_message(target_chat, "🛑 درخواست لغو ثبت شد؛ دانلود در اولین فرصت متوقف می‌شود.")
            else:
                pending.pop(target_chat, None)
                send_message(target_chat, "❎ درخواست لغو شد.")
            return

        job = pending.get(target_chat)
        if not job:
            send_message(target_chat, "این درخواست منقضی شده؛ لینک را دوباره بفرست.")
            return


        if action == "quality" and len(parts) == 3:
            try:
                index = int(parts[2])
                fmt = job["choices"][index]
            except (ValueError, IndexError, KeyError):
                send_message(target_chat, "کیفیت انتخاب‌شده معتبر نیست؛ لینک را دوباره بفرست.")
                return
            threading.Thread(
                target=perform_download,
                args=(target_chat, job["url"], fmt, job.get("title", "")),
                daemon=True
            ).start()
        return

    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    react_eyes(chat_id, message.get("message_id"))
    if chat.get("type") != "private":
        send_message(chat_id, "🔒 برای استفاده امن، لطفاً در گفت‌وگوی خصوصی با ربات پیام بده.")
        return

    text = (message.get("text") or "").strip()
    if text in ("/start", "/start@LinkDownloaderBot"):
        welcome(chat_id)
    elif text in ("/help", "راهنما"):
        send_message(chat_id,
            "لینک ویدیو از YouTube، Instagram، TikTok یا Pinterest بفرست.\n"
            "لینک مستقیم فایل و سایت‌های دیگر پشتیبانی نمی‌شوند.")
    elif text.startswith("http://") or text.startswith("https://"):
        handle_url(chat_id, text)
    else:
        send_message(chat_id, "🔗 لطفاً لینک ویدیو را بفرست؛ یا /help را بزن.")


def main():
    if not BOT_TOKEN or BOT_TOKEN == "توکن_ربات_بله_را_اینجا_قرار_بده":
        print("خطا: مقدار BOT_TOKEN را در ابتدای فایل با توکن ربات بله عوض کن.")
        return

    print("Bale Video Downloader اجرا شد. اگر یوتیوب 403 داد، در Pydroid اجرا کن: pip install -U yt-dlp")
    offset = None
    while True:
        try:
            data = {"timeout": POLL_TIMEOUT, "limit": 50}
            if offset is not None:
                data["offset"] = offset
            updates = api("getUpdates", data=data, timeout=POLL_TIMEOUT + 15) or []
            for update in updates:
                offset = int(update.get("update_id", 0)) + 1
                try:
                    handle_update(update)
                except Exception:
                    log.exception("Update handling failed")
        except KeyboardInterrupt:
            print("\nربات متوقف شد.")
            break
        except Exception as exc:
            log.exception("Polling error: %s", exc)
            time.sleep(4)


if __name__ == "__main__":
    main()
