#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات بله – نسخهٔ ۱۵ (Ultimate Pro)
مرورگر دو حالته با stealth، اسکن ویدیو/فایل، دانلودر هوشمند،
اسکرین‌شات 4K چندمرحله‌ای، ضبط ویدیو با سه رفتار (کلیک/اسکرول/لایو)،
استخراج فرامین، جستجوی پیشرفته، پنل ادمین، محدودیت صف و کلیک.
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

PRO_CODES = ["PRO2024A", "PRO2024B", "PRO2024C", "PRO2024D", "PRO2024E"]
ADMIN_CHAT_ID = 46829437

print_lock = threading.Lock()
queue_lock = threading.Lock()
workers_lock = threading.Lock()
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
    default_download_mode: str = "store"        # "store" یا "stream"
    browser_mode: str = "text"                  # "text" یا "media"
    deep_scan_mode: str = "logical"             # "logical" یا "everything"
    record_behavior: str = "click"              # "click", "scroll", "live"

@dataclass
class SessionState:
    chat_id: int
    state: str = "idle"                         # idle, waiting_url_*, browsing, waiting_record_time, waiting_live_command
    is_pro: bool = False
    is_admin: bool = False
    current_job_id: Optional[str] = None
    browser_url: Optional[str] = None
    last_interaction: float = time.time()
    cancel_requested: bool = False
    text_links: Optional[Dict[str, str]] = None   # command (مثل /a..., /o..., /d..., /H..., /Live_...) -> url
    browser_links: Optional[List[Dict[str, str]]] = None
    browser_page: int = 0
    settings: UserSettings = field(default_factory=UserSettings)
    click_counter: int = 0
    ad_blocked_domains: Optional[List[str]] = field(default_factory=list)
    # برای جستجوی فایل (scan downloads)
    found_downloads: Optional[List[Dict[str, str]]] = None   # لیست نتایج
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
    started_at: Optional[float] = None           # برای دستور /status

@dataclass
class WorkerInfo:
    worker_id: int
    current_job_id: Optional[str] = None
    status: str = "idle"

# ═══════════════════════ ذخیره‌سازی محلی ═══════════════════════
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
            s.is_pro = True
        return s
    s = SessionState(chat_id=chat_id)
    if s.chat_id == ADMIN_CHAT_ID:
        s.is_admin = True
        s.is_pro = True
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

