#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات بله – مرورگر تعاملی، دانلودر هوشمند، اسکرین‌شات 4K، اسکن فایل‌های بزرگ
+ حالت دانلود کور + رفع تمام باگ‌ها
"""

import os, sys, json, time, math, queue, shutil, zipfile, uuid, re, hashlib
import threading, traceback
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import urlparse, urljoin, unquote

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ═══════════════════════════════════════
# تنظیمات
# ═══════════════════════════════════════
BALE_BOT_TOKEN = os.getenv("BALE_BOT_TOKEN", "").strip()
if not BALE_BOT_TOKEN:
    print("ERROR: BALE_BOT_TOKEN not set", file=sys.stderr)
    sys.exit(1)

BALE_API_URL = "https://tapi.bale.ai/bot" + BALE_BOT_TOKEN
REQUEST_TIMEOUT = 30
LONG_POLL_TIMEOUT = 50
WORKER_COUNT = 1
ZIP_PART_SIZE = int(19.5 * 1024 * 1024)

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

# ═══════════════════════════════════════
# مدل‌های داده
# ═══════════════════════════════════════
@dataclass
class SessionState:
    chat_id: int
    state: str = "idle"
    is_pro: bool = False
    current_job_id: Optional[str] = None
    browser_url: Optional[str] = None
    last_interaction: float = time.time()
    cancel_requested: bool = False
    text_links: Optional[Dict[str, str]] = None   # command -> url
    browser_links: Optional[List[Dict[str, str]]] = None  # برای اسکن فایل‌های بزرگ

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

# ═══════════════════════════════════════
# ذخیره‌سازی محلی
# ═══════════════════════════════════════
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
    if key in data: return SessionState(**data[key])
    return SessionState(chat_id=chat_id)
def set_session(session):
    data = load_sessions()
    data[str(session.chat_id)] = asdict(session)
    save_sessions(data)

# ═══════════════════════════════════════
# API بله
# ═══════════════════════════════════════
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
    params = {"callback_query_id": cq_id, "text": text, "show_alert": show_alert}
    return bale_request("answerCallbackQuery", params=params)

def get_updates(offset=None, timeout=LONG_POLL_TIMEOUT):
    params = {"timeout": timeout}
    if offset: params["offset"] = offset
    return bale_request("getUpdates", params=params) or []

# ═══════════════════════════════════════
# منوها
# ═══════════════════════════════════════
def main_menu_keyboard():
    return {"inline_keyboard": [
        [{"text": "🧭 مرورگر من", "callback_data": "menu_browser"}],
        [{"text": "📸 اسکرین‌شات از سایت", "callback_data": "menu_screenshot"}],
        [{"text": "📥 دانلود محتوای سایت", "callback_data": "menu_download"}],
        [{"text": "❌ لغو / تنظیم مجدد", "callback_data": "menu_cancel"}]
    ]}

# ═══════════════════════════════════════
# Playwright – global
# ═══════════════════════════════════════
_global_playwright = None
_global_browser = None
browser_contexts = {}

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
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
        context = _global_browser.new_context(viewport={"width": 1280, "height": 720})
        browser_contexts[ctx_key] = {"context": context, "last_used": time.time()}
        return context

def close_user_context(chat_id):
    ctx_key = str(chat_id)
    with browser_contexts_lock:
        ctx = browser_contexts.pop(ctx_key, None)
    if ctx:
        try: ctx["context"].close()
        except: pass

# ═══════════════════════════════════════
# استخراج المان‌های صفحه + ویدیو با Network Monitoring
# ═══════════════════════════════════════
def extract_clickable_and_media(page):
    """برمی‌گرداند: (links, video_urls)"""
    # استخراج لینک‌های معمولی
    raw = page.evaluate("""() => {
        const items = [];
        const seen = new Set();
        function add(type, text, href) {
            if (!href || seen.has(href)) return;
            seen.add(href);
            items.push([type, text.trim().substring(0, 35), href]);
        }
        document.querySelectorAll('a[href]').forEach(a => {
            let t = a.textContent.trim();
            if (!t) t = 'لینک';
            add('link', t, a.href);
        });
        document.querySelectorAll('button[onclick], button[formaction]').forEach(btn => {
            let h = btn.getAttribute('formaction') || btn.getAttribute('onclick') || '';
            add('button', btn.textContent.trim(), h);
        });
        document.querySelectorAll('[onclick]').forEach(el => {
            if (el.tagName === 'A' || el.tagName === 'BUTTON') return;
            let h = el.getAttribute('onclick') || '';
            add('element', el.textContent.trim().substring(0,30) || 'کلیک', h);
        });
        return items;
    }""")
    links = []
    for typ, txt, href in raw:
        if not href: continue
        if not (href.startswith("http://") or href.startswith("https://")):
            m = re.search(r"(https?://[^\s'\"]*)", href)
            if m: href = m.group(0)
        if href.startswith("http"): links.append((typ, txt, href))

    # ضبط لینک‌های ویدیویی با Network monitoring
    video_urls = []
    def capture_response(response):
        if response.request.resource_type in ("media",):
            video_urls.append(response.url)
        elif "video" in (response.headers.get("content-type") or ""):
            video_urls.append(response.url)
    page.on("response", capture_response)
    # یکبار دیگه wait باشه
    page.wait_for_timeout(1500)
    page.remove_listener("response", capture_response)

    unique_videos = list(dict.fromkeys(video_urls))
    return links, unique_videos
# ═══════════════════════════════════════
# ابزارهای فایل
# ═══════════════════════════════════════
def is_direct_file_url(url: str) -> bool:
    exts = ['.zip','.rar','.7z','.pdf','.mp4','.mkv','.avi','.mp3',
            '.exe','.apk','.dmg','.iso','.tar','.gz','.bz2','.xz','.whl']
    path = urlparse(url).path.lower()
    return any(path.endswith(e) for e in exts)

def get_filename_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    name = os.path.basename(path)
    return name if name and '.' in name else "downloaded_file"

def crawl_for_download_link(start_url: str) -> Optional[str]:
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

def split_file_binary(file_path: str, prefix: str, ext: str) -> List[str]:
    d = os.path.dirname(file_path) or "."
    parts = []
    with open(file_path, "rb") as f:
        i = 1
        while True:
            chunk = f.read(ZIP_PART_SIZE)
            if not chunk: break
            pname = f"{prefix}.part{i:03d}{ext}"
            ppath = os.path.join(d, pname)
            with open(ppath, "wb") as pf: pf.write(chunk)
            parts.append(ppath)
            i += 1
    return parts

def create_zip_and_split(src: str, base: str) -> List[str]:
    d = os.path.dirname(src) or "."
    zp = os.path.join(d, f"{base}.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(src, os.path.basename(src))
    if os.path.getsize(zp) <= ZIP_PART_SIZE:
        return [zp]
    parts = split_file_binary(zp, base, ".zip")
    os.remove(zp)
    return parts

# ═══════════════════════════════════════
# اسکرین‌شات
# ═══════════════════════════════════════
def screenshot_full(context, url: str, out: str):
    page = context.new_page()
    try:
        page.goto(url, timeout=90000, wait_until="networkidle")
        page.wait_for_timeout(2000)
        page.screenshot(path=out, full_page=True)
    finally:
        page.close()

def screenshot_4k(context, url: str, out: str):
    page = context.new_page()
    try:
        page.set_viewport_size({"width": 3840, "height": 2160})
        page.goto(url, timeout=90000, wait_until="networkidle")
        page.wait_for_timeout(3000)
        page.screenshot(path=out, full_page=True)
    finally:
        page.close()

# ═══════════════════════════════════════
# صف و Worker
# ═══════════════════════════════════════
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
def pop_queued() -> Optional[Job]:
    with queue_lock:
        q = load_queue()
        for i, item in enumerate(q):
            if item["status"] == "queued":
                job = Job(**item)
                q[i]["status"] = "running"
                save_queue(q)
                return job
    return None
def find_job(jid: str) -> Optional[Job]:
    for item in load_queue():
        if item["job_id"] == jid: return Job(**item)
    return None
def update_job(job: Job):
    with queue_lock:
        q = load_queue()
        for i, item in enumerate(q):
            if item["job_id"] == job.job_id:
                q[i] = asdict(job)
                save_queue(q)
                return
        q.append(asdict(job))
        save_queue(q)
def job_queue_position(jid: str) -> int:
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

def worker_loop(worker_id: int, stop_event: threading.Event):
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

# ═══════════════════════════════════════
# پردازش Job (اصلاح‌شده)
# ═══════════════════════════════════════
def process_job(worker_id: int, job: Job):
    chat_id = job.chat_id
    session = get_session(chat_id)

    if job.mode == "download_execute":
        job_dir = os.path.join("jobs_data", job.job_id)
        os.makedirs(job_dir, exist_ok=True)
        try:
            execute_download(job, job_dir)
        except Exception as e:
            send_message(chat_id, f"❌ خطا در دانلود: {e}")
            job.status = "error"; job.error_message = str(e); update_job(job)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)
        return

    # اگر برای اسکن فایل‌های بزرگ باشه (Job جدید از مرورگر)
    if job.mode == "scan_files":
        handle_scan_files(job)
        return

    # برای blind_download
    if job.mode == "blind_download":
        handle_blind_download(job)
        return

    session.current_job_id = job.job_id
    set_session(session)

    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        if session.cancel_requested:
            raise InterruptedError("cancel")

        if job.mode == "screenshot":
            send_message(chat_id, f"📸 در حال اسکرین‌شات از:\n{job.url}")
            ctx = get_or_create_context(chat_id)
            spath = os.path.join(job_dir, "screenshot.png")
            screenshot_full(ctx, job.url, spath)
            send_document(chat_id, spath, caption="✅ اسکرین‌شات")
            kb = {"inline_keyboard": [[{"text": "🖼️ 4K", "callback_data": f"req4k_{job.job_id}"}]]}
            send_message(chat_id, "برای کیفیت بالاتر:", reply_markup=kb)
            job.status = "done"; update_job(job)

        elif job.mode == "4k_screenshot":
            send_message(chat_id, "🔍 4K...")
            ctx = get_or_create_context(chat_id)
            spath = os.path.join(job_dir, "screenshot_4k.png")
            screenshot_4k(ctx, job.url, spath)
            send_document(chat_id, spath, caption="✅ اسکرین‌شات 4K")
            job.status = "done"; update_job(job)

        elif job.mode == "download":
            handle_download(job, job_dir)

        elif job.mode in ("browser", "browser_click"):
            handle_browser(job, job_dir)

        else:
            send_message(chat_id, "❌ حالت نامعتبر")
            job.status = "error"; update_job(job)

    except InterruptedError:
        send_message(chat_id, "⏹️ لغو شد.")
        job.status = "cancelled"; update_job(job)
    except Exception as e:
        send_message(chat_id, f"❌ خطا: {e}")
        job.status = "error"; job.error_message = str(e); update_job(job)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        final = find_job(job.job_id)
        if final and final.status in ("done","error","cancelled"):
            s = get_session(chat_id)
            if s.state == "browsing":
                # اگر مرورگر فعال بود، دستش نزن
                pass
            else:
                s.state = "idle"
                s.current_job_id = None
                s.cancel_requested = False
                set_session(s)
                send_message(chat_id, "🔄 آماده.", reply_markup=main_menu_keyboard())

# ═══════════════════════════════════════
# دانلود هوشمند + دانلود کور
# ═══════════════════════════════════════
def handle_download(job: Job, job_dir: str):
    chat_id = job.chat_id
    url = job.url
    # لایه ۱: لینک مستقیم
    if is_direct_file_url(url):
        direct_link = url
    else:
        # لایه ۲: خزیدن
        send_message(chat_id, "🔎 جستجوی فایل...")
        direct_link = crawl_for_download_link(url)
        if not direct_link:
            # لایه ۳: دانلود کور
            send_message(chat_id, "⚠️ تحلیل جواب نداد. تغییر به حالت دانلود کور...")
            job.mode = "blind_download"
            job.url = url
            update_job(job)
            handle_blind_download(job)
            return

    # نمایش اطلاعات فایل
    try:
        head = requests.head(direct_link, timeout=10, allow_redirects=True)
        size = head.headers.get("Content-Length")
        size_str = f"{int(size)/1024/1024:.2f} MB" if size else "نامشخص"
        ftype = head.headers.get("Content-Type", "unknown")
    except:
        size_str = "نامشخص"; ftype = "unknown"
    fname = get_filename_from_url(direct_link)

    text = f"📄 فایل:\nنام: {fname}\nنوع: {ftype}\nحجم: {size_str}\nانتخاب کنید:"
    kb = {"inline_keyboard": [
        [{"text":"📦 ZIP","callback_data":f"dlzip_{job.job_id}"}, {"text":"📄 اصلی","callback_data":f"dlraw_{job.job_id}"}],
        [{"text":"❌ لغو","callback_data":f"canceljob_{job.job_id}"}]
    ]}
    send_message(chat_id, text, reply_markup=kb)
    job.status = "awaiting_user"
    job.extra = {"direct_link": direct_link, "filename": fname}
    update_job(job)

def handle_blind_download(job: Job):
    """دانلود کور: مستقیم دانلود کن و همون اول اطلاعات بگیر"""
    chat_id = job.chat_id
    url = job.url
    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)
    send_message(chat_id, "⏳ دانلود اولیه برای تشخیص...")
    try:
        with requests.get(url, stream=True, timeout=120, headers={"User-Agent": "Mozilla/5.0"}) as r:
            r.raise_for_status()
            content_type = r.headers.get("Content-Type", "application/octet-stream")
            length = r.headers.get("Content-Length")
            fname = get_filename_from_url(url)
            # اگر اسم فایل حدس پسوند نداشت، از content-type حدس بزن
            if '.' not in fname:
                extmap = {"video/mp4":".mp4", "application/pdf":".pdf", "application/zip":".zip"}
                for ct, ext in extmap.items():
                    if ct in content_type:
                        fname += ext
                        break
            file_path = os.path.join(job_dir, fname)
            with open(file_path, "wb") as f:
                for chunk in r.iter_content(8192): f.write(chunk)
        size = os.path.getsize(file_path)
        size_str = f"{size/1024/1024:.2f} MB"
        text = f"📄 فایل (کور):\nنام: {fname}\nنوع: {content_type}\nحجم: {size_str}\nانتخاب کنید:"
        kb = {"inline_keyboard": [
            [{"text":"📦 ZIP","callback_data":f"dlblindzip_{job.job_id}"},
             {"text":"📄 اصلی","callback_data":f"dlblindra_{job.job_id}"}],
            [{"text":"❌ لغو","callback_data":f"canceljob_{job.job_id}"}]
        ]}
        send_message(chat_id, text, reply_markup=kb)
        job.status = "awaiting_user"
        job.extra = {"file_path": file_path, "filename": fname, "pack_zip": False}
        update_job(job)
    except Exception as e:
        send_message(chat_id, f"❌ دانلود کور ناموفق: {e}")
        job.status = "error"; update_job(job)

def execute_download(job: Job, job_dir: str):
    chat_id = job.chat_id
    extra = job.extra
    # اگر از blind آمده باشد مسیر فایل موجود است
    if "file_path" in extra:
        fpath = extra["file_path"]
        fname = extra["filename"]
    else:
        url = extra["direct_link"]; fname = extra["filename"]
        fpath = os.path.join(job_dir, fname)
        send_message(chat_id, "⏳ دانلود...")
        with requests.get(url, stream=True, timeout=120, headers={"User-Agent":"Mozilla/5.0"}) as r:
            r.raise_for_status()
            with open(fpath, "wb") as f:
                for chunk in r.iter_content(8192): f.write(chunk)

    is_zip = extra.get("pack_zip", False)
    if is_zip:
        parts = create_zip_and_split(fpath, fname); label = "ZIP"
    else:
        base, ext = os.path.splitext(fname)
        parts = split_file_binary(fpath, base, ext); label = "اصلی"
    for idx, p in enumerate(parts, 1):
        send_document(chat_id, p, caption=f"{label} پارت {idx}/{len(parts)}")
    job.status = "done"; update_job(job)

# ═══════════════════════════════════════
# اسکن فایل‌های بزرگ (ابزار سوم)
# ═══════════════════════════════════════
def handle_scan_files(job: Job):
    chat_id = job.chat_id
    session = get_session(chat_id)
    links = session.browser_links
    if not links:
        send_message(chat_id, "❌ لینکی برای اسکن نیست.")
        return
    send_message(chat_id, f"🔍 اسکن {len(links)} لینک...")
    results = []
    for link in links:
        try:
            head = requests.head(link["href"], timeout=8, allow_redirects=True)
            cl = head.headers.get("Content-Length")
            if cl:
                size_mb = int(cl)/1024/1024
                if size_mb >= 1:
                    results.append((link["text"], link["href"], size_mb))
        except: pass
    if not results:
        send_message(chat_id, "🚫 هیچ فایل بزرگ‌تر از ۱MB پیدا نشد.")
        return
    lines = ["📊 فایل‌های بزرگ:"]
    kb_rows = []
    for idx, (text, href, size) in enumerate(results[:20]):
        lines.append(f"{text[:20]} – {size:.1f}MB")
        kb_rows.append([{"text": f"⬇️ {text[:15]} {size:.1f}MB", "callback_data": f"bigdl_{chat_id}_{idx}"}])
        with callback_map_lock: callback_map[f"bigdl_{chat_id}_{idx}"] = href
    send_message(chat_id, "\n".join(lines))
    if kb_rows:
        kb_rows.append([{"text":"❌ بستن", "callback_data":"close_scan"}])
        send_message(chat_id, "برای دانلود انتخاب کنید:", reply_markup={"inline_keyboard": kb_rows})
    # ذخیره کال‌بک‌ها در session?
    job.status = "done"; update_job(job)

# ═══════════════════════════════════════
# مرورگر تعاملی (اصلاح‌شده)
# ═══════════════════════════════════════
def handle_browser(job: Job, job_dir: str):
    chat_id = job.chat_id
    ctx = get_or_create_context(chat_id)
    page = ctx.new_page()
    try:
        page.goto(job.url, timeout=90000, wait_until="networkidle")
        page.wait_for_timeout(2000)
        spath = os.path.join(job_dir, "browser.png")
        page.screenshot(path=spath, full_page=True)
        links, video_urls = extract_clickable_and_media(page)

        # ساخت لیست لینک‌ها برای اسکن احتمالی
        all_links_for_scan = []
        keyboard_rows = []
        idx = 0
        # دکمه‌های معمولی
        for typ, text, href in links:
            if typ == "video" or href in video_urls:
                # لینک ویدیو – دکمه دانلود مستقیم
                cb = f"dlvid_{chat_id}_{idx}"
                with callback_map_lock: callback_map[cb] = href
                keyboard_rows.append([{"text": f"🎬 {text[:20]}", "callback_data": cb}])
            else:
                cb = f"nav_{chat_id}_{idx}"
                with callback_map_lock: callback_map[cb] = href
                keyboard_rows.append([{"text": text[:25], "callback_data": cb}])
            all_links_for_scan.append({"text": text, "href": href})
            idx += 1

        # ویدیوهای شبکه
        for vurl in video_urls:
            cb = f"dlvid_{chat_id}_{idx}"
            with callback_map_lock: callback_map[cb] = vurl
            keyboard_rows.append([{"text": f"🎬 ویدیو", "callback_data": cb}])
            idx += 1

        # اگر تعداد زیاد بود، کامندهای متنی
        if len(keyboard_rows) > 30:
            extra = keyboard_rows[30:]
            keyboard_rows = keyboard_rows[:30]
            cmds = {}
            lines = ["🔹 لینک‌های بیشتر:"]
            for row in extra:
                for btn in row:
                    cmd = f"/a{hashlib.md5(btn['callback_data'].encode()).hexdigest()[:5]}"
                    cmds[cmd] = btn['callback_data']
                    lines.append(f"{cmd} : {btn['text']}")
            send_message(chat_id, "\n".join(lines))
            sess = get_session(chat_id)
            sess.text_links = cmds
            set_session(sess)

        # دکمه‌های پایینی
        keyboard_rows.append([{"text": "🔍 اسکن فایل‌های بزرگ", "callback_data": f"scan_{chat_id}"}])
        keyboard_rows.append([{"text": "❌ بستن مرورگر", "callback_data": f"closebrowser_{chat_id}"}])
        kb = {"inline_keyboard": keyboard_rows}
        send_document(chat_id, spath, caption=f"🌐 {job.url}")
        send_message(chat_id, "برای پیمایش:", reply_markup=kb)

        # ذخیره لینک‌ها برای اسکن در session
        session = get_session(chat_id)
        session.state = "browsing"
        session.browser_links = all_links_for_scan
        set_session(session)

        job.status = "done"; update_job(job)
    finally:
        page.close()

# ═══════════════════════════════════════
# مدیریت پیام و Callback (اصلاحی)
# ═══════════════════════════════════════
def handle_message(chat_id: int, text: str):
    session = get_session(chat_id)
    text = text.strip()

    if text == "/start":
        session.state = "idle"; set_session(session)
        if not session.is_pro:
            send_message(chat_id, "👋 یکی از کدهای پرو را وارد کنید:")
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
            send_message(chat_id, "✅ تأیید شد!", reply_markup=main_menu_keyboard())
        else:
            send_message(chat_id, "⛔ کد نامعتبر")
        return

    # حالت browsing: قبول کامندهای متنی
    if session.state == "browsing":
        if session.text_links and text in session.text_links:
            cb = session.text_links.pop(text)
            set_session(session)
            with callback_map_lock:
                url = callback_map.pop(cb, None)
            if url:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="browser", url=url))
            return
        # در غیر این صورت نادیده بگیر (منو نمی‌دیم)
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
        enqueue(job)
        session.state = "idle"; session.current_job_id = job.job_id
        set_session(session)
        pos = job_queue_position(job.job_id)
        send_message(chat_id, f"✅ در صف (نوبت {pos})" if pos != 1 else "✅ در صف قرار گرفت.")
        return

    # پیش‌فرض
    send_message(chat_id, "از منو استفاده کنید:", reply_markup=main_menu_keyboard())

def handle_callback(cq: Dict):
    cid = cq["id"]; msg = cq.get("message"); data = cq.get("data", "")
    if not msg: return answer_callback_query(cid)
    chat_id = msg["chat"]["id"]
    session = get_session(chat_id)

    if data == "menu_screenshot":
        session.state = "waiting_url_screenshot"; set_session(session)
        answer_callback_query(cid, "URL را بفرستید")
        send_message(chat_id, "📸 URL:")
    elif data == "menu_download":
        session.state = "waiting_url_download"; set_session(session)
        answer_callback_query(cid, "URL را بفرستید")
        send_message(chat_id, "📥 URL:")
    elif data == "menu_browser":
        session.state = "waiting_url_browser"; set_session(session)
        answer_callback_query(cid, "URL را بفرستید")
        send_message(chat_id, "🧭 URL:")
    elif data == "menu_cancel":
        session.state = "idle"; session.cancel_requested = True
        session.current_job_id = None; set_session(session)
        close_user_context(chat_id)
        answer_callback_query(cid, "لغو شد")
        send_message(chat_id, "✅ لغو شد.", reply_markup=main_menu_keyboard())
    elif data.startswith("req4k_"):
        jid = data[6:]
        job = find_job(jid)
        if job and job.status == "done":
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="4k_screenshot", url=job.url))
            answer_callback_query(cid, "4K ثبت شد")
    elif data.startswith("dlzip_") or data.startswith("dlraw_"):
        jid = data[6:] if data.startswith("dlzip_") else data[6:]
        job = find_job(jid)
        if job and job.extra:
            is_zip = data.startswith("dlzip_")
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download_execute",
                        url=job.url,
                        extra={"direct_link": job.extra.get("direct_link",""), "filename": job.extra.get("filename",""), "pack_zip": is_zip}))
            job.status = "done"; update_job(job)
            answer_callback_query(cid, "شروع دانلود")
    elif data.startswith("canceljob_"):
        jid = data[10:]
        job = find_job(jid)
        if job: job.status = "cancelled"; update_job(job)
        answer_callback_query(cid, "لغو")
    elif data.startswith("nav_"):
        parts = data.split("_", 2)
        if len(parts) >= 3:
            cb = f"{parts[0]}_{parts[1]}_{parts[2]}"
            with callback_map_lock: url = callback_map.pop(cb, None)
            if url:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="browser", url=url))
                answer_callback_query(cid, "بارگذاری...")
    elif data.startswith("dlvid_"):
        parts = data.split("_", 2)
        if len(parts) >= 3:
            cb = f"{parts[0]}_{parts[1]}_{parts[2]}"
            with callback_map_lock: url = callback_map.pop(cb, None)
            if url:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download", url=url))
                answer_callback_query(cid, "دانلود ویدیو")
    elif data.startswith("scan_"):
        # درخواست اسکن فایل‌های بزرگ
        enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="scan_files", url=""))
        answer_callback_query(cid, "اسکن شروع شد")
    elif data.startswith("bigdl_"):
        parts = data.split("_", 2)
        if len(parts) >= 3:
            cb = f"{parts[0]}_{parts[1]}_{parts[2]}"
            with callback_map_lock: url = callback_map.pop(cb, None)
            if url:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download", url=url))
                answer_callback_query(cid, "دانلود فایل بزرگ")
    elif data == "close_scan":
        answer_callback_query(cid, "بسته شد")
    elif data.startswith("closebrowser_"):
        close_user_context(chat_id)
        session.state = "idle"; set_session(session)
        answer_callback_query(cid, "مرورگر بسته شد")
        send_message(chat_id, "🧭 بسته شد.", reply_markup=main_menu_keyboard())
    else:
        answer_callback_query(cid)

# ═══════════════════════════════════════
# Polling و main
# ═══════════════════════════════════════
def polling_loop(stop_event: threading.Event):
    offset = None
    safe_print("[Polling] شروع")
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
    safe_print("✅ ربات نهایی اجرا شد")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        stop_event.set()

if __name__ == "__main__":
    main()
