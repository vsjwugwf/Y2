#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات بله – نسخهٔ ۸: مرورگر تعاملی، دانلود هوشمند (همزمان/ذخیره)، اسکرین‌شات 4K،
اسکن فایل‌های بزرگ، ضبط ویدیو MKV، دانلود یوتیوب، تنظیمات کاربر، فایل راهنما،
رفع تمام باگ‌های قبلی.
"""

import os, sys, json, time, math, queue, shutil, zipfile, uuid, re, hashlib
import subprocess, threading, traceback
from dataclasses import dataclass, asdict, field
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import urlparse, urljoin, unquote

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ═══════════════════════
# تنظیمات
# ═══════════════════════
BALE_BOT_TOKEN = os.getenv("BALE_BOT_TOKEN", "").strip()
if not BALE_BOT_TOKEN:
    print("ERROR: BALE_BOT_TOKEN not set", file=sys.stderr)
    sys.exit(1)

BALE_API_URL = "https://tapi.bale.ai/bot" + BALE_BOT_TOKEN
REQUEST_TIMEOUT = 30
LONG_POLL_TIMEOUT = 50
WORKER_COUNT = 1
ZIP_PART_SIZE = int(19.5 * 1024 * 1024)   # 19.5 MB

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
    record_time: int = 20                     # ثانیه
    auto_play_video: bool = True
    default_download_mode: str = "store"      # "store" یا "stream"

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
    browser_page: int = 0                     # صفحه فعلی در نمایش دکمه‌ها
    settings: UserSettings = field(default_factory=UserSettings)

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
        s.__dict__.update(data[key])
        # بازسازی settings
        if "settings" in data[key]:
            s.settings = UserSettings(**data[key]["settings"])
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
    return bale_request("answerCallbackQuery", {"callback_query_id": cq_id, "text": text, "show_alert": show_alert})

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
        [{"text": "📸 اسکرین‌شات", "callback_data": "menu_screenshot"}],
        [{"text": "📥 دانلود", "callback_data": "menu_download"}],
        [{"text": "⚙️ تنظیمات", "callback_data": "menu_settings"}],
        [{"text": "❌ لغو / ریست", "callback_data": "menu_cancel"}]
    ]}

def settings_keyboard(settings: UserSettings):
    rec = settings.record_time
    play_status = "فعال ✅" if settings.auto_play_video else "غیرفعال ❌"
    dl_mode = "سریع ⚡" if settings.default_download_mode == "stream" else "عادی 💾"
    return {"inline_keyboard": [
        [{"text": f"⏱️ ضبط: {rec}s", "callback_data": "set_rec"}],
        [{"text": f"▶️ پخش: {play_status}", "callback_data": "set_play"}],
        [{"text": f"📥 دانلود: {dl_mode}", "callback_data": "set_dlmode"}],
        [{"text": "🔙 بازگشت", "callback_data": "back_main"}]
    ]}

# ═══════════════════════
# Playwright – global
# ═══════════════════════
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
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                      "--disable-blink-features=AutomationControlled"]
            )
        # برای سایت‌های خاص از Viewport موبایل استفاده کنیم
        context = _global_browser.new_context(
            viewport={"width": 412, "height": 915},
            user_agent="Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        )
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
# استخراج پیشرفته‌ی المان‌های قابل کلیک
# ═══════════════════════
def extract_clickable_and_media(page):
    # فقط المان‌های visible
    raw = page.evaluate("""() => {
        const items = [];
        const seen = new Set();
        function add(type, text, href) {
            if (!href || seen.has(href)) return;
            seen.add(href);
            items.push([type, text.trim().substring(0, 35), href]);
        }
        function isVisible(el) {
            const style = window.getComputedStyle(el);
            return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetWidth > 0 && el.offsetHeight > 0;
        }
        document.querySelectorAll('a[href]').forEach(a => {
            if (!isVisible(a)) return;
            let t = a.textContent.trim() || 'لینک';
            add('link', t, a.href);
        });
        document.querySelectorAll('button[onclick], button[formaction]').forEach(btn => {
            if (!isVisible(btn)) return;
            let h = btn.getAttribute('formaction') || btn.getAttribute('onclick') || '';
            add('button', btn.textContent.trim(), h);
        });
        document.querySelectorAll('[onclick]').forEach(el => {
            if (el.tagName === 'A' || el.tagName === 'BUTTON') return;
            if (!isVisible(el)) return;
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
            else: continue
        links.append((typ, txt, href))

    video_urls = []
    def capture_response(response):
        if response.request.resource_type in ("media",):
            video_urls.append(response.url)
        elif "video" in (response.headers.get("content-type") or ""):
            video_urls.append(response.url)
    page.on("response", capture_response)
    page.wait_for_timeout(1500)
    page.remove_listener("response", capture_response)
    return links, list(dict.fromkeys(video_urls))

# ═══════════════════════
# ابزارهای فایل
# ═══════════════════════
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
            # برای ZIP از نام‌گذاری ویژه استفاده کن
            if ext.lower() == ".zip":
                pname = f"{prefix}.zip.{i:03d}"      # file.zip.001, file.zip.002
            else:
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

# ═══════════════════════
# اسکرین‌شات
# ═══════════════════════
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

# ═══════════════════════
# پردازش Job
# ═══════════════════════
def process_job(worker_id: int, job: Job):
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

    if job.mode == "scan_files":
        handle_scan_files(job)
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

    session.current_job_id = job.job_id
    set_session(session)

    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        if session.cancel_requested:
            raise InterruptedError("cancel")

        if job.mode == "screenshot":
            ctx = get_or_create_context(chat_id)
            spath = os.path.join(job_dir, "screenshot.png")
            screenshot_full(ctx, job.url, spath)
            send_document(chat_id, spath, caption="✅ اسکرین‌شات")
            kb = {"inline_keyboard": [[{"text": "🖼️ 4K", "callback_data": f"req4k_{job.job_id}"}]]}
            send_message(chat_id, "برای کیفیت بالاتر:", reply_markup=kb)
            job.status = "done"; update_job(job)

        elif job.mode == "4k_screenshot":
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
            if s.state != "browsing":
                s.state = "idle"
                s.current_job_id = None
                s.cancel_requested = False
                set_session(s)
                send_message(chat_id, "🔄 آماده.", reply_markup=main_menu_keyboard())

# ═══════════════════════
# دانلود (با انتخاب روش)
# ═══════════════════════
def handle_download(job: Job, job_dir: str):
    chat_id = job.chat_id
    url = job.url
    if is_direct_file_url(url):
        direct_link = url
    else:
        send_message(chat_id, "🔎 جستجوی فایل...")
        direct_link = crawl_for_download_link(url)
        if not direct_link:
            send_message(chat_id, "⚠️ تغییر به دانلود کور...")
            job.mode = "blind_download"
            job.url = url
            update_job(job)
            handle_blind_download(job)
            return

    try:
        head = requests.head(direct_link, timeout=10, allow_redirects=True)
        size = head.headers.get("Content-Length")
        size_str = f"{int(size)/1024/1024:.2f} MB" if size else "نامشخص"
        ftype = head.headers.get("Content-Type", "unknown")
    except:
        size_str = "نامشخص"; ftype = "unknown"
    fname = get_filename_from_url(direct_link)

    # انتخاب روش دانلود
    kb = {"inline_keyboard": [
        [{"text": "⚡ سریع (همزمان)", "callback_data": f"stream_{job.job_id}"},
         {"text": "💾 عادی (ذخیره)", "callback_data": f"store_{job.job_id}"}],
        [{"text": "❌ لغو", "callback_data": f"canceljob_{job.job_id}"}]
    ]}
    send_message(chat_id, f"📄 فایل:\n{fname} ({size_str})\nروش دانلود:", reply_markup=kb)
    job.status = "awaiting_user"
    job.extra = {"direct_link": direct_link, "filename": fname}
    update_job(job)

def handle_blind_download(job: Job):
    chat_id = job.chat_id
    url = job.url
    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)
    send_message(chat_id, "⏳ دانلود اولیه...")
    try:
        with requests.get(url, stream=True, timeout=120, headers={"User-Agent": "Mozilla/5.0"}) as r:
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "application/octet-stream")
            fname = get_filename_from_url(url)
            if '.' not in fname:
                if "video" in ct: fname += ".mp4"
                elif "pdf" in ct: fname += ".pdf"
            fpath = os.path.join(job_dir, fname)
            with open(fpath, "wb") as f:
                for chunk in r.iter_content(8192): f.write(chunk)
        size = os.path.getsize(fpath)
        size_str = f"{size/1024/1024:.2f} MB"
        kb = {"inline_keyboard": [
            [{"text":"📦 ZIP","callback_data":f"dlblindzip_{job.job_id}"},
             {"text":"📄 اصلی","callback_data":f"dlblindra_{job.job_id}"}],
            [{"text":"❌ لغو","callback_data":f"canceljob_{job.job_id}"}]
        ]}
        send_message(chat_id, f"📄 فایل (کور): {fname} ({size_str})", reply_markup=kb)
        job.status = "awaiting_user"
        job.extra = {"file_path": fpath, "filename": fname}
        update_job(job)
    except Exception as e:
        send_message(chat_id, f"❌ دانلود کور ناموفق: {e}")
        job.status = "error"; update_job(job)
        shutil.rmtree(job_dir, ignore_errors=True)

def execute_download(job: Job, job_dir: str):
    chat_id = job.chat_id
    extra = job.extra
    stream_mode = extra.get("stream_mode", False)
    pack_zip = extra.get("pack_zip", False)
    fname = extra["filename"]

    if "file_path" in extra:             # از blind آمده
        fpath = extra["file_path"]
    else:
        url = extra["direct_link"]
        fpath = os.path.join(job_dir, fname)
        if stream_mode:
            # دانلود همزمان و ارسال پارت‌ها
            send_message(chat_id, "⚡ دانلود همزمان...")
            parts = download_and_stream(url, fname, job_dir)
            job.status = "done"; update_job(job)
            return
        else:
            send_message(chat_id, "⏳ دانلود...")
            with requests.get(url, stream=True, timeout=120, headers={"User-Agent":"Mozilla/5.0"}) as r:
                r.raise_for_status()
                with open(fpath, "wb") as f:
                    for chunk in r.iter_content(8192): f.write(chunk)

    if pack_zip:
        parts = create_zip_and_split(fpath, fname)
        label = "ZIP"
    else:
        base, ext = os.path.splitext(fname)
        parts = split_file_binary(fpath, base, ext)
        label = "اصلی"

    # فایل راهنما
    instr_path = os.path.join(job_dir, "merge_instructions.txt")
    with open(instr_path, "w") as f:
        if pack_zip:
            f.write("همه‌ی فایل‌ها را دانلود کنید، سپس فایل .001 را با WinRAR یا 7-Zip باز کنید.")
        else:
            f.write(f"برای ادغام: copy /b {'+'.join([os.path.basename(p) for p in parts])} {fname}")
    send_document(chat_id, instr_path, caption="📝 راهنما")

    for idx, p in enumerate(parts, 1):
        send_document(chat_id, p, caption=f"{label} پارت {idx}/{len(parts)}")
    job.status = "done"; update_job(job)

def download_and_stream(url: str, fname: str, job_dir: str) -> List[str]:
    """دانلود و ارسال همزمان بدون ذخیره کل فایل"""
    base, ext = os.path.splitext(fname)
    part_idx = 1
    buffer = b""
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent":"Mozilla/5.0"}) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=8192):
            buffer += chunk
            while len(buffer) >= ZIP_PART_SIZE:
                part_data = buffer[:ZIP_PART_SIZE]
                buffer = buffer[ZIP_PART_SIZE:]
                part_name = f"{base}.part{part_idx:03d}{ext}"
                part_path = os.path.join(job_dir, part_name)
                with open(part_path, "wb") as pf: pf.write(part_data)
                send_document(job.chat_id, part_path, caption=f"⚡ پارت {part_idx}")
                os.remove(part_path)
                part_idx += 1
        if buffer:
            part_name = f"{base}.part{part_idx:03d}{ext}"
            part_path = os.path.join(job_dir, part_name)
            with open(part_path, "wb") as pf: pf.write(buffer)
            send_document(job.chat_id, part_path, caption=f"⚡ پارت {part_idx}")
            os.remove(part_path)

# ═══════════════════════
# اسکن فایل‌های بزرگ
# ═══════════════════════
def handle_scan_files(job: Job):
    chat_id = job.chat_id
    session = get_session(chat_id)
    links = session.browser_links
    if not links:
        send_message(chat_id, "❌ لینکی نیست.")
        return
    send_message(chat_id, f"🔍 اسکن {len(links)} لینک...")
    results = []
    for link in links:
        try:
            head = requests.head(link["href"], timeout=8, allow_redirects=True)
            cl = head.headers.get("Content-Length")
            if cl and int(cl)/1024/1024 >= 1:
                results.append((link["text"], link["href"], int(cl)/1024/1024))
        except: pass
    if not results:
        send_message(chat_id, "🚫 فایل بزرگتر از ۱MB پیدا نشد.")
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
        send_message(chat_id, "برای دانلود:", reply_markup={"inline_keyboard": kb_rows})
    job.status = "done"; update_job(job)

# ═══════════════════════
# ضبط ویدیو (با تنظیمات)
# ═══════════════════════
def handle_record_video(job: Job):
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
            page.goto(url, timeout=90000, wait_until="networkidle")
            if auto_play:
                try:
                    page.evaluate("() => { const v = document.querySelector('video'); if (v) v.play(); }")
                except: pass
                try:
                    page.click('.play-button, [aria-label="Play"], .ytp-large-play-button', timeout=2000)
                except: pass
            page.wait_for_timeout(rec_time * 1000)
        finally:
            page.close()
            context.close()

        webm_file = None
        for f in os.listdir(job_dir):
            if f.endswith('.webm'):
                webm_file = os.path.join(job_dir, f)
                break
        if not webm_file:
            send_message(chat_id, "❌ ویدیویی ضبط نشد.")
            job.status = "error"; update_job(job)
            return

        mkv_path = webm_file.replace('.webm', '.mkv')
        subprocess.run(['ffmpeg', '-y', '-i', webm_file, '-c', 'copy', mkv_path],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        final_path = mkv_path if os.path.exists(mkv_path) else webm_file

        if os.path.getsize(final_path) > ZIP_PART_SIZE:
            parts = split_file_binary(final_path, "record", os.path.splitext(final_path)[1])
            for idx, p in enumerate(parts, 1):
                send_document(chat_id, p, caption=f"🎬 پارت {idx}/{len(parts)}")
        else:
            send_document(chat_id, final_path, caption="🎬 ویدیوی ضبط‌شده")
        job.status = "done"; update_job(job)
    except Exception as e:
        send_message(chat_id, f"❌ خطا: {e}")
        job.status = "error"; update_job(job)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)

# ═══════════════════════
# یوتیوب
# ═══════════════════════
def handle_youtube_download(job: Job):
    chat_id = job.chat_id
    url = job.url
    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)
    send_message(chat_id, "📥 دریافت ویدیوی یوتیوب...")
    try:
        outtmpl = os.path.join(job_dir, "%(title)s-%(id)s.%(ext)s")
        cmd = ["yt-dlp", "--no-playlist", "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
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
# مرورگر (صفحه‌بندی، دوتایی، فیلتر visibility)
# ═══════════════════════
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

        all_links = []
        for typ, text, href in links:
            if typ == "video" or href in video_urls:
                all_links.append({"type": "video", "text": text, "href": href})
            else:
                all_links.append({"type": "link", "text": text, "href": href})
        for vurl in video_urls:
            if not any(l["href"] == vurl for l in all_links):
                all_links.append({"type": "video", "text": "ویدیو", "href": vurl})

        session = get_session(chat_id)
        session.state = "browsing"
        session.browser_url = job.url
        session.browser_links = all_links
        session.browser_page = 0
        set_session(session)

        send_browser_page(chat_id, spath, job.url, 0)
        job.status = "done"; update_job(job)
    finally:
        page.close()

def send_browser_page(chat_id: int, image_path: Optional[str] = None, url: str = "", page_num: int = 0):
    session = get_session(chat_id)
    all_links = session.browser_links or []
    per_page = 10   # تعداد دکمه در هر صفحه (۵ ردیف دوتایی)
    start = page_num * per_page
    end = min(start + per_page, len(all_links))
    page_links = all_links[start:end]

    keyboard_rows = []
    idx = start
    row = []
    for link in page_links:
        label = link["text"][:20]
        if link["type"] == "video":
            cb = f"dlvid_{chat_id}_{idx}"
        else:
            cb = f"nav_{chat_id}_{idx}"
        with callback_map_lock: callback_map[cb] = link["href"]
        row.append({"text": label, "callback_data": cb})
        if len(row) == 2:
            keyboard_rows.append(row)
            row = []
        idx += 1
    if row:
        keyboard_rows.append(row)

    # ردیف‌های ناوبری و ابزارها
    nav_row = []
    if page_num > 0:
        nav_row.append({"text": "◀️ قبلی", "callback_data": f"bpg_{chat_id}_{page_num-1}"})
    if end < len(all_links):
        nav_row.append({"text": "بعدی ▶️", "callback_data": f"bpg_{chat_id}_{page_num+1}"})
    if nav_row:
        keyboard_rows.append(nav_row)

    # دکمه‌های ویژه
    is_youtube = ("youtube.com" in url and "/watch" in url) or "youtu.be" in url
    if is_youtube:
        keyboard_rows.append([{"text": "🎥 دانلود یوتیوب", "callback_data": f"ytdl_{chat_id}"}])
    keyboard_rows.append([{"text": "🎬 ضبط صفحه", "callback_data": f"recvid_{chat_id}"}])
    keyboard_rows.append([{"text": "🔍 اسکن فایل‌ها", "callback_data": f"scan_{chat_id}"}])
    keyboard_rows.append([{"text": "❌ بستن", "callback_data": f"closebrowser_{chat_id}"}])

    kb = {"inline_keyboard": keyboard_rows}
    if image_path:
        send_document(chat_id, image_path, caption=f"🌐 {url} (صفحه {page_num+1})")
    send_message(chat_id, f"صفحه {page_num+1}/{math.ceil(len(all_links)/per_page)}", reply_markup=kb)

    # ذخیره لینک‌های اضافی به صورت دستور
    extra_links = all_links[end:]
    if extra_links:
        cmds = {}
        lines = ["🔹 لینک‌های بیشتر (دستورها):"]
        for i, link in enumerate(extra_links):
            cmd = f"/a{hashlib.md5(link['href'].encode()).hexdigest()[:5]}"
            cmds[cmd] = link['href']
            lines.append(f"{cmd} : {link['text'][:30]}")
        send_message(chat_id, "\n".join(lines))
        session.text_links = cmds
        set_session(session)

# ═══════════════════════
# مدیریت تنظیمات
# ═══════════════════════
def settings_callback(cq_data, chat_id, session):
    if cq_data == "set_rec":
        # چرخش بین 10, 20, 30, 60
        opts = [10, 20, 30, 60]
        cur = session.settings.record_time
        nxt = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else 20
        session.settings.record_time = nxt
        set_session(session)
        send_message(chat_id, f"⏱️ زمان ضبط روی {nxt} ثانیه تنظیم شد.")
        send_message(chat_id, "تنظیمات:", reply_markup=settings_keyboard(session.settings))
    elif cq_data == "set_play":
        session.settings.auto_play_video = not session.settings.auto_play_video
        set_session(session)
        send_message(chat_id, "تنظیمات بروز شد.", reply_markup=settings_keyboard(session.settings))
    elif cq_data == "set_dlmode":
        session.settings.default_download_mode = "stream" if session.settings.default_download_mode == "store" else "store"
        set_session(session)
        send_message(chat_id, "تنظیمات بروز شد.", reply_markup=settings_keyboard(session.settings))
    elif cq_data == "back_main":
        send_message(chat_id, "منوی اصلی:", reply_markup=main_menu_keyboard())

# ═══════════════════════
# مدیریت پیام و Callback
# ═══════════════════════
def handle_message(chat_id: int, text: str):
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
            send_message(chat_id, "✅ فعال شد!", reply_markup=main_menu_keyboard())
        else:
            send_message(chat_id, "⛔ کد نامعتبر")
        return

    if session.state == "browsing":
        if session.text_links and text in session.text_links:
            cb = session.text_links.pop(text)
            set_session(session)
            with callback_map_lock: url = callback_map.pop(cb, None)
            if url:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="browser", url=url))
            return
        return

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

    send_message(chat_id, "از منو استفاده کنید:", reply_markup=main_menu_keyboard())

