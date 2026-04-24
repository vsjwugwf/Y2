#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات بله – نسخهٔ ۱۱ (Ultimate)
مرورگر دو حالته با stealth، اسکن ویدیوی هوشمند، گوگل آزاد،
دانلودر هوشمند، اسکرین‌شات 4K، ضبط ویدیو، دانلود سایت، تنظیمات.
"""

import os, sys, json, time, math, queue, shutil, zipfile, uuid, re, hashlib
import subprocess, threading, traceback, random
from dataclasses import dataclass, asdict, field
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import urlparse, urljoin, unquote

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ═══════════════════════
# تنظیمات اصلی
# ═══════════════════════
BALE_BOT_TOKEN = os.getenv("BALE_BOT_TOKEN", "").strip()
if not BALE_BOT_TOKEN:
    print("ERROR: BALE_BOT_TOKEN not set", file=sys.stderr)
    sys.exit(1)

BALE_API_URL = "https://tapi.bale.ai/bot" + BALE_BOT_TOKEN
REQUEST_TIMEOUT = 30
LONG_POLL_TIMEOUT = 50
WORKER_COUNT = 1
ZIP_PART_SIZE = int(19 * 1024 * 1024)

PRO_CODES = ["PRO2024A", "PRO2024B", "PRO2024C", "PRO2024D", "PRO2024E"]

print_lock = threading.Lock()
queue_lock = threading.Lock()
workers_lock = threading.Lock()
callback_map: Dict[str, str] = {}
callback_map_lock = threading.Lock()
browser_contexts_lock = threading.Lock()

def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs, flush=True)

# ═══════════════════════
# مدل‌های داده
# ═══════════════════════
@dataclass
class UserSettings:
    record_time: int = 20
    auto_play_video: bool = True
    default_download_mode: str = "store"   # "store" یا "stream"
    browser_mode: str = "text"             # "text" یا "media"

@dataclass
class SessionState:
    chat_id: int
    state: str = "idle"
    is_pro: bool = False
    current_job_id: Optional[str] = None
    browser_url: Optional[str] = None
    last_interaction: float = time.time()
    cancel_requested: bool = False
    text_links: Optional[Dict[str, str]] = None
    browser_links: Optional[List[Dict[str, str]]] = None
    browser_page: int = 0
    settings: UserSettings = field(default_factory=UserSettings)
    # برای گوگل آزاد
    search_results: Optional[List[Dict[str, str]]] = None
    search_page: int = 0
    search_type: str = "all"  # "all", "videos", "images"
    search_query: Optional[str] = None

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

@dataclass
class WorkerInfo:
    worker_id: int
    current_job_id: Optional[str] = None
    status: str = "idle"

# ═══════════════════════
# ذخیره‌سازی محلی
# ═══════════════════════
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
            else:
                setattr(s, k, v)
        return s
    return SessionState(chat_id=chat_id)
def set_session(session):
    data = load_sessions()
    d = asdict(session)
    d["settings"] = asdict(session.settings)
    data[str(session.chat_id)] = d
    save_sessions(data)

# ═══════════════════════
# API بله
# ═══════════════════════
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

# ═══════════════════════
# منوها
# ═══════════════════════
def main_menu_keyboard():
    return {"inline_keyboard": [
        [{"text": "🧭 مرورگر من", "callback_data": "menu_browser"}],
        [{"text": "🔍 گوگل آزاد", "callback_data": "menu_google"}],
        [{"text": "📸 اسکرین‌شات", "callback_data": "menu_screenshot"}],
        [{"text": "📥 دانلود", "callback_data": "menu_download"}],
        [{"text": "⚙️ تنظیمات", "callback_data": "menu_settings"}],
        [{"text": "❌ لغو / ریست", "callback_data": "menu_cancel"}]
    ]}

def settings_keyboard(settings: UserSettings):
    rec = settings.record_time
    play = "فعال ✅" if settings.auto_play_video else "غیرفعال ❌"
    dlm  = "سریع ⚡" if settings.default_download_mode == "stream" else "عادی 💾"
    mode = "🎬 مدیا" if settings.browser_mode == "media" else "📄 متن"
    return {"inline_keyboard": [
        [{"text": "-۵", "callback_data": "rec_dec"},
         {"text": f"⏱️ ضبط: {rec}s", "callback_data": "rec_null"},
         {"text": "+۵", "callback_data": "rec_inc"}],
        [{"text": f"▶️ پخش: {play}", "callback_data": "set_play"}],
        [{"text": f"📥 دانلود: {dlm}", "callback_data": "set_dlmode"}],
        [{"text": f"🌐 حالت: {mode}", "callback_data": "set_brwmode"}],
        [{"text": "🔙 بازگشت", "callback_data": "back_main"}]
    ]}
# ═══════════════════════
# Playwright – سراسری (با stealth)
# ═══════════════════════
_global_playwright = None
_global_browser = None
browser_contexts = {}

try:
    from playwright_stealth import Stealth
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False
    safe_print("⚠️ playwright_stealth نصب نیست؛ stealth غیرفعال.")

# دامنه‌های تبلیغاتی برای فیلتر
AD_DOMAINS = [
    "doubleclick.net", "googleadsyndication.com", "adservice.google.com",
    "adsrvr.org", "outbrain.com", "taboola.com", "exoclick.com",
    "trafficfactory.biz", "propellerads.com", "adnxs.com", "criteo.com",
    "adsrvr.org", "moatads.com", "amazon-adsystem.com", "pubmatic.com",
    "openx.net", "rubiconproject.com", "sovrn.com", "indexww.com",
    "contextweb.com", "advertising.com", "zedo.com", "adzerk.net",
    "carbonads.com", "buysellads.com", "popads.net", "trafficstars.com",
    "trafficjunky.com", "eroadvertising.com", "juicyads.com", "plugrush.com",
    "txxx.com", "fuckbook.com", "traffic-force.com", "bongacams.com"
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
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-web-security",
                    "--disable-features=BlockInsecurePrivateNetworkRequests",
                ]
            )
        vw = random.choice([412, 390, 414, 428, 360])
        vh = random.choice([915, 844, 896, 926, 780])
        context = _global_browser.new_context(viewport={"width": vw, "height": vh})
        if HAS_STEALTH:
            page = context.new_page()
            try:
                Stealth().apply_stealth(page)
            except Exception as e:
                safe_print(f"stealth apply error: {e}")
            finally:
                page.close()
        browser_contexts[ctx_key] = {"context": context, "last_used": time.time()}
        return context

def close_user_context(chat_id):
    ctx_key = str(chat_id)
    with browser_contexts_lock:
        ctx = browser_contexts.pop(ctx_key, None)
    if ctx:
        try: ctx["context"].close()
        except: pass

# ═══════════════════════
# استخراج المان‌ها – دو حالته + اسکن ویدیوی هوشمند
# ═══════════════════════
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
    """
    اسکن هوشمند ویدیوها – مخصوص دکمهٔ "اسکن ویدیو"
    برمی‌گرداند: لیستی از دیکشنری‌های {"text": ..., "href": ..., "score": ...}
    """
    raw = page.evaluate("""() => {
        const results = [];
        const centerX = window.innerWidth / 2;
        const centerY = window.innerHeight / 2;
        
        // تگ‌های video
        document.querySelectorAll('video').forEach(v => {
            const rect = v.getBoundingClientRect();
            if (rect.width < 200 || rect.height < 150) return; // کوچک = تبلیغ
            const src = v.src || (v.querySelector('source') ? v.querySelector('source').src : '');
            if (!src) return;
            const area = rect.width * rect.height;
            const dist = Math.sqrt(Math.pow(rect.x + rect.width/2 - centerX, 2) + 
                                   Math.pow(rect.y + rect.height/2 - centerY, 2));
            const score = area - (dist * 2);
            results.push({text: 'video element', href: src, score: score, 
                          w: rect.width, h: rect.height, dist: dist});
        });
        
        // iframe
        document.querySelectorAll('iframe').forEach(f => {
            const rect = f.getBoundingClientRect();
            if (rect.width < 300 || rect.height < 200) return;
            const src = f.src || '';
            if (!src || !src.startsWith('http')) return;
            const area = rect.width * rect.height;
            const dist = Math.sqrt(Math.pow(rect.x + rect.width/2 - centerX, 2) + 
                                   Math.pow(rect.y + rect.height/2 - centerY, 2));
            const score = area - (dist * 2);
            results.push({text: 'iframe', href: src, score: score,
                          w: rect.width, h: rect.height, dist: dist});
        });
        
        return results.sort((a, b) => b.score - a.score);
    }""")
    
    # فیلتر دامنه‌های تبلیغاتی + حذف موارد بی‌ربط
    filtered = []
    for item in raw:
        href = item.get("href", "")
        if not href.startswith("http"):
            continue
        # چک دامنه‌های تبلیغاتی
        parsed = urlparse(href)
        if any(ad in parsed.netloc for ad in AD_DOMAINS):
            continue
        # چک کلمات کلیدی تبلیغاتی در URL
        if any(kw in href.lower() for kw in ["/ad/", "/ads/", "/banner/", "/popup/", "/track/", "/pixel/"]):
            continue
        # تخمین یک اسم برای ویدیو
        text = item.get("text", "ویدیو")[:30]
        if parsed.netloc:
            text = f"{text} ({parsed.netloc})"
        filtered.append({
            "text": text[:35],
            "href": href,
            "score": item.get("score", 0)
        })
    return filtered

# ═══════════════════════
# ابزارهای فایل (اصلاح‌شده)
# ═══════════════════════
def is_direct_file_url(url: str) -> bool:
    """تشخیص فایل با پشتیبانی از هر پسوند"""
    known_extensions = [
        '.zip','.rar','.7z','.pdf','.mp4','.mkv','.avi','.mp3',
        '.exe','.apk','.dmg','.iso','.tar','.gz','.bz2','.xz','.whl',
        '.deb','.rpm','.msi','.pkg','.appimage','.flatpakref','.jar','.war',
        '.py','.sh','.bat','.run','.bin','.dmg','.img','.mov','.flv','.wmv',
        '.webm','.ogg','.wav','.flac','.tiff','.bmp','.csv','.docx','.pptx'
    ]
    path = urlparse(url).path.lower()
    # پسوندهای شناخته‌شده
    if any(path.endswith(ext) for ext in known_extensions):
        return True
    # هر فایلی که پسوند معتبر داشته باشد
    filename = path.split('/')[-1]
    if '.' in filename:
        ext = filename.rsplit('.', 1)[-1]
        if ext and re.match(r'^[a-zA-Z0-9_-]+$', ext) and len(ext) <= 10:
            return True
    return False

def get_filename_from_url(url):
    path = unquote(urlparse(url).path)
    name = os.path.basename(path)
    return name if name and '.' in name else "downloaded_file"

def crawl_for_download_link(start_url):
    visited = set()
    q = queue.Queue()
    q.put((start_url, 0))
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    pc = 0
    while not q.empty():
        cur, depth = q.get()
        if cur in visited or depth > 1 or pc > 10: break
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
                q.put((href, depth+1))
    return None

def split_file_binary(file_path, prefix, ext):
    d = os.path.dirname(file_path) or "."
    parts = []
    if not os.path.exists(file_path):
        safe_print(f"split_file_binary: file not found {file_path}")
        return []
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
    if not os.path.exists(src):
        safe_print(f"create_zip_and_split: source not found {src}")
        return []
    zp = os.path.join(d, f"{base}.zip")
    try:
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(src, os.path.basename(src))
    except Exception as e:
        safe_print(f"zip creation error: {e}")
        return []
    if os.path.getsize(zp) <= ZIP_PART_SIZE:
        return [zp]
    parts = split_file_binary(zp, base, ".zip")
    os.remove(zp)
    return parts

# ═══════════════════════
# اسکرین‌شات
# ═══════════════════════
def screenshot_full(context, url, out):
    page = context.new_page()
    try:
        page.goto(url, timeout=90000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        page.screenshot(path=out, full_page=True)
    finally:
        page.close()

def screenshot_4k(context, url, out):
    page = context.new_page()
    try:
        page.set_viewport_size({"width": 3840, "height": 2160})
        page.goto(url, timeout=90000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        page.screenshot(path=out, full_page=True)
    finally:
        page.close()
# ═══════════════════════
# دانلود کامل وب‌سایت (wget با هویت + fallback Playwright)
# ═══════════════════════
def download_full_website(job):
    chat_id = job.chat_id
    url = job.url
    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)
    send_message(chat_id, "🌐 دانلود کامل وب‌سایت...")

    # اول تلاش با wget (با هویت مرورگر)
    if shutil.which("wget"):
        try:
            user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            cmd = [
                "wget", "--adjust-extension", "--span-hosts", "--convert-links",
                "--page-requisites", "--no-directories", "--directory-prefix", job_dir,
                "--recursive", "--level=1", "--accept", "html,css,js,jpg,jpeg,png,gif,svg,mp4,webm,pdf",
                "--user-agent", user_agent,
                "--header", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "--header", "Accept-Language: en-US,en;q=0.5",
                "--header", "Referer: https://www.google.com/",
                "--header", "DNT: 1",
                "--keep-session-cookies", "--max-redirect", "5",
                "--timeout", "30", "--tries", "3",
                url
            ]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
            if result.returncode == 0:
                _finish_website_download(job, job_dir)
                return
            else:
                safe_print(f"wget failed with code {result.returncode}, trying Playwright fallback...")
        except Exception as e:
            safe_print(f"wget exception: {e}, trying Playwright...")

    # Fallback: Playwright Downloader
    send_message(chat_id, "🔄 دانلود با مرورگر مخفی (Playwright)...")
    try:
        context = get_or_create_context(chat_id)
        page = context.new_page()
        try:
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            # ذخیره HTML
            html_content = page.content()
            html_path = os.path.join(job_dir, "index.html")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            # ذخیره اسکرین‌شات
            spath = os.path.join(job_dir, "screenshot.png")
            page.screenshot(path=spath, full_page=True)
        finally:
            page.close()

        _finish_website_download(job, job_dir)
    except Exception as e:
        send_message(chat_id, f"❌ خطا در دانلود سایت: {e}")
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
    zip_path = os.path.join(job_dir, "website.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath in all_files:
            zf.write(fpath, os.path.relpath(fpath, job_dir))
    parts = split_file_binary(zip_path, "website", ".zip") if os.path.getsize(zip_path) > ZIP_PART_SIZE else [zip_path]
    instr = os.path.join(job_dir, "merge.txt")
    with open(instr, "w") as f:
        f.write("برای ادغام فایل‌های zip.001 و ... از WinRAR یا 7-Zip استفاده کنید.")
    send_document(chat_id, instr, caption="📝 راهنما")
    for idx, p in enumerate(parts, 1):
        send_document(chat_id, p, caption=f"🌐 پارت {idx}/{len(parts)}")
    job.status = "done"; update_job(job)
    shutil.rmtree(job_dir, ignore_errors=True)

# ═══════════════════════
# صف و Worker
# ═══════════════════════
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
            if not job:
                time.sleep(2); continue
            set_worker_busy(worker_id, job.job_id)
            try:
                process_job(worker_id, job)
            except Exception as e:
                safe_print(f"Worker error: {e}")
                traceback.print_exc()
            finally:
                set_worker_idle(worker_id)
        else:
            time.sleep(2)

# ═══════════════════════
# پردازش Job
# ═══════════════════════
def process_job(worker_id, job):
    chat_id = job.chat_id
    session = get_session(chat_id)

    if job.mode == "download_execute":
        job_dir = os.path.join("jobs_data", job.job_id)
        os.makedirs(job_dir, exist_ok=True)
        try:
            execute_download(job, job_dir)
        except Exception as e:
            send_message(chat_id, f"❌ خطا: {e}")
            job.status = "error"; update_job(job)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)
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

    if job.mode == "youtube_download":
        handle_youtube_download(job)
        return

    if job.mode == "google_search":
        handle_google_search(job)
        return

    if job.mode == "scan_videos":
        handle_scan_videos(job)
        return

    session.current_job_id = job.job_id
    set_session(session)

    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        if session.cancel_requested:
            raise InterruptedError("cancel")

        if job.mode == "screenshot":
            send_message(chat_id, f"📸 اسکرین‌شات...")
            ctx = get_or_create_context(chat_id)
            spath = os.path.join(job_dir, "screenshot.png")
            screenshot_full(ctx, job.url, spath)
            send_document(chat_id, spath, caption="✅ اسکرین‌شات")
            kb = {"inline_keyboard": [[{"text": "🖼️ 4K", "callback_data": f"req4k_{job.job_id}"}]]}
            send_message(chat_id, "کیفیت بالاتر:", reply_markup=kb)
            job.status = "done"; update_job(job)

        elif job.mode == "4k_screenshot":
            send_message(chat_id, "🔍 4K...")
            ctx = get_or_create_context(chat_id)
            spath = os.path.join(job_dir, "screenshot_4k.png")
            screenshot_4k(ctx, job.url, spath)
            send_document(chat_id, spath, caption="✅ 4K")
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
            if s.state != "browsing" and s.state != "searching":
                s.state = "idle"; s.current_job_id = None; s.cancel_requested = False
                set_session(s)
                send_message(chat_id, "🔄 آماده.", reply_markup=main_menu_keyboard())

# ═══════════════════════
# دانلود هوشمند (اصلاحی)
# ═══════════════════════
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
            job.mode = "blind_download"
            job.url = url
            update_job(job)
            handle_blind_download(job)
            return

    try:
        head = requests.head(direct_link, timeout=10, allow_redirects=True)
        size = head.headers.get("Content-Length")
        size_str = f"{int(size)/1024/1024:.2f} MB" if size else "نامشخص"
    except:
        size_str = "نامشخص"
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
    buf = b""
    idx = 1
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent":"Mozilla/5.0"}) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=8192):
            buf += chunk
            while len(buf) >= ZIP_PART_SIZE:
                part = buf[:ZIP_PART_SIZE]
                buf = buf[ZIP_PART_SIZE:]
                pname = f"{base}.part{idx:03d}{ext}"
                ppath = os.path.join(job_dir, pname)
                with open(ppath, "wb") as f: f.write(part)
                send_document(chat_id, ppath, caption=f"⚡ پارت {idx}")
                os.remove(ppath)
                idx += 1
        if buf:
            pname = f"{base}.part{idx:03d}{ext}"
            ppath = os.path.join(job_dir, pname)
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
            r.raise_for_status()
            with open(fpath, "wb") as f:
                for c in r.iter_content(8192): f.write(c)

    if not os.path.exists(fpath):
        send_message(chat_id, "❌ فایل دانلود شده یافت نشد.")
        job.status = "error"; update_job(job)
        return

    if pack_zip:
        parts = create_zip_and_split(fpath, fname); label = "ZIP"
    else:
        base, ext = os.path.splitext(fname)
        parts = split_file_binary(fpath, base, ext); label = "اصلی"

    if not parts:
        send_message(chat_id, "❌ خطا در تقسیم فایل.")
        job.status = "error"; update_job(job)
        return

    instr = os.path.join(job_dir, "merge.txt")
    with open(instr, "w") as f:
        if pack_zip:
            f.write("همه‌ی فایل‌ها را دانلود کنید، سپس فایل .001 را با WinRAR یا 7-Zip باز کنید.")
        else:
            f.write(f"برای ادغام در ویندوز: copy /b {'+'.join([os.path.basename(p) for p in parts])} {fname}")
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
            r.raise_for_status()
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
            send_message(chat_id, "❌ فایل دانلود نشد.")
            job.status = "error"; update_job(job)
            shutil.rmtree(job_dir, ignore_errors=True)
            return
        size_str = f"{os.path.getsize(fpath)/1024/1024:.2f} MB"
        text = f"📄 فایل (کور): {fname} ({size_str})\nانتخاب:"
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

# ═══════════════════════
# یوتیوب (yt-dlp)
# ═══════════════════════
def handle_youtube_download(job):
    chat_id = job.chat_id
    url = job.url
    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)
    send_message(chat_id, "📥 دریافت ویدیوی یوتیوب...")
    try:
        outtmpl = os.path.join(job_dir, "%(title)s-%(id)s.%(ext)s")
        cmd = ["yt-dlp", "--no-playlist",
               "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
               "--merge-output-format", "mkv", "-o", outtmpl, url]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        video = None
        for f in os.listdir(job_dir):
            if f.endswith(('.mkv','.mp4','.webm')):
                video = os.path.join(job_dir, f); break
        if video:
            if os.path.getsize(video) > ZIP_PART_SIZE:
                parts = split_file_binary(video, "youtube", os.path.splitext(video)[1])
                for idx, p in enumerate(parts, 1):
                    send_document(chat_id, p, caption=f"🎥 پارت {idx}/{len(parts)}")
            else:
                send_document(chat_id, video, caption="🎥 ویدیو")
        else:
            send_message(chat_id, "❌ یافت نشد.")
        job.status = "done"; update_job(job)
    except Exception as e:
        send_message(chat_id, f"❌ خطا: {e}")
        job.status = "error"; update_job(job)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)

# ═══════════════════════
# ضبط ویدیو (اصلاح‌شده)
# ═══════════════════════
def handle_record_video(job):
    chat_id = job.chat_id
    session = get_session(chat_id)
    url = job.url
    rec_time = session.settings.record_time
    auto_play = session.settings.auto_play_video
    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)
    send_message(chat_id, f"🎬 ضبط {rec_time} ثانیه...")

    try:
        context = _global_browser.new_context(
            viewport={"width": 1280, "height": 720},
            record_video_dir=job_dir,
            record_video_size={"width": 1280, "height": 720}
        )
        page = context.new_page()
        try:
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            if auto_play:
                try:
                    page.evaluate("() => { const v = document.querySelector('video'); if (v) v.play(); }")
                except: pass
                try:
                    page.click('button[aria-label="Play"], .vjs-big-play-button, .ytp-large-play-button', timeout=3000)
                except: pass
                time.sleep(1)
                try:
                    page.evaluate("() => { const v = document.querySelector('video'); if (v) v.play(); }")
                except: pass
            page.wait_for_timeout(rec_time * 1000)
        finally:
            page.close()
            context.close()

        time.sleep(1)
        webm = None
        for f in os.listdir(job_dir):
            if f.endswith('.webm'):
                webm = os.path.join(job_dir, f); break
        if not webm:
            send_message(chat_id, "❌ ویدیویی ضبط نشد.")
            job.status = "error"; update_job(job)
            return

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
        send_message(chat_id, f"❌ خطا در ضبط: {e}")
        job.status = "error"; update_job(job)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
# ═══════════════════════
# مرورگر (با دکمهٔ اسکن ویدیو)
# ═══════════════════════
def handle_browser(job, job_dir):
    chat_id = job.chat_id
    session = get_session(chat_id)
    mode = session.settings.browser_mode
    ctx = get_or_create_context(chat_id)
    page = ctx.new_page()
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
    if page_num > 0:
        nav.append({"text": "◀️", "callback_data": f"bpg_{chat_id}_{page_num-1}"})
    if end < len(all_links):
        nav.append({"text": "▶️", "callback_data": f"bpg_{chat_id}_{page_num+1}"})
    if nav: keyboard_rows.append(nav)

    is_youtube = ("youtube.com" in url and "/watch" in url) or "youtu.be" in url
    if is_youtube:
        keyboard_rows.append([{"text": "🎥 دانلود یوتیوب", "callback_data": f"ytdl_{chat_id}"}])

    # دکمهٔ اسکن ویدیو فقط در مدیا مود
    if session.settings.browser_mode == "media":
        keyboard_rows.append([{"text": "🎬 اسکن ویدیوها", "callback_data": f"scvid_{chat_id}"}])

    keyboard_rows.append([{"text": "🎬 ضبط", "callback_data": f"recvid_{chat_id}"}])
    keyboard_rows.append([{"text": "🌐 دانلود سایت", "callback_data": f"dlweb_{chat_id}"}])
    keyboard_rows.append([{"text": "❌ بستن", "callback_data": f"closebrowser_{chat_id}"}])

    kb = {"inline_keyboard": keyboard_rows}
    if image_path:
        send_document(chat_id, image_path, caption=f"🌐 {url} (صفحه {page_num+1})")
    else:
        send_message(chat_id, f"🌐 {url} (صفحه {page_num+1})")
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

# ═══════════════════════
# اسکن ویدیوی هوشمند (خروجی با دستور /o)
# ═══════════════════════
def handle_scan_videos(job):
    chat_id = job.chat_id
    session = get_session(chat_id)
    ctx = get_or_create_context(chat_id)
    page = ctx.new_page()
    try:
        page.goto(session.browser_url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        videos = scan_videos_smart(page)

        if not videos:
            send_message(chat_id, "🚫 هیچ ویدیویی در این صفحه یافت نشد.")
            job.status = "done"; update_job(job)
            return

        lines = [f"🎬 **{len(videos)} ویدیو یافت شد:**"]
        cmds = {}
        for i, vid in enumerate(videos[:15]):
            cmd = f"/o{hashlib.md5(vid['href'].encode()).hexdigest()[:5]}"
            cmds[cmd] = vid['href']
            lines.append(f"{i+1}. {vid['text']}")
            lines.append(f"   📥 {cmd}")
            lines.append("")

        send_message(chat_id, "\n".join(lines))
        session.text_links = {**session.text_links, **cmds} if session.text_links else cmds
        set_session(session)
        job.status = "done"; update_job(job)
    except Exception as e:
        send_message(chat_id, f"❌ خطا در اسکن: {e}")
        job.status = "error"; update_job(job)
    finally:
        page.close()

# ═══════════════════════
# گوگل آزاد
# ═══════════════════════
def handle_google_search(job):
    chat_id = job.chat_id
    session = get_session(chat_id)
    query = job.extra.get("query", job.url) if job.extra else job.url
    search_type = job.extra.get("type", "all") if job.extra else "all"

    # ساخت URL جستجو
    if search_type == "videos":
        url = f"https://www.google.com/search?q={requests.utils.quote(query)}&tbm=vid&safe=off&filter=0"
    elif search_type == "images":
        url = f"https://www.google.com/search?q={requests.utils.quote(query)}&tbm=isch&safe=off&filter=0"
    else:
        url = f"https://www.google.com/search?q={requests.utils.quote(query)}&safe=off&filter=0"

    ctx = get_or_create_context(chat_id)
    page = ctx.new_page()
    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        send_message(chat_id, f"🔍 جستجوی گوگل: {query}")
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # اسکرین‌شات
        spath = os.path.join(job_dir, "google.png")
        page.screenshot(path=spath, full_page=True)
        send_document(chat_id, spath, caption=f"🔍 نتایج: {query}")

        # استخراج نتایج
        results = page.evaluate("""() => {
            const items = [];
            document.querySelectorAll('#search .g').forEach(g => {
                const h3 = g.querySelector('h3');
                const a = g.querySelector('a[href]');
                const desc = g.querySelector('.VwiC3b, span.st, .aCOpRe');
                if (h3 && a && a.href.startsWith('http')) {
                    items.push({
                        title: h3.textContent.trim().substring(0, 50),
                        href: a.href,
                        desc: desc ? desc.textContent.trim().substring(0, 70) : ''
                    });
                }
            });
            return items.slice(0, 15);
        }""")

        if not results:
            send_message(chat_id, "🚫 نتیجه‌ای یافت نشد یا گوگل مسدود کرد.")
            job.status = "done"; update_job(job)
            return

        # ذخیره در session
        session.search_results = results
        session.search_query = query
        session.search_type = search_type
        session.search_page = 0
        session.state = "searching"
        set_session(session)

        send_search_page(chat_id, 0)
        job.status = "done"; update_job(job)
    except Exception as e:
        send_message(chat_id, f"❌ خطا در جستجو: {e}")
        job.status = "error"; update_job(job)
    finally:
        page.close()
        shutil.rmtree(job_dir, ignore_errors=True)

def send_search_page(chat_id, page_num=0):
    session = get_session(chat_id)
    results = session.search_results or []
    per_page = 5
    start = page_num * per_page
    end = min(start + per_page, len(results))
    page_results = results[start:end]

    keyboard_rows = []
    for i, r in enumerate(page_results):
        title = r.get("title", "نتیجه")[:30]
        cb = f"sres_{chat_id}_{start+i}"
        with callback_map_lock: callback_map[cb] = r.get("href", "")
        keyboard_rows.append([{"text": f"🔗 {title}", "callback_data": cb}])
        desc = r.get("desc", "")[:50]
        if desc:
            keyboard_rows.append([{"text": f"   {desc}", "callback_data": "none"}])

    nav = []
    if page_num > 0:
        nav.append({"text": "◀️ قبلی", "callback_data": f"spg_{chat_id}_{page_num-1}"})
    if end < len(results):
        nav.append({"text": "بعدی ▶️", "callback_data": f"spg_{chat_id}_{page_num+1}"})
    if nav: keyboard_rows.append(nav)

    # سوئیچ بین دسته‌ها
    type_row = []
    if session.search_type != "all":
        type_row.append({"text": "📄 همه", "callback_data": f"stype_{chat_id}_all"})
    if session.search_type != "videos":
        type_row.append({"text": "🎬 ویدیوها", "callback_data": f"stype_{chat_id}_videos"})
    if session.search_type != "images":
        type_row.append({"text": "🖼️ عکس‌ها", "callback_data": f"stype_{chat_id}_images"})
    if type_row: keyboard_rows.append(type_row)

    keyboard_rows.append([{"text": "🔙 خروج", "callback_data": f"close_search_{chat_id}"}])

    kb = {"inline_keyboard": keyboard_rows}
    send_message(chat_id, f"نتایج جستجو: **{session.search_query}** (صفحه {page_num+1})", reply_markup=kb)

# ═══════════════════════
# تنظیمات
# ═══════════════════════
def settings_callback(data, chat_id, session):
    if data == "rec_dec":
        session.settings.record_time = max(5, session.settings.record_time - 5)
    elif data == "rec_inc":
        session.settings.record_time = min(120, session.settings.record_time + 5)
    elif data == "set_play":
        session.settings.auto_play_video = not session.settings.auto_play_video
    elif data == "set_dlmode":
        session.settings.default_download_mode = "stream" if session.settings.default_download_mode == "store" else "store"
    elif data == "set_brwmode":
        session.settings.browser_mode = "media" if session.settings.browser_mode == "text" else "text"
    set_session(session)
    if data != "back_main":
        send_message(chat_id, "تنظیمات:", reply_markup=settings_keyboard(session.settings))
    else:
        send_message(chat_id, "منوی اصلی:", reply_markup=main_menu_keyboard())

# ═══════════════════════
# مدیریت پیام و Callback
# ═══════════════════════
def handle_message(chat_id, text):
    session = get_session(chat_id)
    text = text.strip()

    if text == "/start":
        session.state = "idle"; set_session(session)
        if not session.is_pro:
            send_message(chat_id, "👋 کد پرو را وارد کنید:")
        else:
            send_message(chat_id, "منوی اصلی:", reply_markup=main_menu_keyboard())
        return

    if text == "/cancel":
        session.state = "idle"; session.cancel_requested = True
        session.current_job_id = None; set_session(session)
        close_user_context(chat_id)
        send_message(chat_id, "⏹️ لغو شد.", reply_markup=main_menu_keyboard())
        return

    if not session.is_pro:
        if text in PRO_CODES:
            session.is_pro = True; set_session(session)
            send_message(chat_id, "✅ فعال!", reply_markup=main_menu_keyboard())
        else:
            send_message(chat_id, "⛔ کد نامعتبر")
        return

    # حالت browsing: قبول دستورهای /a و /o
    if session.state == "browsing":
        if session.text_links and text in session.text_links:
            url = session.text_links.pop(text)
            set_session(session)
            if text.startswith("/o"):
                # دانلود ویدیو
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download", url=url))
            else:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="browser", url=url))
            return
        return

    # حالت searching: قبول دستورهای جستجو
    if session.state == "searching":
        return  # فقط callbackها در این حالت کار می‌کنند

    # حالت‌های انتظار URL
    if session.state.startswith("waiting_url_"):
        url = text
        if not (url.startswith("http://") or url.startswith("https://")):
            send_message(chat_id, "❌ URL نامعتبر"); return
        mode_map = {
            "waiting_url_screenshot": "screenshot",
            "waiting_url_download": "download",
            "waiting_url_browser": "browser",
            "waiting_url_google": "google_search"
        }
        mode = mode_map.get(session.state, "screenshot")
        extra = None
        if mode == "google_search":
            extra = {"query": url, "type": "all"}
        job = Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode=mode, url=url, extra=extra)
        enqueue(job)
        session.state = "idle"; session.current_job_id = job.job_id
        set_session(session)
        pos = job_queue_position(job.job_id)
        send_message(chat_id, f"✅ در صف (نوبت {pos})" if pos != 1 else "✅ در صف قرار گرفت.")
        return

    send_message(chat_id, "از منو استفاده کنید:", reply_markup=main_menu_keyboard())

def handle_callback(cq):
    cid = cq["id"]; msg = cq.get("message"); data = cq.get("data", "")
    if not msg: return answer_callback_query(cid)
    chat_id = msg["chat"]["id"]
    session = get_session(chat_id)

    # منوی اصلی
    if data == "menu_screenshot":
        session.state = "waiting_url_screenshot"; set_session(session)
        send_message(chat_id, "📸 URL:")
    elif data == "menu_download":
        session.state = "waiting_url_download"; set_session(session)
        send_message(chat_id, "📥 URL:")
    elif data == "menu_browser":
        session.state = "waiting_url_browser"; set_session(session)
        send_message(chat_id, "🧭 URL:")
    elif data == "menu_google":
        session.state = "waiting_url_google"; set_session(session)
        send_message(chat_id, "🔍 عبارت جستجو را بفرستید:")
    elif data == "menu_settings":
        send_message(chat_id, "⚙️", reply_markup=settings_keyboard(session.settings))
    elif data == "menu_cancel":
        session.state = "idle"; session.cancel_requested = True
        session.current_job_id = None; set_session(session)
        close_user_context(chat_id)
        send_message(chat_id, "✅ لغو شد.", reply_markup=main_menu_keyboard())
    # تنظیمات
    elif data in ("rec_dec", "rec_inc", "set_play", "set_dlmode", "set_brwmode", "back_main"):
        settings_callback(data, chat_id, session)
    # اسکرین‌شات 4K
    elif data.startswith("req4k_"):
        jid = data[6:]
        job = find_job(jid)
        if job and job.status == "done":
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="4k_screenshot", url=job.url))
    # دانلود ZIP/اصلی
    elif data.startswith("dlzip_") or data.startswith("dlraw_"):
        jid = data[6:] if data.startswith("dlzip_") else data[6:]
        job = find_job(jid)
        if job and job.extra:
            job.extra["pack_zip"] = data.startswith("dlzip_")
            job.status = "done"; update_job(job)
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download_execute",
                        url=job.url, extra=job.extra))
    # دانلود کور ZIP/اصلی
    elif data.startswith("dlblindzip_") or data.startswith("dlblindra_"):
        jid = data[11:] if data.startswith("dlblindzip_") else data[11:]
        job = find_job(jid)
        if job and job.extra and "file_path" in job.extra:
            job.extra["pack_zip"] = data.startswith("dlblindzip_")
            job.status = "done"; update_job(job)
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download_execute",
                        url=job.url, extra=job.extra))
    # لغو
    elif data.startswith("canceljob_"):
        jid = data[10:]
        job = find_job(jid)
        if job: job.status = "cancelled"; update_job(job)
        send_message(chat_id, "❌ لغو شد.", reply_markup=main_menu_keyboard())
    # ناوبری مرورگر
    elif data.startswith("nav_"):
        parts = data.split("_", 2)
        if len(parts) >= 3:
            cb = f"{parts[0]}_{parts[1]}_{parts[2]}"
            with callback_map_lock: url = callback_map.pop(cb, None)
            if url:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="browser", url=url))
    # دانلود ویدیو از مرورگر
    elif data.startswith("dlvid_"):
        parts = data.split("_", 2)
        if len(parts) >= 3:
            cb = f"{parts[0]}_{parts[1]}_{parts[2]}"
            with callback_map_lock: url = callback_map.pop(cb, None)
            if url:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download", url=url))
    # اسکن ویدیو
    elif data.startswith("scvid_"):
        enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="scan_videos", url=""))
    # ضبط ویدیو
    elif data.startswith("recvid_"):
        url = session.browser_url
        if url:
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="record_video", url=url))
    # یوتیوب
    elif data.startswith("ytdl_"):
        url = session.browser_url
        if url:
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="youtube_download", url=url))
    # دانلود سایت
    elif data.startswith("dlweb_"):
        url = session.browser_url
        if url:
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download_website", url=url))
    # صفحه‌بندی مرورگر
    elif data.startswith("bpg_"):
        parts = data.split("_")
        if len(parts) == 3:
            page = int(parts[2])
            session.browser_page = page; set_session(session)
            send_browser_page(chat_id, page_num=page)
    # صفحه‌بندی نتایج جستجو
    elif data.startswith("spg_"):
        parts = data.split("_")
        if len(parts) == 3:
            page = int(parts[2])
            session.search_page = page; set_session(session)
            send_search_page(chat_id, page)
    # کلیک روی نتیجهٔ جستجو
    elif data.startswith("sres_"):
        parts = data.split("_", 2)
        if len(parts) >= 3:
            cb = f"{parts[0]}_{parts[1]}_{parts[2]}"
            with callback_map_lock: url = callback_map.pop(cb, None)
            if url:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="browser", url=url))
    # تغییر نوع جستجو
    elif data.startswith("stype_"):
        parts = data.split("_")
        if len(parts) == 3:
            stype = parts[2]
            query = session.search_query
            if query:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="google_search",
                            url=query, extra={"query": query, "type": stype}))
    # بستن جستجو
    elif data.startswith("close_search_"):
        session.state = "idle"; set_session(session)
        send_message(chat_id, "🔍 جستجو بسته شد.", reply_markup=main_menu_keyboard())
    # بستن مرورگر
    elif data.startswith("closebrowser_"):
        close_user_context(chat_id)
        session.state = "idle"; set_session(session)
        send_message(chat_id, "🧭 بسته شد.", reply_markup=main_menu_keyboard())
    else:
        answer_callback_query(cid)

# ═══════════════════════
# Polling و Main
# ═══════════════════════
def polling_loop(stop_event):
    offset = None
    safe_print("[Polling] start")
    while not stop_event.is_set():
        try:
            updates = get_updates(offset, LONG_POLL_TIMEOUT)
        except Exception as e:
            safe_print(f"Poll error: {e}"); time.sleep(5); continue
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
    safe_print("✅ Bot11 اجرا شد")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        stop_event.set()

if __name__ == "__main__":
    main()
    