# ═══════════════════════ منوها (با تنظیمات جدید) ═══════════════════════
def main_menu_keyboard(is_admin=False):
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
    rec_behavior = {"click": "🖱️ کلیک هوشمند", "scroll": "📜 اسکرول+کلیک", "live": "🎭 لایو کامند"}[settings.record_behavior]
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
                    "--mute-audio"
                ]
            )
        vw = random.choice([412, 390, 414])
        vh = random.choice([915, 844, 896])
        context = _global_browser.new_context(viewport={"width": vw, "height": vh})
        # مدیریت هوشمند پاپ‌آپ
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
    # 1. عناصر visible
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

    # 2. شبکه (HLS/DASH)
    network_urls = []
    def capture(response):
        ct = response.headers.get("content-type", "")
        url = response.url.lower()
        if "mpegurl" in ct or "dash+xml" in ct or url.endswith((".m3u8", ".mpd")):
            network_urls.append(response.url)
    page.on("response", capture)
    page.wait_for_timeout(3000)
    page.remove_listener("response", capture)

    # 3. JSON درون اسکریپت‌ها
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
    if size_bytes and size_bytes > 1024 * 1024:  # بالای 1MB
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
                q[i]["started_at"] = time.time()   # ★ ثبت زمان شروع
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
        download_full_website(job)
        return
    if job.mode == "blind_download":
        handle_blind_download(job)
        return
    if job.mode == "record_video":
        handle_record_video(job)
        return
    if job.mode == "scan_videos":
        handle_scan_videos(job)
        return
    if job.mode == "scan_downloads":
        handle_scan_downloads(job)
        return
    if job.mode == "extract_commands":
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
            send_message(chat_id, f"📸 اسکرین‌شات...")
            ctx = get_or_create_context(chat_id)
            spath = os.path.join(job_dir, "screenshot.png")
            screenshot_full(ctx, job.url, spath)
            send_document(chat_id, spath, caption="✅ اسکرین‌شات (مرحله ۱)")
            kb = {"inline_keyboard": [
                [{"text": "🔍 2x Zoom", "callback_data": f"req2x_{job.job_id}"},
                 {"text": "🖼️ 4K", "callback_data": f"req4k_{job.job_id}"}]
            ]}
            send_message(chat_id, "کیفیت بالاتر:", reply_markup=kb)
            job.status = "done"; update_job(job)
        elif job.mode == "2x_screenshot":
            send_message(chat_id, "🔍 2x Zoom...")
            ctx = get_or_create_context(chat_id)
            spath = os.path.join(job_dir, "screenshot_2x.png")
            screenshot_2x(ctx, job.url, spath)
            send_document(chat_id, spath, caption="✅ اسکرین‌شات 2x (مرحله ۲)")
            kb = {"inline_keyboard": [[{"text": "🖼️ 4K", "callback_data": f"req4k_{job.job_id}"}]]}
            send_message(chat_id, "کیفیت بالاتر:", reply_markup=kb)
            job.status = "done"; update_job(job)
        elif job.mode == "4k_screenshot":
            send_message(chat_id, "🖼️ 4K...")
            ctx = get_or_create_context(chat_id)
            spath = os.path.join(job_dir, "screenshot_4k.png")
            screenshot_4k(ctx, job.url, spath)
            send_document(chat_id, spath, caption="✅ اسکرین‌شات 4K (مرحله ۳)")
            job.status = "done"; update_job(job)
        elif job.mode == "download":
            handle_download(job, job_dir)
        elif job.mode in ("browser", "browser_click"):
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
                send_message(chat_id, "🔄 آماده.", reply_markup=main_menu_keyboard(s.is_admin))

# ═══════════════════════ دانلود هوشمند ═══════════════════════
def handle_download(job, job_dir):
    chat_id = job.chat_id
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

    try:
        head = requests.head(direct_link, timeout=10, allow_redirects=True)
        size = head.headers.get("Content-Length")
        size_str = f"{int(size)/1024/1024:.2f} MB" if size else "نامشخص"
    except: size_str = "نامشخص"
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
        size_str = f"{os.path.getsize(fpath)/1024/1024:.2f} MB"
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
# ═══════════════════════ تشخیص موقعیت ویدیو ═══════════════════════
def find_video_center(page):
    """بزرگترین المان video/iframe قابل مشاهده را پیدا کرده و مختصات مرکز آن را برمی‌گرداند."""
    coords = page.evaluate("""() => {
        const centerX = window.innerWidth / 2;
        const centerY = window.innerHeight / 2;
        let best = null;
        let bestArea = 0;
        
        document.querySelectorAll('video').forEach(v => {
            const rect = v.getBoundingClientRect();
            if (rect.width < 200 || rect.height < 150) return;
            const area = rect.width * rect.height;
            if (area > bestArea) {
                bestArea = area;
                best = { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
            }
        });
        
        document.querySelectorAll('iframe').forEach(f => {
            const rect = f.getBoundingClientRect();
            if (rect.width < 300 || rect.height < 200) return;
            const area = rect.width * rect.height;
            if (area > bestArea) {
                bestArea = area;
                best = { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
            }
        });
        
        return best || { x: centerX, y: centerY };
    }""")
    return coords["x"], coords["y"]