def handle_callback(cq: Dict):
    cid = cq["id"]; msg = cq.get("message"); data = cq.get("data", "")
    if not msg: return answer_callback_query(cid)
    chat_id = msg["chat"]["id"]
    session = get_session(chat_id)

    if data == "menu_screenshot":
        session.state = "waiting_url_screenshot"; set_session(session)
        send_message(chat_id, "📸 URL:")
    elif data == "menu_download":
        session.state = "waiting_url_download"; set_session(session)
        send_message(chat_id, "📥 URL:")
    elif data == "menu_browser":
        session.state = "waiting_url_browser"; set_session(session)
        send_message(chat_id, "🧭 URL:")
    elif data == "menu_settings":
        send_message(chat_id, "⚙️ تنظیمات:", reply_markup=settings_keyboard(session.settings))
    elif data == "menu_cancel":
        session.state = "idle"; session.cancel_requested = True
        session.current_job_id = None; set_session(session)
        close_user_context(chat_id)
        send_message(chat_id, "✅ لغو شد.", reply_markup=main_menu_keyboard())
    elif data in ("set_rec", "set_play", "set_dlmode", "back_main"):
        settings_callback(data, chat_id, session)
    elif data.startswith("req4k_"):
        jid = data[6:]
        job = find_job(jid)
        if job and job.status == "done":
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="4k_screenshot", url=job.url))
            answer_callback_query(cid, "4K ثبت شد")
    elif data.startswith("stream_") or data.startswith("store_"):
        jid = data[7:]
        job = find_job(jid)
        if job and job.extra:
            stream = data.startswith("stream_")
            # حالا بپرس ZIP یا اصلی
            kb = {"inline_keyboard": [
                [{"text":"📦 ZIP","callback_data":f"dlzip_{job.job_id}"},
                 {"text":"📄 اصلی","callback_data":f"dlraw_{job.job_id}"}],
                [{"text":"❌ لغو","callback_data":f"canceljob_{job.job_id}"}]
            ]}
            send_message(chat_id, "فشرده‌سازی؟", reply_markup=kb)
            job.extra["stream_mode"] = stream
            update_job(job)
    elif data.startswith("dlzip_") or data.startswith("dlraw_"):
        jid = data[6:] if data.startswith("dlzip_") else data[6:]
        job = find_job(jid)
        if job and job.extra:
            is_zip = data.startswith("dlzip_")
            job.extra["pack_zip"] = is_zip
            job.status = "done"; update_job(job)
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download_execute", url=job.url, extra=job.extra))
            answer_callback_query(cid, "شروع دانلود")
    elif data.startswith("dlblindzip_") or data.startswith("dlblindra_"):
        jid = data[11:] if data.startswith("dlblindzip_") else data[11:]
        job = find_job(jid)
        if job and job.extra and "file_path" in job.extra:
            is_zip = data.startswith("dlblindzip_")
            job.extra["pack_zip"] = is_zip
            job.status = "done"; update_job(job)
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download_execute", url=job.url, extra=job.extra))
            answer_callback_query(cid, "شروع دانلود")
    elif data.startswith("canceljob_"):
        jid = data[10:]
        job = find_job(jid)
        if job:
            job.status = "cancelled"; update_job(job)
            answer_callback_query(cid, "لغو")
            send_message(chat_id, "❌ دانلود لغو شد.", reply_markup=main_menu_keyboard())
    elif data.startswith("nav_"):
        parts = data.split("_", 2)
        if len(parts) >= 3:
            cb = f"{parts[0]}_{parts[1]}_{parts[2]}"
            with callback_map_lock: url = callback_map.pop(cb, None)
            if url:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="browser", url=url))
    elif data.startswith("dlvid_"):
        parts = data.split("_", 2)
        if len(parts) >= 3:
            cb = f"{parts[0]}_{parts[1]}_{parts[2]}"
            with callback_map_lock: url = callback_map.pop(cb, None)
            if url:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download", url=url))
    elif data.startswith("scan_"):
        enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="scan_files", url=""))
    elif data.startswith("recvid_"):
        url = session.browser_url
        if url:
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="record_video", url=url))
    elif data.startswith("ytdl_"):
        url = session.browser_url
        if url:
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="youtube_download", url=url))
    elif data.startswith("bpg_"):
        parts = data.split("_")
        if len(parts) == 3:
            page_num = int(parts[2])
            session.browser_page = page_num
            set_session(session)
            send_browser_page(chat_id, page_num=page_num)
            answer_callback_query(cid)
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
    safe_print("✅ ربات نسخه ۸ اجرا شد")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        stop_event.set()

if __name__ == "__main__":
    main()
