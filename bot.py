#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات بله – نسخهٔ ۱۶ (Enterprise Pro)
مرورگر دو حالته با stealth، اسکن ویدیو/فایل، دانلودر هوشمند،
اسکرین‌شات 4K چندمرحله‌ای، ضبط ویدیو با سه رفتار (کلیک/اسکرول/لایو)،
استخراج فرامین، جستجوی پیشرفته، سیستم اشتراک سه‌لایه (برنز/پلاس/پرو)،
پنل ادمین، محدودیت صف و کلیک، Rate Limiting.
"""

import os, sys, json, time, math, queue, shutil, zipfile, uuid, re, hashlib
import subprocess, threading, traceback, random
from dataclasses import dataclass, asdict, field
from typing import Dict, Any, Optional, List, Tuple, Set
from urllib.parse import urlparse, urljoin, unquote

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ═══════════════════════ تنظیمات اصلی ═══════════════════════
BALE_BOT_TOKEN = os.getenv("BALE_BOT_TOKEN", "").strip()
if not BALE_BOT_TOKEN:
    print("ERROR: BALE_BOT_TOKEN not set", file=sys.stderr)
    sys.exit(1)

BALE_API_URL = "https://tapi.bale.ai/bot" + BALE_BOT_TOKEN
REQUEST_TIMEOUT = 30
LONG_POLL_TIMEOUT = 50
WORKER_COUNT = 1
ZIP_PART_SIZE = int(19 * 1024 * 1024)      # 19 MB

ADMIN_CHAT_ID = 46829437

# ═══════════════════════ کدهای اشتراک ═══════════════════════
BRONZE_CODES = ["BRONZE01", "BRONZE02", "BRONZE03", "BRONZE04", "BRONZE05",
                "BRONZE06", "BRONZE07", "BRONZE08", "BRONZE09", "BRONZE10"]
PLUS_CODES   = ["PLUS2024A", "PLUS2024B", "PLUS2024C", "PLUS2024D", "PLUS2024E"]
PRO_CODES    = ["PRO2024A", "PRO2024B", "PRO2024C", "PRO2024D", "PRO2024E"]

# ═══════════════════════ محدودیت‌های مصرف (Rate Limits) ═══════════════════════
# هر سطح: { "mode": (حداکثر تعداد, بازه‌ی زمانی به ثانیه, حداکثر حجم به بایت) }
# None در حجم یعنی بدون محدودیت حجم
LIMITS = {
    "free": {
        "browser":       (2, 3600, None),
        "screenshot":    (2, 3600, None),
        "2x_screenshot": (0, 3600, None),
        "4k_screenshot": (0, 3600, None),
        "download":      (1, 3600, 10 * 1024 * 1024),       # 10 MB
        "record_video":  (0, 3600, None),
        "scan_downloads":(0, 3600, None),
        "scan_videos":   (0, 3600, None),
        "download_website": (0, 3600, None),
        "extract_commands": (0, 3600, None),
    },
    "bronze": {
        "browser":       (5, 3600, None),
        "screenshot":    (2, 3600, None),
        "2x_screenshot": (1, 3600, None),
        "4k_screenshot": (1, 3600, None),
        "download":      (2, 3600, 100 * 1024 * 1024),      # 100 MB
        "record_video":  (1, 3600, None),
        "scan_downloads":(1, 3600, None),
        "scan_videos":   (1, 3600, None),
        "download_website": (0, 3600, None),
        "extract_commands": (1, 3600, None),
    },
    "plus": {
        "browser":       (10, 3600, None),
        "screenshot":    (10, 3600, None),
        "2x_screenshot": (5, 3600, None),
        "4k_screenshot": (3, 3600, None),
        "download":      (5, 3600, 600 * 1024 * 1024),      # 600 MB
        "record_video":  (3, 3600, None),
        "scan_downloads":(2, 3600, None),
        "scan_videos":   (5, 3600, None),
        "download_website": (1, 3600, None),
        "extract_commands": (3, 3600, None),
    },
    "pro": {
        "browser":       (999, 3600, None),
        "screenshot":    (999, 3600, None),
        "2x_screenshot": (999, 3600, None),
        "4k_screenshot": (999, 3600, None),
        "download":      (999, 3600, None),
        "record_video":  (999, 3600, None),
        "scan_downloads":(999, 3600, None),
        "scan_videos":   (999, 3600, None),
        "download_website": (3, 86400, None),               # 3 بار در روز
        "extract_commands": (999, 3600, None),
    },
}

print_lock = threading.Lock()
queue_lock = threading.Lock()
workers_lock = threading.Lock()
subscriptions_lock = threading.Lock()
callback_map: Dict[str, str] = {}
callback_map_lock = threading.Lock()
browser_contexts_lock = threading.Lock()

def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs, flush=True)

# ═══════════════════════ مدل‌های داده ═══════════════════════
@dataclass
class UserSettings:
    record_time: int = 20
    default_download_mode: str = "store"
    browser_mode: str = "text"
    deep_scan_mode: str = "logical"
    record_behavior: str = "click"

@dataclass
class SessionState:
    chat_id: int
    state: str = "idle"
    is_pro: bool = False
    is_admin: bool = False
    subscription: str = "free"              # "free", "bronze", "plus", "pro"
    current_job_id: Optional[str] = None
    browser_url: Optional[str] = None
    last_interaction: float = time.time()
    cancel_requested: bool = False
    text_links: Optional[Dict[str, str]] = None
    browser_links: Optional[List[Dict[str, str]]] = None
    browser_page: int = 0
    settings: UserSettings = field(default_factory=UserSettings)
    click_counter: int = 0
    ad_blocked_domains: Optional[List[str]] = field(default_factory=list)
    found_downloads: Optional[List[Dict[str, str]]] = None
    found_downloads_page: int = 0

@dataclass
class Job:
    job_id: str
    chat_id: int
    mode: str
    url: str
    status: str = "queued"
    created_at: float = time.time()
    updated_at: float = time.time()
    error_message: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None
    started_at: Optional[float] = None

@dataclass
class WorkerInfo:
    worker_id: int
    current_job_id: Optional[str] = None
    status: str = "idle"

# ═══════════════════════ مدیریت اشتراک‌ها ═══════════════════════
SUBSCRIPTIONS_FILE = "subscriptions.json"

def load_subscriptions() -> Dict[str, Any]:
    try:
        with open(SUBSCRIPTIONS_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_subscriptions(data: Dict[str, Any]):
    tmp = SUBSCRIPTIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, SUBSCRIPTIONS_FILE)

def get_user_subscription(chat_id: int) -> Tuple[str, Optional[Dict[str, Any]]]:
    """برمی‌گرداند (سطح, info دیکشنری)"""
    data = load_subscriptions()
    key = str(chat_id)
    if key in data:
        return data[key].get("level", "free"), data[key]
    return "free", None

def set_user_subscription(chat_id: int, level: str):
    with subscriptions_lock:
        data = load_subscriptions()
        data[str(chat_id)] = {
            "level": level,
            "activated_at": time.time(),
            "usage": {}       # رکورد مصرف این کاربر
        }
        save_subscriptions(data)

def update_usage(chat_id: int, mode: str):
    """ثبت یک مصرف از mode برای کاربر"""
    with subscriptions_lock:
        data = load_subscriptions()
        key = str(chat_id)
        if key not in data:
            data[key] = {"level": "free", "activated_at": time.time(), "usage": {}}
        usage = data[key].get("usage", {})
        now = time.time()
        # پاک‌سازی رکوردهای قدیمی (بیشتر از ۱ ساعت)
        hour_ago = now - 3600
        day_ago = now - 86400
        # نگهداری فقط رکوردهای ۱ ساعت اخیر (و ۲۴ ساعت برای download_website)
        if mode not in usage:
            usage[mode] = []
        usage[mode] = [t for t in usage[mode] if now - t < 86400]  # نگهداری ۲۴ ساعت
        usage[mode].append(now)
        data[key]["usage"] = usage
        save_subscriptions(data)

def check_rate_limit(chat_id: int, mode: str, file_size_bytes: Optional[int] = None) -> Optional[str]:
    """
    چک می‌کند که کاربر مجاز به اجرای mode هست یا نه.
    اگر مجاز بود None برمی‌گرداند.
    اگر محدود شده بود، پیام خطا برمی‌گرداند.
    """
    # ادمین همیشه مجازه
    if chat_id == ADMIN_CHAT_ID:
        return None

    level, _ = get_user_subscription(chat_id)
    limits = LIMITS.get(level, LIMITS["free"])
    limit = limits.get(mode)

    if not limit:
        return f"⛔ این قابلیت برای سطح {level} در دسترس نیست."

    max_count, window_seconds, max_size = limit

    # چک حجم
    if max_size is not None and file_size_bytes is not None and file_size_bytes > max_size:
        max_mb = max_size / (1024 * 1024)
        return f"📦 حجم فایل ({file_size_bytes/(1024*1024):.1f}MB) بیش از حد مجاز ({max_mb:.0f}MB) برای سطح {level} است."

    # چک تعداد
    if max_count >= 999:  # نامحدود عملی
        return None

    # خواندن usage
    data = load_subscriptions()
    key = str(chat_id)
    usage = data.get(key, {}).get("usage", {})
    times = usage.get(mode, [])
    now = time.time()
    if window_seconds == 86400:  # روزانه
        day_ago = now - 86400
        recent = [t for t in times if t > day_ago]
    else:
        hour_ago = now - window_seconds
        recent = [t for t in times if t > hour_ago]

    if len(recent) >= max_count:
        if window_seconds == 3600:
            return f"⏰ محدودیت ساعتی: حداکثر {max_count} بار در ساعت (سطح {level})."
        else:
            return f"⏰ محدودیت روزانه: حداکثر {max_count} بار در روز (سطح {level})."

    return None

# ═══════════════════════ ذخیره‌سازی محلی Sessionها ═══════════════════════
SESSIONS_FILE = "sessions.json"
def load_sessions():
    try:
        with open(SESSIONS_FILE, "r") as f: return json.load(f)
    except: return {}
def save_sessions(data):
    tmp = SESSIONS_FILE + ".tmp"
    with open(tmp, "w") as f: json.dump(data, f)
    os.replace(tmp, SESSIONS_FILE)
def get_session(chat_id):
    data = load_sessions()
    key = str(chat_id)
    if key in data:
        s = SessionState(chat_id=chat_id)
        d = data[key]
        for k, v in d.items():
            if k == "settings":
                s.settings = UserSettings(**v)
            elif k in ("ad_blocked_domains", "found_downloads"):
                setattr(s, k, v)
            else:
                setattr(s, k, v)
        if s.chat_id == ADMIN_CHAT_ID:
            s.is_admin = True
            s.subscription = "pro"
        else:
            # بازیابی اشتراک از فایل subscriptions
            level, _ = get_user_subscription(chat_id)
            s.subscription = level
        return s
    s = SessionState(chat_id=chat_id)
    if s.chat_id == ADMIN_CHAT_ID:
        s.is_admin = True
        s.subscription = "pro"
    return s
def set_session(session):
    data = load_sessions()
    d = asdict(session)
    d["settings"] = asdict(session.settings)
    d["ad_blocked_domains"] = session.ad_blocked_domains
    d["found_downloads"] = session.found_downloads
    data[str(session.chat_id)] = d
    save_sessions(data)

# ═══════════════════════ API بله ═══════════════════════
def bale_request(method, params=None, files=None):
    url = f"{BALE_API_URL}/{method}"
    try:
        if files:
            r = requests.post(url, data=params or {}, files=files, timeout=REQUEST_TIMEOUT)
        else:
            r = requests.post(url, json=params or {}, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200: return None
        data = r.json()
        if not data.get("ok"): return None
        return data["result"]
    except: return None

def send_message(chat_id, text, reply_markup=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_markup: params["reply_markup"] = json.dumps(reply_markup)
    return bale_request("sendMessage", params=params)

def send_document(chat_id, file_path, caption=""):
    with open(file_path, "rb") as f:
        return bale_request("sendDocument",
                            params={"chat_id": chat_id, "caption": caption},
                            files={"document": (os.path.basename(file_path), f)})

def answer_callback_query(cq_id, text="", show_alert=False):
    return bale_request("answerCallbackQuery",
                        {"callback_query_id": cq_id, "text": text, "show_alert": show_alert})

def get_updates(offset=None, timeout=LONG_POLL_TIMEOUT):
    params = {"timeout": timeout}
    if offset: params["offset"] = offset
    return bale_request("getUpdates", params=params) or []

# ═══════════════════════ منوها (با نمایش سطح اشتراک) ═══════════════════════
def main_menu_keyboard(is_admin=False, subscription="free"):
    sub_display = {"free": "🆓 رایگان", "bronze": "🥉 برنز", "plus": "🥈 پلاس", "pro": "🥇 پرو"}
    keyboard = [
        [{"text": "🧭 مرورگر من", "callback_data": "menu_browser"}],
        [{"text": "📸 اسکرین‌شات", "callback_data": "menu_screenshot"}],
        [{"text": "📥 دانلود", "callback_data": "menu_download"}],
        [{"text": "⚙️ تنظیمات", "callback_data": "menu_settings"}],
        [{"text": "💀 لغو / ریست", "callback_data": "menu_cancel"}]
    ]
    if is_admin:
        keyboard.append([{"text": "🛠️ پنل ادمین", "callback_data": "menu_admin"}])
    return {"inline_keyboard": keyboard}

def settings_keyboard(settings: UserSettings):
    rec = settings.record_time
    dlm  = "سریع ⚡" if settings.default_download_mode == "stream" else "عادی 💾"
    mode = "🎬 مدیا" if settings.browser_mode == "media" else "📄 متن"
    deep = "🧠 منطقی" if settings.deep_scan_mode == "logical" else "🗑 همه چیز"
    rec_behavior = {"click": "🖱️ کلیک هوشمند", "scroll": "📜 اسکرول نرم", "live": "🎭 لایو کامند"}[settings.record_behavior]
    return {"inline_keyboard": [
        [{"text": f"⏱️ زمان ضبط: {rec}s", "callback_data": "set_rec"}],
        [{"text": f"📥 دانلود: {dlm}", "callback_data": "set_dlmode"}],
        [{"text": f"🌐 حالت: {mode}", "callback_data": "set_brwmode"}],
        [{"text": f"🔍 جستجو: {deep}", "callback_data": "set_deep"}],
        [{"text": f"🎬 ضبط: {rec_behavior}", "callback_data": "set_recbeh"}],
        [{"text": "🔙 بازگشت", "callback_data": "back_main"}]
    ]}

# ═══════════════════════ Playwright – global ═══════════════════════
_global_playwright = None
_global_browser = None
browser_contexts = {}

try:
    from playwright_stealth import Stealth
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

AD_DOMAINS = [
    "doubleclick.net", "googleadsyndication.com", "adservice.google.com",
    "adsrvr.org", "outbrain.com", "taboola.com", "exoclick.com",
    "trafficfactory.biz", "propellerads.com", "adnxs.com", "criteo.com",
    "moatads.com", "amazon-adsystem.com", "pubmatic.com", "openx.net",
    "rubiconproject.com", "sovrn.com", "indexww.com", "contextweb.com",
    "advertising.com", "zedo.com", "adzerk.net", "carbonads.com",
    "buysellads.com", "popads.net", "trafficstars.com", "trafficjunky.com",
    "eroadvertising.com", "juicyads.com", "plugrush.com",
    "txxx.com", "fuckbook.com", "traffic-force.com", "bongacams.com",
    "trafficjunky.net", "adtng.com"
]

BLOCKED_AD_KEYWORDS = [
    "ads", "advert", "popunder", "banner", "doubleclick", "taboola",
    "outbrain", "popcash", "traffic", "monetize", "adx", "adserving"
]

def get_or_create_context(chat_id):
    global _global_playwright, _global_browser
    ctx_key = str(chat_id)
    with browser_contexts_lock:
        existing = browser_contexts.get(ctx_key)
        if existing and time.time() - existing["last_used"] < 600:
            existing["last_used"] = time.time()
            return existing["context"]
        if existing:
            try: existing["context"].close()
            except: pass
        if _global_browser is None:
            _global_playwright = sync_playwright().start()
            _global_browser = _global_playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-web-security",
                ]
            )
        vw = random.choice([412, 390, 414])
        vh = random.choice([915, 844, 896])
        context = _global_browser.new_context(viewport={"width": vw, "height": vh})
        def handle_popup(page):
            try:
                url = page.url.lower()
                if any(kw in url for kw in BLOCKED_AD_KEYWORDS) or \
                   any(ad in url for ad in AD_DOMAINS):
                    page.close()
            except: pass
        context.on("page", handle_popup)
        if HAS_STEALTH:
            page = context.new_page()
            try: Stealth().apply_stealth(page)
            except: pass
            finally: page.close()
        browser_contexts[ctx_key] = {"context": context, "last_used": time.time()}
        return context

def close_user_context(chat_id):
    ctx_key = str(chat_id)
    with browser_contexts_lock:
        ctx = browser_contexts.pop(ctx_key, None)
    if ctx:
        try: ctx["context"].close()
        except: pass
# ═══════════════════════ استخراج المان‌ها ═══════════════════════
def extract_clickable_and_media(page, mode="text"):
    if mode == "text":
        raw = page.evaluate("""() => {
            const items = []; const seen = new Set();
            function isVisible(el) {
                const s = window.getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden' && el.offsetWidth > 0;
            }
            document.querySelectorAll('a[href]').forEach(a => {
                if (!isVisible(a)) return;
                let t = a.textContent.trim() || 'لینک';
                if (!seen.has(a.href)) { seen.add(a.href); items.push(['link', t, a.href]); }
            });
            return items;
        }""")
        links = [(t, txt, h) for t, txt, h in raw if h.startswith("http")]
        return links, []
    else:  # media mode
        video_sources = page.evaluate("""() => {
            const vids = [];
            document.querySelectorAll('video').forEach(v => {
                let src = v.src || (v.querySelector('source') ? v.querySelector('source').src : '');
                if (src) vids.push(src);
            });
            document.querySelectorAll('iframe').forEach(f => {
                if (f.src) vids.push(f.src);
            });
            return [...new Set(vids)].filter(u => u.startsWith('http'));
        }""")
        anchors = page.evaluate("""() => {
            const a = []; document.querySelectorAll('a[href]').forEach(e => a.push(e.href));
            return a.filter(h => h.startsWith('http'));
        }""")
        links = [("link", href.split("/")[-1][:20] or "لینک", href) for href in anchors[:20]]
        return links, video_sources

def scan_videos_smart(page):
    elements = page.evaluate("""() => {
        const results = [];
        const centerX = window.innerWidth / 2;
        const centerY = window.innerHeight / 2;
        document.querySelectorAll('video').forEach(v => {
            const rect = v.getBoundingClientRect();
            if (rect.width < 200 || rect.height < 150) return;
            let src = v.src || (v.querySelector('source') ? v.querySelector('source').src : '');
            if (!src) return;
            const area = rect.width * rect.height;
            const dist = Math.sqrt(Math.pow(rect.x + rect.width/2 - centerX, 2) + Math.pow(rect.y + rect.height/2 - centerY, 2));
            results.push({text: 'video element', href: src, score: area - dist*2, w: rect.width, h: rect.height});
        });
        document.querySelectorAll('iframe').forEach(f => {
            const rect = f.getBoundingClientRect();
            if (rect.width < 300 || rect.height < 200) return;
            let src = f.src || '';
            if (!src.startsWith('http')) return;
            const area = rect.width * rect.height;
            const dist = Math.sqrt(Math.pow(rect.x + rect.width/2 - centerX, 2) + Math.pow(rect.y + rect.height/2 - centerY, 2));
            results.push({text: 'iframe', href: src, score: area - dist*2, w: rect.width, h: rect.height});
        });
        return results;
    }""")

    network_urls = []
    def capture(response):
        ct = response.headers.get("content-type", "")
        url = response.url.lower()
        if "mpegurl" in ct or "dash+xml" in ct or url.endswith((".m3u8", ".mpd")):
            network_urls.append(response.url)
    page.on("response", capture)
    page.wait_for_timeout(3000)
    page.remove_listener("response", capture)

    json_urls = page.evaluate("""() => {
        const results = [];
        const scripts = document.querySelectorAll('script');
        for (const s of scripts) {
            const text = s.textContent || '';
            const matches = text.match(/(https?:\\/\\/[^"']+\\.(?:m3u8|mp4|mkv|webm|mpd)[^"']*)/gi);
            if (matches) results.push(...matches);
        }
        return results;
    }""")

    all_candidates = []
    for el in elements:
        href = el["href"]
        if not href.startswith("http"): continue
        parsed = urlparse(href)
        if any(ad in parsed.netloc for ad in AD_DOMAINS): continue
        if any(kw in href.lower() for kw in BLOCKED_AD_KEYWORDS): continue
        all_candidates.append({
            "text": (el["text"] + f" ({parsed.netloc})")[:35],
            "href": href,
            "score": el["score"]
        })
    for url in network_urls:
        if url in [c["href"] for c in all_candidates]: continue
        parsed = urlparse(url)
        if any(ad in parsed.netloc for ad in AD_DOMAINS): continue
        all_candidates.append({
            "text": f"HLS/DASH ({parsed.netloc})"[:35],
            "href": url,
            "score": 100000
        })
    for url in json_urls:
        if url in [c["href"] for c in all_candidates]: continue
        parsed = urlparse(url)
        if any(ad in parsed.netloc for ad in AD_DOMAINS): continue
        all_candidates.append({
            "text": f"JSON stream ({parsed.netloc})"[:35],
            "href": url,
            "score": 90000
        })
    all_candidates.sort(key=lambda x: x["score"], reverse=True)
    return all_candidates

# ═══════════════════════ اسکرول نرم و هوشمند ═══════════════════════
def smooth_scroll_to_video(page):
    """
    پیدا کردن بزرگترین ویدیو/iframe و اسکرول نرم و تدریجی به سمت آن.
    """
    coords = page.evaluate("""() => {
        let best = null;
        let bestArea = 0;
        document.querySelectorAll('video').forEach(v => {
            const rect = v.getBoundingClientRect();
            if (rect.width < 200 || rect.height < 150) return;
            const area = rect.width * rect.height;
            if (area > bestArea) { bestArea = area; best = { y: rect.top + window.scrollY, x: rect.left + window.scrollX, w: rect.width, h: rect.height }; }
        });
        document.querySelectorAll('iframe').forEach(f => {
            const rect = f.getBoundingClientRect();
            if (rect.width < 300 || rect.height < 200) return;
            const area = rect.width * rect.height;
            if (area > bestArea) { bestArea = area; best = { y: rect.top + window.scrollY, x: rect.left + window.scrollX, w: rect.width, h: rect.height }; }
        });
        return best || { y: window.scrollY, x: 0, w: 0, h: 0 };
    }""")

    target_y = coords["y"]
    current_y = page.evaluate("window.scrollY")
    distance = target_y - current_y
    steps = max(10, abs(distance) // 30)  # هر بار ۳۰ پیکسل
    step_size = distance / steps

    for i in range(steps):
        current_y += step_size
        page.evaluate(f"window.scrollTo(0, {int(current_y)})")
        page.wait_for_timeout(30)  # تأخیر کم برای نرمی

    # مطمئن بشیم رسیدیم
    page.evaluate(f"window.scrollTo(0, {int(target_y)})")
    page.wait_for_timeout(100)

# ═══════════════════════ ابزارهای فایل ═══════════════════════
def is_direct_file_url(url: str) -> bool:
    known_extensions = [
        '.zip','.rar','.7z','.pdf','.mp4','.mkv','.avi','.mp3',
        '.exe','.apk','.dmg','.iso','.tar','.gz','.bz2','.xz','.whl',
        '.deb','.rpm','.msi','.pkg','.appimage','.jar','.war',
        '.py','.sh','.bat','.run','.bin','.img','.mov','.flv','.wmv',
        '.webm','.ogg','.wav','.flac','.csv','.docx','.pptx','.m3u8'
    ]
    path = urlparse(url).path.lower()
    if any(path.endswith(ext) for ext in known_extensions):
        return True
    filename = path.split('/')[-1]
    if '.' in filename:
        ext = filename.rsplit('.', 1)[-1]
        if ext and re.match(r'^[a-zA-Z0-9_-]+$', ext) and len(ext) <= 10:
            return True
    return False

def is_logical_download(url: str, size_bytes: Optional[int] = None) -> bool:
    """در حالت منطقی فقط فایل‌های با پسوند معتبر یا حجم > 1MB را قبول می‌کند"""
    if is_direct_file_url(url):
        return True
    if size_bytes and size_bytes > 1024 * 1024:
        return True
    return False

def get_filename_from_url(url):
    path = unquote(urlparse(url).path)
    name = os.path.basename(path)
    return name if name and '.' in name else "downloaded_file"

def crawl_for_download_link(start_url, max_depth=1, max_pages=10, timeout_seconds=30):
    visited = set()
    q = queue.Queue()
    q.put((start_url, 0))
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    pc = 0
    start_time = time.time()
    while not q.empty():
        if time.time() - start_time > timeout_seconds:
            break
        cur, depth = q.get()
        if cur in visited or depth > max_depth or pc > max_pages: continue
        visited.add(cur); pc += 1
        try:
            r = s.get(cur, timeout=10)
        except: continue
        if is_direct_file_url(cur): return cur
        if "text/html" in r.headers.get("Content-Type", ""):
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = urljoin(cur, a["href"])
                if is_direct_file_url(href): return href
                if depth + 1 <= max_depth:
                    q.put((href, depth+1))
    return None

def split_file_binary(file_path, prefix, ext):
    d = os.path.dirname(file_path) or "."
    parts = []
    if not os.path.exists(file_path): return []
    with open(file_path, "rb") as f:
        i = 1
        while True:
            chunk = f.read(ZIP_PART_SIZE)
            if not chunk: break
            if ext.lower() == ".zip":
                pname = f"{prefix}.zip.{i:03d}"
            else:
                pname = f"{prefix}.part{i:03d}{ext}"
            ppath = os.path.join(d, pname)
            with open(ppath, "wb") as pf: pf.write(chunk)
            parts.append(ppath)
            i += 1
    return parts

def create_zip_and_split(src, base):
    d = os.path.dirname(src) or "."
    if not os.path.exists(src): return []
    zp = os.path.join(d, f"{base}.zip")
    try:
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(src, os.path.basename(src))
    except: return []
    if os.path.getsize(zp) <= ZIP_PART_SIZE:
        return [zp]
    parts = split_file_binary(zp, base, ".zip")
    os.remove(zp)
    return parts

# ═══════════════════════ اسکرین‌شات (چندمرحله‌ای) ═══════════════════════
def screenshot_full(context, url, out):
    page = context.new_page()
    try:
        page.goto(url, timeout=90000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        page.screenshot(path=out, full_page=True)
    finally: page.close()

def screenshot_2x(context, url, out):
    page = context.new_page()
    try:
        page.goto(url, timeout=90000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        page.evaluate("document.body.style.zoom = '200%'")
        page.wait_for_timeout(500)
        page.screenshot(path=out, full_page=True)
    finally: page.close()

def screenshot_4k(context, url, out):
    page = context.new_page()
    try:
        page.set_viewport_size({"width": 3840, "height": 2160})
        page.goto(url, timeout=90000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        page.screenshot(path=out, full_page=True)
    finally: page.close()

# ═══════════════════════ دانلود کامل سایت ═══════════════════════
def download_full_website(job):
    chat_id = job.chat_id
    url = job.url
    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)
    send_message(chat_id, "🌐 دانلود کامل وب‌سایت...")
    if shutil.which("wget"):
        try:
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            cmd = ["wget", "--adjust-extension", "--span-hosts", "--convert-links",
                   "--page-requisites", "--no-directories", "--directory-prefix", job_dir,
                   "--recursive", "--level=1", "--accept", "html,css,js,jpg,jpeg,png,gif,svg,mp4,webm,pdf",
                   "--user-agent", ua, "--header", "Accept: */*", "--timeout", "30", "--tries", "2", url]
            if subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300).returncode == 0:
                _finish_website_download(job, job_dir)
                return
        except: pass
    send_message(chat_id, "🔄 دانلود با مرورگر مخفی...")
    try:
        ctx = get_or_create_context(chat_id)
        page = ctx.new_page()
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        html = page.content()
        with open(os.path.join(job_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
        page.screenshot(path=os.path.join(job_dir, "screenshot.png"), full_page=True)
        page.close()
        _finish_website_download(job, job_dir)
    except Exception as e:
        send_message(chat_id, f"❌ خطا: {e}")
        job.status = "error"; update_job(job)
        shutil.rmtree(job_dir, ignore_errors=True)

def _finish_website_download(job, job_dir):
    chat_id = job.chat_id
    all_files = []
    for root, _, files in os.walk(job_dir):
        for f in files:
            all_files.append(os.path.join(root, f))
    if not all_files:
        send_message(chat_id, "❌ محتوایی یافت نشد.")
        job.status = "error"; update_job(job)
        return
    zp = os.path.join(job_dir, "website.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in all_files:
            zf.write(fp, os.path.relpath(fp, job_dir))
    parts = split_file_binary(zp, "website", ".zip") if os.path.getsize(zp) > ZIP_PART_SIZE else [zp]
    instr = os.path.join(job_dir, "merge.txt")
    with open(instr, "w") as f:
        f.write("همه‌ی فایل‌ها را دانلود کنید، سپس فایل .001 را با WinRAR یا 7-Zip باز کنید.")
    send_document(chat_id, instr, caption="📝 راهنما")
    for idx, p in enumerate(parts, 1):
        send_document(chat_id, p, caption=f"🌐 پارت {idx}/{len(parts)}")
    job.status = "done"; update_job(job)
    shutil.rmtree(job_dir, ignore_errors=True)
# ═══════════════════════ صف و Worker ═══════════════════════
QUEUE_FILE = "queue.json"
def load_queue():
    try:
        with open(QUEUE_FILE) as f: return json.load(f)
    except: return []
def save_queue(data): 
    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w") as f: json.dump(data, f)
    os.replace(tmp, QUEUE_FILE)
def enqueue(job: Job):
    with queue_lock:
        q = load_queue()
        q.append(asdict(job))
        save_queue(q)
def pop_queued():
    with queue_lock:
        q = load_queue()
        for i, item in enumerate(q):
            if item["status"] == "queued":
                job = Job(**item)
                q[i]["status"] = "running"
                q[i]["updated_at"] = time.time()
                q[i]["started_at"] = time.time()
                save_queue(q)
                return job
    return None
def find_job(jid):
    for item in load_queue():
        if item["job_id"] == jid: return Job(**item)
    return None
def update_job(job):
    with queue_lock:
        q = load_queue()
        for i, item in enumerate(q):
            if item["job_id"] == job.job_id:
                q[i] = asdict(job)
                save_queue(q)
                return
        q.append(asdict(job))
        save_queue(q)
def job_queue_position(jid):
    q = load_queue()
    pos = 1
    for item in q:
        if item["status"] == "queued":
            if item["job_id"] == jid: return pos
            pos += 1
    return -1

def count_user_jobs(chat_id: int) -> int:
    q = load_queue()
    count = 0
    for item in q:
        if item["chat_id"] == chat_id and item["status"] in ("queued", "running"):
            count += 1
    return count

def kill_all_user_jobs(chat_id: int):
    with queue_lock:
        q = load_queue()
        for item in q:
            if item["chat_id"] == chat_id and item["status"] in ("queued", "running"):
                item["status"] = "cancelled"
                item["updated_at"] = time.time()
        save_queue(q)

WORKERS_FILE = "workers.json"
def load_workers():
    try:
        with open(WORKERS_FILE) as f: return json.load(f)
    except: return [asdict(WorkerInfo(i)) for i in range(WORKER_COUNT)]
def save_workers(data): 
    tmp = WORKERS_FILE + ".tmp"
    with open(tmp, "w") as f: json.dump(data, f)
    os.replace(tmp, WORKERS_FILE)
def find_idle_worker():
    with workers_lock:
        for w in load_workers():
            if w["status"] == "idle": return WorkerInfo(**w)
    return None
def set_worker_busy(wid, jid):
    with workers_lock:
        wlist = load_workers()
        for w in wlist:
            if w["worker_id"] == wid:
                w["status"] = "busy"; w["current_job_id"] = jid
        save_workers(wlist)
def set_worker_idle(wid):
    with workers_lock:
        wlist = load_workers()
        for w in wlist:
            if w["worker_id"] == wid:
                w["status"] = "idle"; w["current_job_id"] = None
        save_workers(wlist)

def worker_loop(worker_id, stop_event):
    safe_print(f"[Worker {worker_id}] start")
    while not stop_event.is_set():
        if find_idle_worker() and find_idle_worker().worker_id == worker_id:
            job = pop_queued()
            if not job: time.sleep(2); continue
            set_worker_busy(worker_id, job.job_id)
            try: process_job(worker_id, job)
            except Exception as e: safe_print(f"Worker error: {e}"); traceback.print_exc()
            finally: set_worker_idle(worker_id)
        else: time.sleep(2)

# ═══════════════════════ Rate Limiting (محدودیت مصرف) ═══════════════════════
def check_rate_limit(chat_id: int, mode: str, subscription: str, file_size_mb: float = 0) -> Tuple[bool, str]:
    """
    بررسی محدودیت مصرف بر اساس اشتراک.
    برمی‌گرداند: (مجاز است؟, پیام خطا در صورت عدم مجاز)
    """
    now = time.time()
    hour_ago = now - 3600

    # بارگذاری فایل مصرف
    usage_file = "usage.json"
    try:
        with open(usage_file) as f:
            all_usage = json.load(f)
    except:
        all_usage = {}

    key = str(chat_id)
    if key not in all_usage:
        all_usage[key] = {}

    # پاکسازی رکوردهای قدیمی
    user_usage = all_usage[key]
    user_usage = {k: [t for t in v if t > hour_ago] for k, v in user_usage.items()}
    all_usage[key] = user_usage

    # تعریف محدودیت‌ها بر اساس اشتراک
    limits = {
        "bronze": {
            "screenshot": 1, "2x_screenshot": 0, "4k_screenshot": 0,
            "download": 2, "download_max_mb": 100,
            "record_video": 0, "scan_videos": 0, "scan_downloads": 0,
            "download_website": 0, "extract_commands": 0, "browser": 1
        },
        "plus": {
            "screenshot": 99, "2x_screenshot": 2, "4k_screenshot": 2,
            "download": 5, "download_max_mb": 600,
            "record_video": 3, "scan_videos": 3, "scan_downloads": 1,
            "download_website": 0, "extract_commands": 1, "browser": 99
        },
        "pro": {
            "screenshot": 999, "2x_screenshot": 999, "4k_screenshot": 999,
            "download": 999, "download_max_mb": 99999,
            "record_video": 999, "scan_videos": 999, "scan_downloads": 999,
            "download_website": 3, "extract_commands": 999, "browser": 999
        }
    }

    tier_limits = limits.get(subscription, limits["bronze"])

    # حالت‌های مختلف
    mode_key = mode
    if mode in ("browser", "browser_click"): mode_key = "browser"
    elif mode in ("2x_screenshot", "4k_screenshot"): pass
    elif mode == "screenshot": pass
    elif mode == "download": mode_key = "download"
    elif mode == "record_video": mode_key = "record_video"
    elif mode == "scan_videos": mode_key = "scan_videos"
    elif mode == "scan_downloads": mode_key = "scan_downloads"
    elif mode == "download_website": mode_key = "download_website"
    elif mode == "extract_commands": mode_key = "extract_commands"
    else: mode_key = "other"

    max_allowed = tier_limits.get(mode_key, 0)
    if max_allowed == 0:
        return False, "⛔ اشتراک شما اجازه استفاده از این قابلیت را ندارد."

    current_usage = len(user_usage.get(mode_key, []))
    if current_usage >= max_allowed:
        return False, f"⛔ محدودیت ساعتی ({max_allowed} بار). لطفاً بعداً تلاش کنید."

    # چک حجم برای دانلود
    if mode_key == "download":
        max_mb = tier_limits.get("download_max_mb", 100)
        if file_size_mb > max_mb:
            return False, f"⛔ حجم فایل ({file_size_mb:.1f}MB) بیش از حد مجاز ({max_mb}MB) است."

    # ثبت مصرف
    if mode_key not in user_usage:
        user_usage[mode_key] = []
    user_usage[mode_key].append(now)
    all_usage[key] = user_usage

    try:
        with open(usage_file, "w") as f:
            json.dump(all_usage, f)
    except: pass

    return True, ""

# ═══════════════════════ هستهٔ پردازش Job ═══════════════════════
def process_job(worker_id, job):
    chat_id = job.chat_id
    session = get_session(chat_id)

    if job.mode == "download_execute":
        job_dir = os.path.join("jobs_data", job.job_id)
        os.makedirs(job_dir, exist_ok=True)
        try: execute_download(job, job_dir)
        except Exception as e:
            send_message(chat_id, f"❌ خطا: {e}")
            job.status = "error"; update_job(job)
        finally: shutil.rmtree(job_dir, ignore_errors=True)
        return

    if job.mode == "download_website":
        if not session.is_admin:
            allowed, msg = check_rate_limit(chat_id, "download_website", session.subscription)
            if not allowed:
                send_message(chat_id, msg); job.status = "cancelled"; update_job(job); return
        download_full_website(job)
        return
    if job.mode == "blind_download":
        handle_blind_download(job)
        return
    if job.mode == "record_video":
        if not session.is_admin:
            allowed, msg = check_rate_limit(chat_id, "record_video", session.subscription)
            if not allowed:
                send_message(chat_id, msg); job.status = "cancelled"; update_job(job); return
        handle_record_video(job)
        return
    if job.mode == "scan_videos":
        if not session.is_admin:
            allowed, msg = check_rate_limit(chat_id, "scan_videos", session.subscription)
            if not allowed:
                send_message(chat_id, msg); job.status = "cancelled"; update_job(job); return
        handle_scan_videos(job)
        return
    if job.mode == "scan_downloads":
        if not session.is_admin:
            allowed, msg = check_rate_limit(chat_id, "scan_downloads", session.subscription)
            if not allowed:
                send_message(chat_id, msg); job.status = "cancelled"; update_job(job); return
        handle_scan_downloads(job)
        return
    if job.mode == "extract_commands":
        if not session.is_admin:
            allowed, msg = check_rate_limit(chat_id, "extract_commands", session.subscription)
            if not allowed:
                send_message(chat_id, msg); job.status = "cancelled"; update_job(job); return
        handle_extract_commands(job)
        return
    if job.mode == "download_all_found":
        handle_download_all_found(job)
        return

    session.current_job_id = job.job_id
    set_session(session)
    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        if session.cancel_requested: raise InterruptedError("cancel")
        if job.mode == "screenshot":
            if not session.is_admin:
                allowed, msg = check_rate_limit(chat_id, "screenshot", session.subscription)
                if not allowed:
                    send_message(chat_id, msg); job.status = "cancelled"; update_job(job); return
            send_message(chat_id, f"📸 اسکرین‌شات...")
            ctx = get_or_create_context(chat_id)
            spath = os.path.join(job_dir, "screenshot.png")
            screenshot_full(ctx, job.url, spath)
            send_document(chat_id, spath, caption="✅ اسکرین‌شات (مرحله ۱)")
            # دکمه‌های مرحله بعد فقط برای پلاس و پرو
            if session.subscription in ("plus", "pro", "admin"):
                kb = {"inline_keyboard": [
                    [{"text": "🔍 2x Zoom", "callback_data": f"req2x_{job.job_id}"},
                     {"text": "🖼️ 4K", "callback_data": f"req4k_{job.job_id}"}]
                ]}
                send_message(chat_id, "کیفیت بالاتر:", reply_markup=kb)
            job.status = "done"; update_job(job)
        elif job.mode == "2x_screenshot":
            if not session.is_admin:
                allowed, msg = check_rate_limit(chat_id, "2x_screenshot", session.subscription)
                if not allowed:
                    send_message(chat_id, msg); job.status = "cancelled"; update_job(job); return
            send_message(chat_id, "🔍 2x Zoom...")
            ctx = get_or_create_context(chat_id)
            spath = os.path.join(job_dir, "screenshot_2x.png")
            screenshot_2x(ctx, job.url, spath)
            send_document(chat_id, spath, caption="✅ اسکرین‌شات 2x (مرحله ۲)")
            if session.subscription in ("plus", "pro", "admin"):
                kb = {"inline_keyboard": [[{"text": "🖼️ 4K", "callback_data": f"req4k_{job.job_id}"}]]}
                send_message(chat_id, "کیفیت بالاتر:", reply_markup=kb)
            job.status = "done"; update_job(job)
        elif job.mode == "4k_screenshot":
            if not session.is_admin:
                allowed, msg = check_rate_limit(chat_id, "4k_screenshot", session.subscription)
                if not allowed:
                    send_message(chat_id, msg); job.status = "cancelled"; update_job(job); return
            send_message(chat_id, "🖼️ 4K...")
            ctx = get_or_create_context(chat_id)
            spath = os.path.join(job_dir, "screenshot_4k.png")
            screenshot_4k(ctx, job.url, spath)
            send_document(chat_id, spath, caption="✅ اسکرین‌شات 4K (مرحله ۳)")
            job.status = "done"; update_job(job)
        elif job.mode == "download":
            handle_download(job, job_dir)
        elif job.mode in ("browser", "browser_click"):
            if not session.is_admin:
                allowed, msg = check_rate_limit(chat_id, "browser", session.subscription)
                if not allowed:
                    send_message(chat_id, msg); job.status = "cancelled"; update_job(job); return
            handle_browser(job, job_dir)
        else:
            send_message(chat_id, "❌ نامعتبر")
            job.status = "error"; update_job(job)
    except InterruptedError:
        send_message(chat_id, "⏹️ لغو شد.")
        job.status = "cancelled"; update_job(job)
    except Exception as e:
        send_message(chat_id, f"❌ خطا: {e}")
        job.status = "error"; update_job(job)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        final = find_job(job.job_id)
        if final and final.status in ("done","error","cancelled"):
            s = get_session(chat_id)
            if s.state != "browsing":
                s.state = "idle"; s.current_job_id = None; s.cancel_requested = False
                set_session(s)
                send_message(chat_id, "🔄 آماده.", reply_markup=main_menu_keyboard(s.is_admin, s.subscription))

# ═══════════════════════ دانلود هوشمند ═══════════════════════
def handle_download(job, job_dir):
    chat_id = job.chat_id
    session = get_session(chat_id)
    url = job.url
    if is_direct_file_url(url):
        direct_link = url
    else:
        send_message(chat_id, "🔎 جستجوی فایل...")
        direct_link = crawl_for_download_link(url)
        if not direct_link:
            send_message(chat_id, "⚠️ دانلود کور...")
            job.mode = "blind_download"; job.url = url
            update_job(job); handle_blind_download(job)
            return

    # تخمین حجم فایل و چک Rate Limit
    size_str = "نامشخص"; size_mb = 0
    try:
        head = requests.head(direct_link, timeout=10, allow_redirects=True)
        size = head.headers.get("Content-Length")
        if size:
            size_mb = int(size) / (1024 * 1024)
            size_str = f"{size_mb:.2f} MB"
    except: pass

    if not session.is_admin:
        allowed, msg = check_rate_limit(chat_id, "download", session.subscription, size_mb)
        if not allowed:
            send_message(chat_id, msg); job.status = "cancelled"; update_job(job); return

    fname = get_filename_from_url(direct_link)

    kb = {"inline_keyboard": [
        [{"text": "📦 ZIP", "callback_data": f"dlzip_{job.job_id}"},
         {"text": "📄 اصلی", "callback_data": f"dlraw_{job.job_id}"}],
        [{"text": "❌ لغو", "callback_data": f"canceljob_{job.job_id}"}]
    ]}
    send_message(chat_id, f"📄 {fname} ({size_str})", reply_markup=kb)
    job.status = "awaiting_user"
    job.extra = {"direct_link": direct_link, "filename": fname}
    update_job(job)

def download_and_stream(url, fname, job_dir, chat_id):
    base, ext = os.path.splitext(fname)
    buf = b""; idx = 1
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent":"Mozilla/5.0"}) as r:
        for chunk in r.iter_content(chunk_size=8192):
            buf += chunk
            while len(buf) >= ZIP_PART_SIZE:
                part = buf[:ZIP_PART_SIZE]; buf = buf[ZIP_PART_SIZE:]
                pname = f"{base}.part{idx:03d}{ext}"
                ppath = os.path.join(job_dir, pname)
                with open(ppath, "wb") as f: f.write(part)
                send_document(chat_id, ppath, caption=f"⚡ پارت {idx}")
                os.remove(ppath)
                idx += 1
        if buf:
            pname = f"{base}.part{idx:03d}{ext}"; ppath = os.path.join(job_dir, pname)
            with open(ppath, "wb") as f: f.write(buf)
            send_document(chat_id, ppath, caption=f"⚡ پارت {idx}")
            os.remove(ppath)

def execute_download(job, job_dir):
    chat_id = job.chat_id
    extra = job.extra
    session = get_session(chat_id)
    mode = session.settings.default_download_mode
    pack_zip = extra.get("pack_zip", False)
    if mode == "stream" and pack_zip:
        send_message(chat_id, "📦 ZIP با حالت سریع ممکن نیست؛ دانلود عادی انجام می‌شود.")
        mode = "store"
    if mode == "stream":
        send_message(chat_id, "⚡ دانلود همزمان...")
        download_and_stream(extra["direct_link"], extra["filename"], job_dir, chat_id)
        job.status = "done"; update_job(job)
        return

    fname = extra["filename"]
    if "file_path" in extra:
        fpath = extra["file_path"]
    else:
        fpath = os.path.join(job_dir, fname)
        send_message(chat_id, "⏳ دانلود...")
        with requests.get(extra["direct_link"], stream=True, timeout=120, headers={"User-Agent":"Mozilla/5.0"}) as r:
            with open(fpath, "wb") as f:
                for c in r.iter_content(8192): f.write(c)

    if not os.path.exists(fpath):
        send_message(chat_id, "❌ فایل یافت نشد."); job.status = "error"; update_job(job); return

    if pack_zip:
        parts = create_zip_and_split(fpath, fname); label = "ZIP"
    else:
        base, ext = os.path.splitext(fname)
        parts = split_file_binary(fpath, base, ext); label = "اصلی"
    if not parts:
        send_message(chat_id, "❌ خطا در تقسیم فایل."); job.status = "error"; update_job(job); return

    instr = os.path.join(job_dir, "merge.txt")
    with open(instr, "w") as f:
        if pack_zip: f.write("همه‌ی فایل‌ها را دانلود کنید، سپس فایل .001 را با WinRAR یا 7-Zip باز کنید.")
        else: f.write(f"برای ادغام: copy /b {'+'.join([os.path.basename(p) for p in parts])} {fname}")
    send_document(chat_id, instr, caption="📝 راهنما")
    for idx, p in enumerate(parts, 1):
        send_document(chat_id, p, caption=f"{label} پارت {idx}/{len(parts)}")
    job.status = "done"; update_job(job)

def handle_blind_download(job):
    chat_id = job.chat_id
    session = get_session(chat_id)
    url = job.url
    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)
    send_message(chat_id, "⏳ دانلود اولیه...")
    try:
        with requests.get(url, stream=True, timeout=120, headers={"User-Agent":"Mozilla/5.0"}) as r:
            ct = r.headers.get("Content-Type", "application/octet-stream")
            fname = get_filename_from_url(url)
            if '.' not in fname:
                if "video" in ct: fname += ".mp4"
                elif "pdf" in ct: fname += ".pdf"
                else: fname += ".bin"
            fpath = os.path.join(job_dir, fname)
            with open(fpath, "wb") as f:
                for c in r.iter_content(8192): f.write(c)
        if not os.path.exists(fpath):
            send_message(chat_id, "❌ فایل دانلود نشد."); job.status = "error"; update_job(job); return
        size_mb = os.path.getsize(fpath) / (1024 * 1024)
        size_str = f"{size_mb:.2f} MB"

        if not session.is_admin:
            allowed, msg = check_rate_limit(chat_id, "download", session.subscription, size_mb)
            if not allowed:
                send_message(chat_id, msg); job.status = "cancelled"; update_job(job); return

        text = f"📄 فایل (کور): {fname} ({size_str})"
        kb = {"inline_keyboard": [
            [{"text":"📦 ZIP","callback_data":f"dlblindzip_{job.job_id}"},
             {"text":"📄 اصلی","callback_data":f"dlblindra_{job.job_id}"}],
            [{"text":"❌ لغو","callback_data":f"canceljob_{job.job_id}"}]
        ]}
        send_message(chat_id, text, reply_markup=kb)
        job.status = "awaiting_user"
        job.extra = {"file_path": fpath, "filename": fname}
        update_job(job)
    except Exception as e:
        send_message(chat_id, f"❌ دانلود کور ناموفق: {e}")
        job.status = "error"; update_job(job)
        shutil.rmtree(job_dir, ignore_errors=True)
# ═══════════════════════ مدیریت پیام و Callback ═══════════════════════
def handle_message(chat_id, text):
    session = get_session(chat_id)
    text = text.strip()

    # 💀 دستور /kill – اولویت مطلق
    if text == "/kill":
        if not session.is_pro and not session.is_admin:
            send_message(chat_id, "⛔ دسترسی غیرمجاز.")
            return
        kill_all_user_jobs(chat_id)
        close_user_context(chat_id)
        was_pro = session.is_pro
        was_admin = session.is_admin
        was_sub = session.subscription
        session = SessionState(chat_id=chat_id)
        session.is_pro = was_pro
        session.is_admin = was_admin
        session.subscription = was_sub
        session.state = "idle"
        session.click_counter = 0
        set_session(session)
        send_message(chat_id, "💀 تمام فعالیت‌ها متوقف و وضعیت به روز اول برگردانده شد.",
                     reply_markup=main_menu_keyboard(session.is_admin, session.subscription))
        return

    # ⏱️ دستور /status
    if text == "/status":
        if not session.is_pro and not session.is_admin:
            send_message(chat_id, "⛔ دسترسی غیرمجاز.")
            return
        running_job = None
        for item in load_queue():
            if item["chat_id"] == chat_id and item["status"] == "running":
                running_job = Job(**item)
                break
        if not running_job:
            send_message(chat_id, "ℹ️ هیچ فرایندی در حال اجرا نیست.")
            return
        elapsed = time.time() - (running_job.started_at or running_job.created_at)
        estimates = {"screenshot": 30, "download": 60, "scan": 120, "record": 180}
        est = 60
        for key, val in estimates.items():
            if key in running_job.mode:
                est = val; break
        remaining = max(0, est - elapsed)
        msg = f"⏱️ **وضعیت فرایند**\n\n📌 شناسه: `{running_job.job_id[:8]}`\n🔧 حالت: `{running_job.mode}`\n⏳ زمان سپری‌شده: {elapsed:.0f} ثانیه\n🕒 زمان تخمینی باقی‌مانده: {remaining:.0f} ثانیه"
        kb = {"inline_keyboard": [[{"text": "❌ لغو این فرایند", "callback_data": f"canceljob_{running_job.job_id}"}]]}
        send_message(chat_id, msg, reply_markup=kb)
        return

    if text == "/start":
        session.state = "idle"; session.click_counter = 0; set_session(session)
        if session.is_admin or session.is_pro:
            send_message(chat_id, "منوی اصلی:", reply_markup=main_menu_keyboard(session.is_admin, session.subscription))
        else:
            send_message(chat_id, "👋 کد اشتراک خود را وارد کنید:")
        return

    if text == "/cancel":
        session.state = "idle"; session.cancel_requested = True; session.current_job_id = None
        session.click_counter = 0; set_session(session)
        close_user_context(chat_id)
        send_message(chat_id, "⏹️ لغو شد.", reply_markup=main_menu_keyboard(session.is_admin, session.subscription))
        return

    # فعال‌سازی اشتراک با کد
    if not session.is_pro and not session.is_admin:
        sub = activate_subscription(chat_id, text)
        if sub:
            send_message(chat_id, f"✅ اشتراک **{sub.upper()}** فعال شد!", reply_markup=main_menu_keyboard(session.is_admin, sub))
        else:
            send_message(chat_id, "⛔ کد نامعتبر")
        return

    # 📋 حالت browsing: قبول دستورهای /a، /o، /d، /H و /Live_
    if session.state == "browsing":
        if session.text_links and text in session.text_links:
            url = session.text_links.pop(text)
            set_session(session)
            if text.startswith("/o") or text.startswith("/d"):
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download", url=url))
            elif text.startswith("/H"):
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="browser", url=url))
            elif text.startswith("/Live_"):
                handle_live_command(chat_id, text, url)
            else:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="browser", url=url))
        return

    # 🎭 حالت انتظار Live Command
    if session.state == "waiting_live_command":
        if text.startswith("/Live_"):
            parts = text[6:]
            need_scroll = parts.endswith("_S")
            if need_scroll:
                parts = parts[:-2]
            # اگر با http شروع بشه، لینک مستقیم
            if parts.startswith("http://") or parts.startswith("https://"):
                url = parts
            else:
                # جستجو در text_links
                url = None
                if session.text_links and parts in session.text_links:
                    url = session.text_links[parts]
                else:
                    for link in (session.browser_links or []):
                        if parts in link.get("href", ""):
                            url = link["href"]; break
            if url:
                handle_live_command(chat_id, text, url, need_scroll)
            else:
                send_message(chat_id, "❌ دستور Live نامعتبر یا منقضی شده است.")
        else:
            send_message(chat_id, "❌ لطفاً یک دستور Live معتبر ارسال کنید (مثال: /Live_d6h7s).")
        return

    # حالت انتظار عدد برای زمان ضبط
    if session.state == "waiting_record_time":
        try:
            val = int(text)
            if 1 <= val <= 30:
                session.settings.record_time = val
                session.state = "idle"
                set_session(session)
                send_message(chat_id, f"⏱️ زمان ضبط روی {val} ثانیه تنظیم شد.",
                             reply_markup=settings_keyboard(session.settings))
            else:
                send_message(chat_id, "❌ لطفاً عددی بین ۱ تا ۳۰ وارد کنید.")
        except:
            send_message(chat_id, "❌ لطفاً یک عدد معتبر بین ۱ تا ۳۰ وارد کنید.")
        return

    # حالت‌های انتظار URL
    if session.state.startswith("waiting_url_"):
        url = text
        if not (url.startswith("http://") or url.startswith("https://")):
            send_message(chat_id, "❌ URL نامعتبر"); return
        mode_map = {
            "waiting_url_screenshot": "screenshot",
            "waiting_url_download": "download",
            "waiting_url_browser": "browser"
        }
        mode = mode_map.get(session.state, "screenshot")
        job = Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode=mode, url=url)
        if not session.is_admin and count_user_jobs(chat_id) >= 2:
            send_message(chat_id, "🛑 صف پردازش پر است (حداکثر ۲). لطفاً کمی صبر کنید یا /kill را بزنید.")
            return
        enqueue(job)
        session.state = "idle"; session.current_job_id = job.job_id
        set_session(session)
        pos = job_queue_position(job.job_id)
        send_message(chat_id, f"✅ در صف (نوبت {pos})" if pos != 1 else "✅ در صف قرار گرفت.")
        return

    send_message(chat_id, "از منو استفاده کنید:", reply_markup=main_menu_keyboard(session.is_admin, session.subscription))

def handle_live_command(chat_id, text, url, need_scroll=False):
    """اجرای عملیات Live: رفتن به صفحه (در صورت لینک) و سپس ضبط"""
    session = get_session(chat_id)
    rec_time = session.settings.record_time

    # اگر url یک لینک کامل باشه، مستقیم برو به اون صفحه
    if url.startswith("http://") or url.startswith("https://"):
        # ایجاد Job جدید برای ضبط
        job_id = str(uuid.uuid4())
        job = Job(job_id=job_id, chat_id=chat_id, mode="record_video", url=url)
        job.extra = {"live_scroll": need_scroll}
        enqueue(job)
        send_message(chat_id, f"🎬 ضبط Live آغاز شد ({rec_time} ثانیه) – رفتن به آدرس داده‌شده...")
        return

    # در غیر این صورت، url یک دکمه در صفحه فعلی
    if not session.browser_url:
        send_message(chat_id, "❌ مرورگری برای اجرای Live باز نیست.")
        return

    ctx = get_or_create_context(chat_id)
    page = ctx.new_page()
    try:
        page.goto(session.browser_url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # کلیک روی دکمه با href برابر url
        page.evaluate(f"""() => {{
            const links = document.querySelectorAll('a[href]');
            for (const a of links) {{
                if (a.href === '{url}') {{
                    a.click();
                    return;
                }}
            }}
        }}""")
        page.wait_for_timeout(3000)

        if need_scroll:
            smooth_scroll_to_video(page)

        # ایجاد Job ضبط
        job_id = str(uuid.uuid4())
        job = Job(job_id=job_id, chat_id=chat_id, mode="record_video", url=page.url)
        enqueue(job)
        send_message(chat_id, f"🎬 ضبط Live آغاز شد ({rec_time} ثانیه)...")
    except Exception as e:
        send_message(chat_id, f"❌ خطا در Live: {e}")
    finally:
        page.close()

def handle_callback(cq):
    cid = cq["id"]; msg = cq.get("message"); data = cq.get("data", "")
    if not msg: return answer_callback_query(cid)
    chat_id = msg["chat"]["id"]
    session = get_session(chat_id)

    # محدودیت کلیک (۵ بار) برای غیر ادمین
    if not session.is_admin:
        if session.click_counter >= 5:
            answer_callback_query(cid, "⛔ به حداکثر کلیک (۵) رسیدید. /cancel را بزنید.", show_alert=True)
            return
        session.click_counter += 1
        set_session(session)

    # منوی اصلی
    if data == "menu_screenshot":
        session.state = "waiting_url_screenshot"; set_session(session); send_message(chat_id, "📸 URL:")
    elif data == "menu_download":
        session.state = "waiting_url_download"; set_session(session); send_message(chat_id, "📥 URL:")
    elif data == "menu_browser":
        session.state = "waiting_url_browser"; set_session(session); send_message(chat_id, "🧭 URL:")
    elif data == "menu_settings":
        send_message(chat_id, "⚙️", reply_markup=settings_keyboard(session.settings))
    elif data == "menu_admin":
        if session.is_admin: admin_panel(chat_id)
        else: answer_callback_query(cid, "دسترسی غیرمجاز")
    elif data == "menu_cancel":
        session.state = "idle"; session.cancel_requested = True; session.current_job_id = None
        session.click_counter = 0; set_session(session)
        close_user_context(chat_id)
        send_message(chat_id, "✅ لغو شد.", reply_markup=main_menu_keyboard(session.is_admin, session.subscription))

    # تنظیمات
    elif data == "set_rec":
        session.state = "waiting_record_time"; set_session(session)
        send_message(chat_id, "⏱️ زمان ضبط را به ثانیه وارد کنید (۱ تا ۳۰):")
    elif data == "set_dlmode":
        session.settings.default_download_mode = "stream" if session.settings.default_download_mode == "store" else "store"
        set_session(session)
        send_message(chat_id, "تنظیمات:", reply_markup=settings_keyboard(session.settings))
    elif data == "set_brwmode":
        session.settings.browser_mode = "media" if session.settings.browser_mode == "text" else "text"
        set_session(session)
        send_message(chat_id, "تنظیمات:", reply_markup=settings_keyboard(session.settings))
    elif data == "set_deep":
        session.settings.deep_scan_mode = "everything" if session.settings.deep_scan_mode == "logical" else "logical"
        set_session(session)
        send_message(chat_id, "تنظیمات:", reply_markup=settings_keyboard(session.settings))
    elif data == "set_recbeh":
        behaviors = ["click", "scroll", "live"]
        current = session.settings.record_behavior
        idx = behaviors.index(current)
        session.settings.record_behavior = behaviors[(idx + 1) % 3]
        set_session(session)
        send_message(chat_id, "تنظیمات:", reply_markup=settings_keyboard(session.settings))
    elif data == "back_main":
        send_message(chat_id, "منوی اصلی:", reply_markup=main_menu_keyboard(session.is_admin, session.subscription))

    # اسکرین‌شات‌های چندمرحله‌ای
    elif data.startswith("req2x_"):
        jid = data[6:]; job = find_job(jid)
        if job and job.status == "done":
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="2x_screenshot", url=job.url))
    elif data.startswith("req4k_"):
        jid = data[6:]; job = find_job(jid)
        if job and job.status == "done":
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="4k_screenshot", url=job.url))

    # دانلود ZIP/اصلی
    elif data.startswith("dlzip_") or data.startswith("dlraw_"):
        jid = data[6:] if data.startswith("dlzip_") else data[6:]; job = find_job(jid)
        if job and job.extra:
            job.extra["pack_zip"] = data.startswith("dlzip_"); job.status = "done"; update_job(job)
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download_execute", url=job.url, extra=job.extra))
    elif data.startswith("dlblindzip_") or data.startswith("dlblindra_"):
        jid = data[11:] if data.startswith("dlblindzip_") else data[11:]; job = find_job(jid)
        if job and job.extra and "file_path" in job.extra:
            job.extra["pack_zip"] = data.startswith("dlblindzip_"); job.status = "done"; update_job(job)
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download_execute", url=job.url, extra=job.extra))
    elif data.startswith("canceljob_"):
        jid = data[10:]; job = find_job(jid)
        if job: job.status = "cancelled"; update_job(job)
        send_message(chat_id, "❌ لغو شد.", reply_markup=main_menu_keyboard(session.is_admin, session.subscription))

    # ناوبری مرورگر
    elif data.startswith("nav_"):
        parts = data.split("_", 2)
        if len(parts) >= 3:
            cb = f"{parts[0]}_{parts[1]}_{parts[2]}"
            with callback_map_lock: url = callback_map.pop(cb, None)
            if url:
                if session.is_admin or count_user_jobs(chat_id) < 2:
                    enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="browser", url=url))
                else:
                    answer_callback_query(cid, "🛑 صف پر است.")
    elif data.startswith("dlvid_"):
        parts = data.split("_", 2)
        if len(parts) >= 3:
            cb = f"{parts[0]}_{parts[1]}_{parts[2]}"
            with callback_map_lock: url = callback_map.pop(cb, None)
            if url:
                if session.is_admin or count_user_jobs(chat_id) < 2:
                    enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download", url=url))
                else:
                    answer_callback_query(cid, "🛑 صف پر است.")

    # اسکن ویدیوها / جستجوی فایل‌ها / استخراج فرامین
    elif data.startswith("scvid_"):
        enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="scan_videos", url=""))
    elif data.startswith("scdl_"):
        enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="scan_downloads", url=""))
    elif data.startswith("extcmd_"):
        enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="extract_commands", url=""))

    # ضبط ویدیو – اگر حالت live باشد، prompt می‌دهیم
    elif data.startswith("recvid_"):
        if session.settings.record_behavior == "live":
            session.state = "waiting_live_command"; set_session(session)
            send_message(chat_id, "🎭 حالت Live فعال است. لطفاً دستور Live را وارد کنید (مثال: /Live_https://... یا /Live_d6h7s):")
        elif session.browser_url:
            if session.is_admin or count_user_jobs(chat_id) < 2:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="record_video", url=session.browser_url))
            else:
                answer_callback_query(cid, "🛑 صف پر است.")

    # دانلود سایت
    elif data.startswith("dlweb_"):
        if session.browser_url:
            if session.is_admin or count_user_jobs(chat_id) < 2:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download_website", url=session.browser_url))
            else:
                answer_callback_query(cid, "🛑 صف پر است.")

    # صفحه‌بندی مرورگر / نتایج دانلود
    elif data.startswith("bpg_"):
        parts = data.split("_")
        if len(parts) == 3:
            page = int(parts[2]); session.browser_page = page; set_session(session)
            send_browser_page(chat_id, page_num=page)
    elif data.startswith("dfpg_"):
        parts = data.split("_")
        if len(parts) == 3:
            page = int(parts[2]); session.found_downloads_page = page; set_session(session)
            send_found_downloads_page(chat_id, page)
    elif data == "close_downloads":
        session.found_downloads = None; session.found_downloads_page = 0; set_session(session)
        send_message(chat_id, "📦 نتایج جستجو بسته شد.", reply_markup=main_menu_keyboard(session.is_admin, session.subscription))

    # دانلود همه فایل‌ها
    elif data.startswith("dlall_"):
        enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download_all_found", url=""))

    # مسدودساز تبلیغات
    elif data.startswith("adblock_"):
        parsed_url = urlparse(session.browser_url or "")
        current_domain = parsed_url.netloc.lower()
        if not current_domain:
            answer_callback_query(cid, "دامنه‌ای برای مسدودسازی شناسایی نشد.")
            return
        if session.ad_blocked_domains is None:
            session.ad_blocked_domains = []
        if current_domain in session.ad_blocked_domains:
            session.ad_blocked_domains.remove(current_domain)
            answer_callback_query(cid, "🛡️ مسدودساز غیرفعال شد.")
        else:
            session.ad_blocked_domains.append(current_domain)
            answer_callback_query(cid, "🛡️ مسدودساز فعال شد.")
        set_session(session)
        send_browser_page(chat_id, page_num=session.browser_page)

    # بستن مرورگر
    elif data.startswith("closebrowser_"):
        close_user_context(chat_id); session.state = "idle"; session.click_counter = 0; set_session(session)
        send_message(chat_id, "🧭 بسته شد.", reply_markup=main_menu_keyboard(session.is_admin, session.subscription))

    else:
        answer_callback_query(cid)

# ═══════════════════════ Polling و Main ═══════════════════════
def polling_loop(stop_event):
    offset = None
    safe_print("[Polling] start")
    while not stop_event.is_set():
        try: updates = get_updates(offset, LONG_POLL_TIMEOUT)
        except Exception as e: safe_print(f"Poll error: {e}"); time.sleep(5); continue
        for upd in updates:
            offset = upd["update_id"] + 1
            if "message" in upd and "text" in upd["message"]:
                handle_message(upd["message"]["chat"]["id"], upd["message"]["text"])
            elif "callback_query" in upd:
                handle_callback(upd["callback_query"])

def main():
    os.makedirs("jobs_data", exist_ok=True)
    stop_event = threading.Event()
    for i in range(WORKER_COUNT):
        threading.Thread(target=worker_loop, args=(i, stop_event), daemon=True).start()
    threading.Thread(target=polling_loop, args=(stop_event,), daemon=True).start()
    safe_print("✅ Bot16 Ultimate اجرا شد")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: stop_event.set()

if __name__ == "__main__":
    main()