# ═══════════════════════ ضبط ویدیو (سه رفتار) ═══════════════════════
def handle_record_video(job):
    chat_id = job.chat_id
    session = get_session(chat_id)
    url = job.url
    rec_time = session.settings.record_time
    behavior = session.settings.record_behavior
    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)

    behavior_names = {"click": "کلیک هوشمند", "scroll": "اسکرول + کلیک", "live": "لایو کامند"}
    send_message(chat_id, f"🎬 ضبط {rec_time} ثانیه ({behavior_names.get(behavior, behavior)})...")

    try:
        context = _global_browser.new_context(
            viewport={"width": 1280, "height": 720},
            record_video_dir=job_dir,
            record_video_size={"width": 1280, "height": 720}
        )
        page = context.new_page()

        # اعمال فیلتر تبلیغات در صورت فعال بودن
        if session.ad_blocked_domains:
            parsed = urlparse(url)
            if parsed.netloc.lower() in session.ad_blocked_domains:
                page.route("**/*", lambda route: route.abort()
                           if any(ad in route.request.url for ad in AD_DOMAINS)
                           else route.continue_())

        try:
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            if behavior == "scroll":
                # اسکرول برای بیدار کردن Lazy Load
                for _ in range(3):
                    page.evaluate("window.scrollBy(0, 300)")
                    page.wait_for_timeout(500)

            if behavior == "live":
                # منطق live در handle_message مدیریت می‌شود. اینجا فقط حالت پیش‌فرض.
                # اگر به اینجا رسید یعنی کاربر مستقیماً دکمه ضبط را زده و حالت live است
                # بدون دستور خاصی، مانند click رفتار می‌کنیم
                pass

            # کلیک هوشمند (برای click، scroll و live بدون دستور خاص)
            vx, vy = find_video_center(page)
            page.mouse.click(vx, vy)

            # تلاش JS برای play()
            try:
                page.evaluate("() => { const v = document.querySelector('video'); if (v) v.play(); }")
            except: pass

            page.wait_for_timeout(rec_time * 1000)
        finally:
            page.close()
            context.close()

        # پیدا کردن فایل webm
        webm = None
        for f in os.listdir(job_dir):
            if f.endswith('.webm'):
                webm = os.path.join(job_dir, f); break
        if not webm:
            send_message(chat_id, "❌ ویدیویی ضبط نشد.")
            job.status = "error"; update_job(job)
            return

        # تبدیل به mkv در صورت وجود ffmpeg
        if shutil.which('ffmpeg'):
            mkv_path = webm.replace('.webm', '.mkv')
            try:
                subprocess.run(['ffmpeg', '-y', '-i', webm, '-c', 'copy', mkv_path],
                               check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
                final = mkv_path if os.path.exists(mkv_path) else webm
            except:
                final = webm
        else:
            final = webm

        if os.path.getsize(final) > ZIP_PART_SIZE:
            parts = split_file_binary(final, "record", os.path.splitext(final)[1])
            for idx, p in enumerate(parts, 1):
                send_document(chat_id, p, caption=f"🎬 پارت {idx}/{len(parts)}")
        else:
            send_document(chat_id, final, caption="🎬 ویدیوی ضبط‌شده")
        job.status = "done"; update_job(job)
    except Exception as e:
        send_message(chat_id, f"❌ خطا: {e}")
        job.status = "error"; update_job(job)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)

# ═══════════════════════ مرورگر (با دکمه‌های جدید) ═══════════════════════
def handle_browser(job, job_dir):
    chat_id = job.chat_id
    session = get_session(chat_id)
    mode = session.settings.browser_mode
    ctx = get_or_create_context(chat_id)
    page = ctx.new_page()

    # اعمال فیلتر تبلیغات اگر دامنه مسدود باشد
    parsed_url = urlparse(job.url)
    if parsed_url.netloc.lower() in (session.ad_blocked_domains or []):
        page.route("**/*", lambda route: route.abort()
                   if any(ad in route.request.url for ad in AD_DOMAINS)
                   else route.continue_())

    try:
        page.goto(job.url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        spath = os.path.join(job_dir, "browser.png")
        page.screenshot(path=spath, full_page=True)
        links, video_urls = extract_clickable_and_media(page, mode)

        all_links = []
        for typ, text, href in links:
            all_links.append({"type": typ, "text": text[:25], "href": href})
        if mode == "media":
            clean_videos = [v for v in video_urls if not any(ad in v for ad in AD_DOMAINS)]
            for vurl in clean_videos:
                all_links.append({"type": "video", "text": "🎬 ویدیو", "href": vurl})

        session.state = "browsing"
        session.browser_url = job.url
        session.browser_links = all_links
        session.browser_page = 0
        set_session(session)
        send_browser_page(chat_id, spath, job.url, 0)
        job.status = "done"; update_job(job)
    finally:
        page.close()

def send_browser_page(chat_id, image_path=None, url="", page_num=0):
    session = get_session(chat_id)
    all_links = session.browser_links or []
    per_page = 10
    start = page_num * per_page
    end = min(start + per_page, len(all_links))
    page_links = all_links[start:end]

    keyboard_rows = []
    idx = start
    row = []
    for link in page_links:
        label = link["text"][:20]
        cb = f"nav_{chat_id}_{idx}" if link["type"] != "video" else f"dlvid_{chat_id}_{idx}"
        with callback_map_lock: callback_map[cb] = link["href"]
        row.append({"text": label, "callback_data": cb})
        if len(row) == 2:
            keyboard_rows.append(row); row = []
        idx += 1
    if row: keyboard_rows.append(row)

    nav = []
    if page_num > 0: nav.append({"text": "◀️", "callback_data": f"bpg_{chat_id}_{page_num-1}"})
    if end < len(all_links): nav.append({"text": "▶️", "callback_data": f"bpg_{chat_id}_{page_num+1}"})
    if nav: keyboard_rows.append(nav)

    # دکمه‌های ویژه بر اساس حالت
    if session.settings.browser_mode == "media":
        keyboard_rows.append([{"text": "🎬 اسکن ویدیوها", "callback_data": f"scvid_{chat_id}"}])
        parsed_url = urlparse(url)
        current_domain = parsed_url.netloc.lower()
        is_blocked = current_domain in (session.ad_blocked_domains or [])
        ad_text = "🛡️ تبلیغات: روشن" if is_blocked else "🛡️ تبلیغات: خاموش"
        keyboard_rows.append([{"text": ad_text, "callback_data": f"adblock_{chat_id}"}])
    else:
        keyboard_rows.append([{"text": "📦 جستجوی فایل‌ها", "callback_data": f"scdl_{chat_id}"}])

    # دکمه‌های مشترک
    keyboard_rows.append([{"text": "📋 استخراج فرامین", "callback_data": f"extcmd_{chat_id}"}])
    keyboard_rows.append([{"text": "🎬 ضبط", "callback_data": f"recvid_{chat_id}"}])
    keyboard_rows.append([{"text": "🌐 دانلود سایت", "callback_data": f"dlweb_{chat_id}"}])
    keyboard_rows.append([{"text": "❌ بستن", "callback_data": f"closebrowser_{chat_id}"}])

    kb = {"inline_keyboard": keyboard_rows}
    if image_path:
        send_document(chat_id, image_path, caption=f"🌐 {url}")
    send_message(chat_id, f"صفحه {page_num+1}/{math.ceil(len(all_links)/per_page)}", reply_markup=kb)

    extra = all_links[end:]
    if extra:
        cmds = {}
        lines = ["🔹 لینک‌های بیشتر:"]
        for i, link in enumerate(extra):
            cmd = f"/a{hashlib.md5(link['href'].encode()).hexdigest()[:5]}"
            cmds[cmd] = link['href']
            lines.append(f"{cmd} : {link['text']}")
        send_message(chat_id, "\n".join(lines))
        session.text_links = cmds
        set_session(session)

# ═══════════════════════ اسکن ویدیوها ═══════════════════════
def handle_scan_videos(job):
    chat_id = job.chat_id
    session = get_session(chat_id)
    ctx = get_or_create_context(chat_id)
    page = ctx.new_page()
    try:
        page.goto(session.browser_url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        videos = scan_videos_smart(page)
        if not videos:
            send_message(chat_id, "🚫 هیچ ویدیویی یافت نشد.")
            job.status = "done"; update_job(job)
            return
        lines = [f"🎬 **{len(videos)} ویدیو یافت شد:**"]
        cmds = {}
        for i, vid in enumerate(videos[:15]):
            cmd = f"/o{hashlib.md5(vid['href'].encode()).hexdigest()[:5]}"
            cmds[cmd] = vid['href']
            lines.append(f"{i+1}. {vid['text']}")
            lines.append(f"   📥 {cmd}")
        send_message(chat_id, "\n".join(lines))
        session.text_links = {**session.text_links, **cmds} if session.text_links else cmds
        set_session(session)
        job.status = "done"; update_job(job)
    except Exception as e:
        send_message(chat_id, f"❌ خطا: {e}")
        job.status = "error"; update_job(job)
    finally:
        page.close()

# ═══════════════════════ جستجوی فایل‌ها (سه مرحله‌ای) ═══════════════════════
def handle_scan_downloads(job):
    chat_id = job.chat_id
    session = get_session(chat_id)
    url = session.browser_url
    if not url:
        send_message(chat_id, "❌ صفحه‌ای برای جستجو باز نیست.")
        return

    deep_mode = session.settings.deep_scan_mode
    send_message(chat_id, f"🔎 جستجوی فایل‌ها (حالت: {deep_mode})...")

    found_links: Set[str] = set()
    all_results: List[Dict[str, str]] = []

    def add_result(link: str):
        if link in found_links: return
        found_links.add(link)
        fname = get_filename_from_url(link)
        size_str = "نامشخص"
        size_bytes = None
        try:
            head = requests.head(link, timeout=5, allow_redirects=True)
            if head.headers.get("Content-Length"):
                size_bytes = int(head.headers.get("Content-Length"))
                size_str = f"{size_bytes/1024/1024:.2f} MB"
        except: pass

        # فیلتر منطقی
        if deep_mode == "logical" and not is_logical_download(link, size_bytes):
            return

        all_results.append({"name": fname[:35], "url": link, "size": size_str})

    # --- مرحله ۱: مستقیم از صفحه (۱۰ ثانیه) ---
    start_time = time.time()
    try:
        ctx = get_or_create_context(chat_id)
        page = ctx.new_page()
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        all_hrefs = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a[href]'))
                        .map(a => a.href).filter(h => h.startsWith('http'));
        }""")
        page.close()
        for href in all_hrefs:
            parsed = urlparse(href)
            if any(ad in parsed.netloc for ad in AD_DOMAINS): continue
            if any(kw in href.lower() for kw in BLOCKED_AD_KEYWORDS): continue
            if is_direct_file_url(href):
                add_result(href)
        elapsed = time.time() - start_time
        if all_results:
            send_message(chat_id, f"✅ مرحله ۱: {len(all_results)} فایل ({elapsed:.1f}s)")
    except Exception as e:
        safe_print(f"scan_downloads stage1 error: {e}")

    # --- مرحله ۲: کراول سبک (تا ۶۰ ثانیه) ---
    if not all_results and time.time() - start_time < 60:
        send_message(chat_id, "🔄 مرحله ۲: کراول سبک...")
        try:
            s = requests.Session()
            s.headers.update({"User-Agent": "Mozilla/5.0"})
            resp = s.get(url, timeout=10)
            if resp.status_code == 200 and "text/html" in resp.headers.get("Content-Type", ""):
                soup = BeautifulSoup(resp.text, "html.parser")
                links_to_crawl = []
                for a in soup.find_all("a", href=True):
                    href = urljoin(url, a["href"])
                    parsed = urlparse(href)
                    if any(ad in parsed.netloc for ad in AD_DOMAINS): continue
                    if any(kw in href.lower() for kw in BLOCKED_AD_KEYWORDS): continue
                    if is_direct_file_url(href):
                        add_result(href)
                    else:
                        links_to_crawl.append(href)

                for link in links_to_crawl[:15]:
                    if time.time() - start_time > 60: break
                    found = crawl_for_download_link(link, max_depth=1, max_pages=5, timeout_seconds=10)
                    if found:
                        add_result(found)
                elapsed = time.time() - start_time
                send_message(chat_id, f"✅ مرحله ۲: مجموعاً {len(all_results)} فایل ({elapsed:.1f}s)")
        except Exception as e:
            safe_print(f"scan_downloads stage2 error: {e}")

    if not all_results:
        send_message(chat_id, "🚫 هیچ فایل قابل دانلودی یافت نشد.")
        job.status = "done"; update_job(job)
        return

    # ذخیره نتایج در session برای صفحه‌بندی و دانلود همه
    session.found_downloads = all_results
    session.found_downloads_page = 0
    set_session(session)

    # نمایش صفحه اول نتایج
    send_found_downloads_page(chat_id, 0)
    job.status = "done"; update_job(job)

def send_found_downloads_page(chat_id, page_num=0):
    session = get_session(chat_id)
    all_results = session.found_downloads or []
    per_page = 10
    start = page_num * per_page
    end = min(start + per_page, len(all_results))
    page_results = all_results[start:end]

    lines = [f"📦 **فایل‌های یافت‌شده (صفحه {page_num+1}/{math.ceil(len(all_results)/per_page)}):**"]
    cmds = {}
    for i, f in enumerate(page_results):
        idx = start + i
        cmd = f"/d{hashlib.md5(f['url'].encode()).hexdigest()[:5]}"
        cmds[cmd] = f['url']
        lines.append(f"{idx+1}. {f['name']} ({f['size']})")
        lines.append(f"   📥 {cmd}    🔗 {f['url'][:60]}")

    keyboard_rows = []
    nav = []
    if page_num > 0:
        nav.append({"text": "◀️ قبلی", "callback_data": f"dfpg_{chat_id}_{page_num-1}"})
    if end < len(all_results):
        nav.append({"text": "بعدی ▶️", "callback_data": f"dfpg_{chat_id}_{page_num+1}"})
    if nav: keyboard_rows.append(nav)
    keyboard_rows.append([{"text": "📦 دانلود همه (ZIP)", "callback_data": f"dlall_{chat_id}"}])
    keyboard_rows.append([{"text": "❌ بستن", "callback_data": "close_downloads"}])

    send_message(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": keyboard_rows})
    session.text_links = {**session.text_links, **cmds} if session.text_links else cmds
    set_session(session)

# ═══════════════════════ استخراج فرامین ═══════════════════════
def handle_extract_commands(job):
    chat_id = job.chat_id
    session = get_session(chat_id)
    all_links = session.browser_links or []
    if not all_links:
        send_message(chat_id, "🚫 لینکی برای استخراج وجود ندارد.")
        job.status = "done"; update_job(job)
        return

    cmds = {}
    lines = [f"📋 **{len(all_links)} فرمان استخراج شد:**"]
    for i, link in enumerate(all_links):
        cmd = f"/H{hashlib.md5(link['href'].encode()).hexdigest()[:5]}"
        cmds[cmd] = link['href']
        line = f"{cmd} : {link['text'][:40]}"
        lines.append(line)

        # هر ۲۰ تا رو توی یه پیام بفرست
        if (i + 1) % 20 == 0 or i == len(all_links) - 1:
            send_message(chat_id, "\n".join(lines))
            lines = [f"📋 **ادامه فرامین ({i+1}/{len(all_links)}):**"]

    session.text_links = {**session.text_links, **cmds} if session.text_links else cmds
    set_session(session)
    job.status = "done"; update_job(job)

# ═══════════════════════ دانلود همه فایل‌های پیدا شده ═══════════════════════
def handle_download_all_found(job):
    chat_id = job.chat_id
    session = get_session(chat_id)
    all_results = session.found_downloads or []
    if not all_results:
        send_message(chat_id, "🚫 فایلی برای دانلود وجود ندارد.")
        job.status = "done"; update_job(job)
        return

    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)
    send_message(chat_id, f"📦 در حال دانلود {len(all_results)} فایل...")

    downloaded_files = []
    for i, f in enumerate(all_results):
        try:
            fname = get_filename_from_url(f['url'])
            fpath = os.path.join(job_dir, fname)
            # جلوگیری از بازنویسی فایل‌های هم‌نام
            counter = 1
            while os.path.exists(fpath):
                base, ext = os.path.splitext(fname)
                fpath = os.path.join(job_dir, f"{base}_{counter}{ext}")
                counter += 1
            with requests.get(f['url'], stream=True, timeout=60, headers={"User-Agent":"Mozilla/5.0"}) as r:
                r.raise_for_status()
                with open(fpath, "wb") as fh:
                    for chunk in r.iter_content(8192): fh.write(chunk)
            downloaded_files.append(fpath)
        except Exception as e:
            safe_print(f"download_all: failed {f['url']}: {e}")

    if not downloaded_files:
        send_message(chat_id, "❌ هیچ فایلی دانلود نشد.")
        job.status = "error"; update_job(job)
        shutil.rmtree(job_dir, ignore_errors=True)
        return

    # ساخت ZIP
    zp = os.path.join(job_dir, "all_files.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp in downloaded_files:
            zf.write(fp, os.path.basename(fp))

    parts = split_file_binary(zp, "all_files", ".zip") if os.path.getsize(zp) > ZIP_PART_SIZE else [zp]
    instr = os.path.join(job_dir, "merge.txt")
    with open(instr, "w") as f:
        f.write("همه‌ی فایل‌ها را دانلود کنید، سپس فایل .001 را با WinRAR یا 7-Zip باز کنید.")
    send_document(chat_id, instr, caption="📝 راهنما")
    for idx, p in enumerate(parts, 1):
        send_document(chat_id, p, caption=f"📦 پارت {idx}/{len(parts)}")
    job.status = "done"; update_job(job)
    shutil.rmtree(job_dir, ignore_errors=True)

# ═══════════════════════ پنل ادمین ═══════════════════════
def admin_panel(chat_id):
    try:
        mem = subprocess.run(['free', '-m'], stdout=subprocess.PIPE, text=True).stdout.strip()
        disk = subprocess.run(['df', '-h'], stdout=subprocess.PIPE, text=True).stdout.strip()
        uptime = subprocess.run(['uptime'], stdout=subprocess.PIPE, text=True).stdout.strip()
        sessions = load_sessions()
        active_users = len(sessions)
        msg = f"🛠️ **پنل ادمین**\n\n💾 **حافظه:**\n{mem}\n\n📀 **دیسک:**\n{disk}\n\n⏱️ **آپ‌تایم:**\n{uptime}\n\n👥 **کاربران فعال:** {active_users}"
        send_message(chat_id, msg)
    except Exception as e:
        send_message(chat_id, f"❌ خطا در دریافت اطلاعات: {e}")
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
        session = SessionState(chat_id=chat_id)
        session.is_pro = was_pro
        session.is_admin = was_admin
        session.state = "idle"
        session.click_counter = 0
        set_session(session)
        send_message(chat_id, "💀 تمام فعالیت‌ها متوقف و وضعیت به روز اول برگردانده شد.",
                     reply_markup=main_menu_keyboard(session.is_admin))
        return

    # ⏱️ دستور /status – نمایش وضعیت Job در حال اجرا
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
        # تخمین ساده: ۳۰ ثانیه برای اسکرین‌شات، ۶۰ برای دانلود، ۱۲۰ برای اسکن، ۱۸۰ برای ضبط
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
            send_message(chat_id, "منوی اصلی:", reply_markup=main_menu_keyboard(session.is_admin))
        else:
            send_message(chat_id, "👋 کد پرو را وارد کنید:")
        return

    if text == "/cancel":
        session.state = "idle"; session.cancel_requested = True; session.current_job_id = None
        session.click_counter = 0; set_session(session)
        close_user_context(chat_id)
        send_message(chat_id, "⏹️ لغو شد.", reply_markup=main_menu_keyboard(session.is_admin))
        return

    if not session.is_pro and not session.is_admin:
        if text in PRO_CODES:
            session.is_pro = True; set_session(session)
            send_message(chat_id, "✅ فعال!", reply_markup=main_menu_keyboard(session.is_admin))
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
                # عملیات Live – بعداً مدیریت می‌شود
                handle_live_command(chat_id, text, url)
            else:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="browser", url=url))
        return

    # 🎭 حالت انتظار Live Command
    if session.state == "waiting_live_command":
        if text.startswith("/Live_"):
            # استخراج شناسه دکمه
            parts = text[6:]  # بعد از /Live_
            need_scroll = parts.endswith("_Sk")
            if need_scroll:
                parts = parts[:-3]
            # پیدا کردن URL از text_links یا browser_links
            url = None
            if session.text_links and parts in session.text_links:
                url = session.text_links[parts]
            else:
                # جستجو در browser_links
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

    send_message(chat_id, "از منو استفاده کنید:", reply_markup=main_menu_keyboard(session.is_admin))

def handle_live_command(chat_id, text, url, need_scroll=False):
    """اجرای عملیات Live: کلیک روی دکمه و سپس ضبط"""
    session = get_session(chat_id)
    if not session.browser_url:
        send_message(chat_id, "❌ مرورگری برای اجرای Live باز نیست.")
        return

    ctx = get_or_create_context(chat_id)
    page = ctx.new_page()
    try:
        page.goto(session.browser_url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # پیدا کردن لینک و کلیک
        # تلاش می‌کنیم href مورد نظر را پیدا کنیم
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
            for _ in range(3):
                page.evaluate("window.scrollBy(0, 300)")
                page.wait_for_timeout(500)

        # شروع ضبط (ایجاد Job جدید)
        rec_time = session.settings.record_time
        job_id = str(uuid.uuid4())
        job = Job(job_id=job_id, chat_id=chat_id, mode="record_video", url=page.url)
        job.extra = {"live_page": page}  # page را برای ضبط ارسال می‌کنیم
        enqueue(job)
        send_message(chat_id, f"🎬 ضبط Live آغاز شد ({rec_time} ثانیه)...")
    except Exception as e:
        send_message(chat_id, f"❌ خطا در Live: {e}")
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
        send_message(chat_id, "✅ لغو شد.", reply_markup=main_menu_keyboard(session.is_admin))

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
        send_message(chat_id, "منوی اصلی:", reply_markup=main_menu_keyboard(session.is_admin))

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
        send_message(chat_id, "❌ لغو شد.", reply_markup=main_menu_keyboard(session.is_admin))

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
            send_message(chat_id, "🎭 حالت Live فعال است. لطفاً دستور Live را وارد کنید (مثال: /Live_d6h7s یا /Live_r4f3g_Sk):")
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
        send_message(chat_id, "📦 نتایج جستجو بسته شد.", reply_markup=main_menu_keyboard(session.is_admin))

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
        send_message(chat_id, "🧭 بسته شد.", reply_markup=main_menu_keyboard(session.is_admin))

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
    safe_print("✅ Bot15 Ultimate اجرا شد")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: stop_event.set()

if __name__ == "__main__":
    main()
