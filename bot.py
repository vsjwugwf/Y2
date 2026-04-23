#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات بله – مرورگر تعاملی، دانلودر هوشمند، اسکرین‌شات 4K، اشتراک پرو
نسخه‌ی نهایی با تمام رفع باگ‌ها و قابلیت‌های جدید
"""

import os, sys, json, time, math, queue, shutil, zipfile, uuid, re
import threading, traceback, hashlib
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import urlparse, urljoin, unquote

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ════════════════════════════════════
# تنظیمات
# ════════════════════════════════════
BALE_BOT_TOKEN = os.getenv("BALE_BOT_TOKEN", "").strip()
if not BALE_BOT_TOKEN:
    print("ERROR: BALE_BOT_TOKEN not set", file=sys.stderr)
    sys.exit(1)

BALE_API_URL = "https://tapi.bale.ai/bot" + BALE_BOT_TOKEN
REQUEST_TIMEOUT = 30
LONG_POLL_TIMEOUT = 50
WORKER_COUNT = 1                    # فقط یک Worker برای جلوگیری از تداخل Playwright
ZIP_PART_SIZE = int(19.5 * 1024 * 1024)

PRO_CODES = ["PRO2024A", "PRO2024B", "PRO2024C", "PRO2024D", "PRO2024E"]

# قفل‌ها
print_lock = threading.Lock()
queue_lock = threading.Lock()
workers_lock = threading.Lock()
callback_map: Dict[str, str] = {}
callback_map_lock = threading.Lock()
browser_contexts_lock = threading.Lock()

# دیکشنری موقت برای نگاشت کامندهای متنی به URL (مخصوص مرورگر)
text_commands: Dict[str, str] = {}
text_commands_lock = threading.Lock()

def safe_print(*args, **kwargs):
    with print_lock: print(*args, **kwargs, flush=True)


# ════════════════════════════════════
# کلاس‌های داده
# ════════════════════════════════════
@dataclass
class SessionState:
    chat_id: int
    state: str = "idle"                     # idle, waiting_url_screenshot, waiting_url_download, waiting_url_browser, browsing
    is_pro: bool = False
    current_job_id: Optional[str] = None
    browser_url: Optional[str] = None
    last_interaction: float = time.time()
    cancel_requested: bool = False
    # برای مرورگر: لینک‌های ذخیره‌شده برای کامندهای متنی
    text_links: Optional[Dict[str, str]] = None   # command -> url

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


# ════════════════════════════════════
# ذخیره‌سازی محلی Sessionها
# ════════════════════════════════════
SESSIONS_FILE = "sessions.json"
def load_sessions():
    try:
        with open(SESSIONS_FILE, "r") as f: return json.load(f)
    except: return {}
def save_sessions(data):
    with open(SESSIONS_FILE+".tmp", "w") as f: json.dump(data, f)
    os.replace(SESSIONS_FILE+".tmp", SESSIONS_FILE)
def get_session(chat_id):
    data = load_sessions()
    key = str(chat_id)
    if key in data: return SessionState(**data[key])
    return SessionState(chat_id=chat_id)
def set_session(session):
    data = load_sessions()
    data[str(session.chat_id)] = asdict(session)
    save_sessions(data)
def is_pro(chat_id):
    return get_session(chat_id).is_pro


# ════════════════════════════════════
# API بله
# ════════════════════════════════════
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

def send_photo(chat_id, file_path, caption="", reply_markup=None):
    # دیگر استفاده نمی‌شود، اما نگه می‌داریم
    with open(file_path, "rb") as f:
        return bale_request("sendPhoto",
                            params={"chat_id": chat_id, "caption": caption},
                            files={"photo": (os.path.basename(file_path), f)})

def answer_callback_query(cq_id, text="", show_alert=False):
    return bale_request("answerCallbackQuery", {"callback_query_id": cq_id, "text": text, "show_alert": show_alert})

def get_updates(offset=None, timeout=LONG_POLL_TIMEOUT):
    params = {"timeout": timeout, "offset": offset} if offset else {"timeout": timeout}
    return bale_request("getUpdates", params=params) or []


# ════════════════════════════════════
# منوها
# ════════════════════════════════════
def main_menu_keyboard():
    return {"inline_keyboard": [
        [{"text": "🧭 مرورگر من", "callback_data": "menu_browser"}],
        [{"text": "📸 اسکرین‌شات از سایت", "callback_data": "menu_screenshot"}],
        [{"text": "📥 دانلود محتوای سایت", "callback_data": "menu_download"}],
        [{"text": "❌ لغو / تنظیم مجدد", "callback_data": "menu_cancel"}]
    ]}


# ════════════════════════════════════
# Playwright – سراسری
# ════════════════════════════════════
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


# ════════════════════════════════════
# استخراج لینک‌ها و دکمه‌های قابل کلیک (پیشرفته)
# ════════════════════════════════════
def extract_all_clickable(page) -> List[Tuple[str, str, str]]:
    """
    برمی‌گرداند: [(نوع، متن، آدرس)]
    نوع: 'link', 'video', 'button', 'div'
    """
    result = page.evaluate("""
        () => {
            const items = [];
            const seen = new Set();
            function add(type, text, href) {
                if (!href || seen.has(href)) return;
                seen.add(href);
                let t = text.trim().substring(0, 30);
                if (!t) t = type;
                items.push([type, t, href]);
            }
            // تگ a
            document.querySelectorAll('a[href]').forEach(a => {
                let text = (a.textContent || '').trim();
                if (!text && a.querySelector('img')) text = '🖼️';
                add('link', text, a.href);
            });
            // تگ button با onclick یا formaction
            document.querySelectorAll('button[onclick], button[formaction]').forEach(btn => {
                let href = btn.getAttribute('formaction') || btn.getAttribute('onclick');
                let text = (btn.textContent || '').trim();
                add('button', text, href);
            });
            // div با onclick
            document.querySelectorAll('div[onclick]').forEach(div => {
                let href = div.getAttribute('onclick');
                let text = (div.textContent || '').trim().substring(0, 30);
                add('div', text, href);
            });
            // عناصر با cursor:pointer و دارای onclick
            document.querySelectorAll('[onclick]').forEach(el => {
                if (el.tagName === 'A' || el.tagName === 'BUTTON' || el.tagName === 'DIV') return;
                let style = window.getComputedStyle(el);
                if (style.cursor === 'pointer') {
                    add('element', el.textContent.trim(), el.getAttribute('onclick'));
                }
            });
            // تشخیص ویدیو: لینک‌هایی که به فرمت ویدیو ختم می‌شوند
            const videoExts = ['.mp4', '.mkv', '.webm', '.avi', '.mov'];
            items.forEach(item => {
                const url = item[2].toLowerCase();
                for (const ext of videoExts) {
                    if (url.includes(ext)) {
                        item[0] = 'video';
                        break;
                    }
                }
            });
            return items;
        }
    """)
    # فیلتر: فقط لینک‌های معتبر (http/https) را نگه دار
    filtered = []
    for item in result:
        typ, txt, href = item
        if not href:
            continue
        # برای onclick سعی کن URL را استخراج کنی
        if not (href.startswith('http://') or href.startswith('https://')):
            # شاید داخل onclick آدرس باشد
            match = re.search(r"(https?://[^\s'\"]*)", href)
            if match:
                href = match.group(0)
            else:
                continue
        filtered.append((typ, txt, href))
    return filtered


def is_video_url(url):
    exts = ('.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv')
    return urlparse(url).path.lower().endswith(exts)


# ════════════════════════════════════
# ابزارهای فایل و دانلود
# ════════════════════════════════════
def is_direct_file_url(url):
    exts = ['.zip','.rar','.7z','.pdf','.mp4','.mkv','.avi','.mp3','.exe','.apk','.dmg','.iso','.tar','.gz','.bz2','.xz']
    path = urlparse(url).path.lower()
    return any(path.endswith(e) for e in exts)

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

def create_zip_and_split(src, base):
    d = os.path.dirname(src) or "."
    zp = os.path.join(d, f"{base}.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(src, os.path.basename(src))
    if os.path.getsize(zp) <= ZIP_PART_SIZE: return [zp]
    parts = split_file_binary(zp, base, ".zip")
    os.remove(zp)
    return parts


# ════════════════════════════════════
# اسکرین‌شات (خروجی PNG ذخیره)
# ════════════════════════════════════
def screenshot_full(context, url, out):
    page = context.new_page()
    try:
        page.goto(url, timeout=90000, wait_until="networkidle")
        page.wait_for_timeout(2000)
        page.screenshot(path=out, full_page=True)
    finally: page.close()

def screenshot_4k(context, url, out):
    page = context.new_page()
    try:
        page.set_viewport_size({"width": 3840, "height": 2160})
        page.goto(url, timeout=90000, wait_until="networkidle")
        page.wait_for_timeout(3000)
        page.screenshot(path=out, full_page=True)
    finally: page.close()


# ════════════════════════════════════
# صف و Worker
# ════════════════════════════════════
QUEUE_FILE = "queue.json"
def load_queue():
    try:
        with open(QUEUE_FILE) as f: return json.load(f)
    except: return []
def save_queue(data):
    with open(QUEUE_FILE+".tmp", "w") as f: json.dump(data, f)
    os.replace(QUEUE_FILE+".tmp", QUEUE_FILE)
def enqueue(job):
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
    with open(WORKERS_FILE+".tmp", "w") as f: json.dump(data, f)
    os.replace(WORKERS_FILE+".tmp", WORKERS_FILE)
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
            finally:
                set_worker_idle(worker_id)
        else:
            time.sleep(2)


# ════════════════════════════════════
# پردازش Job
# ════════════════════════════════════
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
            job.status = "error"
            update_job(job)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)
        return

    session.current_job_id = job.job_id
    set_session(session)
    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        if session.cancel_requested: raise InterruptedError("cancel")

        if job.mode == "screenshot":
            send_message(chat_id, f"📸 اسکرین‌شات از:\n{job.url}")
            ctx = get_or_create_context(chat_id)
            spath = os.path.join(job_dir, "screenshot.png")
            screenshot_full(ctx, job.url, spath)
            # ارسال به صورت فایل (با کیفیت اصلی)
            send_document(chat_id, spath, caption=f"✅ اسکرین‌شات")
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

        elif job.mode == "browser" or job.mode == "browser_click":
            handle_browser(job, job_dir)

        else:
            send_message(chat_id, "❌ حالت نامعتبر")
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
            if s.state == "browsing":
                # اگر مرورگر تمام شد به idle برگرد
                s.state = "idle"
            s.current_job_id = None
            s.cancel_requested = False
            set_session(s)
            # اگر browsing نبود، منو بده
            if s.state == "idle":
                send_message(chat_id, "🔄 آماده.", reply_markup=main_menu_keyboard())


def handle_download(job, job_dir):
    chat_id = job.chat_id
    url = job.url
    if is_direct_file_url(url):
        direct_link = url
    else:
        send_message(chat_id, "🔎 جستجوی فایل...")
        direct_link = crawl_for_download_link(url)
        if not direct_link:
            send_message(chat_id, "❌ فایل یافت نشد")
            job.status = "error"; update_job(job); return

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

def execute_download(job, job_dir):
    chat_id = job.chat_id
    extra = job.extra
    url = extra["direct_link"]; fname = extra["filename"]; is_zip = extra.get("pack_zip", False)
    fpath = os.path.join(job_dir, fname)
    send_message(chat_id, "⏳ دانلود...")
    headers = {"User-Agent": "Mozilla/5.0"}
    with requests.get(url, stream=True, timeout=120, headers=headers) as r:
        r.raise_for_status()
        with open(fpath, "wb") as f:
            for chunk in r.iter_content(8192): f.write(chunk)
    if is_zip:
        parts = create_zip_and_split(fpath, fname); label = "ZIP"
    else:
        base, ext = os.path.splitext(fname)
        parts = split_file_binary(fpath, base, ext); label = "اصلی"
    for idx, p in enumerate(parts, 1):
        send_document(chat_id, p, caption=f"{label} پارت {idx}/{len(parts)}")
    job.status = "done"; update_job(job)


def handle_browser(job, job_dir):
    chat_id = job.chat_id
    ctx = get_or_create_context(chat_id)
    page = ctx.new_page()
    try:
        page.goto(job.url, timeout=90000, wait_until="networkidle")
        page.wait_for_timeout(2000)
        spath = os.path.join(job_dir, "browser.png")
        page.screenshot(path=spath, full_page=True)
        items = extract_all_clickable(page)
        links = [(typ, text, url) for typ, text, url in items]
        # جدا کردن ویدیوها
        video_links = [(text, url) for typ, text, url in links if typ == 'video']
        other_links = [(text, url) for typ, text, url in links if typ != 'video']

        keyboard_rows = []
        idx = 0
        # دکمه‌های دیگر (حداکثر ۳۰)
        for text, href in other_links[:30]:
            cb = f"nav_{chat_id}_{idx}"
            with callback_map_lock: callback_map[cb] = href
            keyboard_rows.append([{"text": text[:25], "callback_data": cb}])
            idx += 1

        # دکمه‌های ویدیو (با callback مخصوص دانلود)
        for text, href in video_links:
            cb = f"dlvid_{chat_id}_{idx}"
            with callback_map_lock: callback_map[cb] = href
            keyboard_rows.append([{"text": f"🎬 {text[:20]}", "callback_data": cb}])
            idx += 1

        # اگر هنوز لینک‌های معمولی باقی مانده، به صورت کامند متنی در پیام جداگانه
        extra_links = other_links[30:]
        if extra_links:
            cmds = {}
            lines = ["🔹 لینک‌های بیشتر (دستور زیر را بفرستید):"]
            for text, href in extra_links:
                cmd = f"/a{hashlib.md5(href.encode()).hexdigest()[:5]}"
                cmds[cmd] = href
                lines.append(f"{cmd} : {text[:40]}")
            send_message(chat_id, "\n".join(lines))
            # ذخیره در session
            sess = get_session(chat_id)
            sess.text_links = cmds
            set_session(sess)

        # افزودن دکمه بستن
        keyboard_rows.append([{"text": "❌ بستن مرورگر", "callback_data": f"closebrowser_{chat_id}"}])
        kb = {"inline_keyboard": keyboard_rows}
        send_document(chat_id, spath, caption=f"🌐 {job.url}")
        send_message(chat_id, "برای پیمایش از دکمه‌ها استفاده کنید:", reply_markup=kb)

        # تغییر state به browsing
        session = get_session(chat_id)
        session.state = "browsing"
        set_session(session)

        job.status = "done"; update_job(job)
    finally:
        page.close()


# ════════════════════════════════════
# مدیریت پیام‌ها و Callback
# ════════════════════════════════════
def handle_message(chat_id, text):
    session = get_session(chat_id)
    text = text.strip()

    if text == "/start":
        session.state = "idle"; set_session(session)
        if not session.is_pro:
            send_message(chat_id, "👋 برای استفاده یکی از کدهای پرو را وارد کنید:")
        else:
            send_message(chat_id, "منوی اصلی:", reply_markup=main_menu_keyboard())
        return

    if text == "/cancel":
        session.state = "idle"; session.cancel_requested = True
        session.current_job_id = None
        set_session(session); close_user_context(chat_id)
        send_message(chat_id, "⏹️ لغو شد.", reply_markup=main_menu_keyboard())
        return

    # اگر کاربر پرو نیست
    if not session.is_pro:
        if text in PRO_CODES:
            session.is_pro = True; set_session(session)
            send_message(chat_id, "✅ کد تأیید شد!", reply_markup=main_menu_keyboard())
        else:
            send_message(chat_id, "⛔ کد نامعتبر")
        return

    # حالت browsing: قبول کامندهای متنی و نادیده گرفتن بقیه
    if session.state == "browsing":
        # چک کردن کامندهای متنی
        if session.text_links and text in session.text_links:
            url = session.text_links[text]
            # پاک کردن کامندها (منقضی)
            session.text_links = None; set_session(session)
            # ایجاد job برای مرور به این لینک
            new_job = Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="browser", url=url)
            enqueue(new_job)
            return
        # سایر پیام‌ها را نادیده بگیر
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
        if pos == 1: send_message(chat_id, "✅ در صف قرار گرفت.")
        else: send_message(chat_id, f"✅ در صف (نوبت {pos})")
        return

    # پیش‌فرض
    send_message(chat_id, "از منو استفاده کنید:", reply_markup=main_menu_keyboard())


def handle_callback(cq):
    cid = cq["id"]; msg = cq.get("message"); data = cq.get("data", "")
    if not msg: return answer_callback_query(cid)
    chat_id = msg["chat"]["id"]
    session = get_session(chat_id)

    if data == "menu_screenshot":
        if not session.is_pro: answer_callback_query(cid, "پرو لازم است"); return
        session.state = "waiting_url_screenshot"; set_session(session)
        send_message(chat_id, "📸 URL:")
    elif data == "menu_download":
        if not session.is_pro: answer_callback_query(cid, "پرو لازم است"); return
        session.state = "waiting_url_download"; set_session(session)
        send_message(chat_id, "📥 URL:")
    elif data == "menu_browser":
        if not session.is_pro: answer_callback_query(cid, "پرو لازم است"); return
        session.state = "waiting_url_browser"; set_session(session)
        send_message(chat_id, "🧭 URL:")
    elif data == "menu_cancel":
        session.state = "idle"; session.cancel_requested = True
        session.current_job_id = None; set_session(session)
        close_user_context(chat_id)
        send_message(chat_id, "✅ لغو شد.", reply_markup=main_menu_keyboard())
    elif data.startswith("req4k_"):
        jid = data[6:]
        job = find_job(jid)
        if job and job.status == "done":
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="4k_screenshot", url=job.url))
            answer_callback_query(cid, "درخواست 4K ثبت شد")
            send_message(chat_id, "🖼️ 4K ثبت شد")
    elif data.startswith("dlzip_") or data.startswith("dlraw_"):
        jid = data[6:] if data.startswith("dlzip_") else data[6:]
        job = find_job(jid)
        if job and job.status == "awaiting_user" and job.extra:
            is_zip = data.startswith("dlzip_")
            enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download_execute", url=job.url,
                        extra={"direct_link": job.extra["direct_link"], "filename": job.extra["filename"], "pack_zip": is_zip}))
            job.status = "done"; update_job(job)
            answer_callback_query(cid, "شروع دانلود")
            send_message(chat_id, "⬇️ دانلود...")
        else: answer_callback_query(cid, "منقضی شده")
    elif data.startswith("canceljob_"):
        jid = data[10:]
        job = find_job(jid)
        if job: job.status = "cancelled"; update_job(job)
        answer_callback_query(cid, "لغو شد")
        send_message(chat_id, "❌ لغو شد.")
    elif data.startswith("nav_"):
        parts = data.split("_", 2)
        if len(parts) >= 3:
            cb = f"{parts[0]}_{parts[1]}_{parts[2]}"
            with callback_map_lock: url = callback_map.pop(cb, None)
            if url:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="browser", url=url))
                answer_callback_query(cid, "بارگذاری...")
            else: answer_callback_query(cid, "منقضی")
    elif data.startswith("dlvid_"):
        # دانلود مستقیم ویدیو
        parts = data.split("_", 2)
        if len(parts) >= 3:
            cb = f"{parts[0]}_{parts[1]}_{parts[2]}"
            with callback_map_lock: url = callback_map.pop(cb, None)
            if url:
                # ایجاد job دانلود با لینک مستقیم
                j = Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download", url=url)
                enqueue(j)
                answer_callback_query(cid, "درخواست دانلود ویدیو")
                send_message(chat_id, "🎬 دانلود ویدیو شروع می‌شود...")
            else: answer_callback_query(cid, "منقضی")
    elif data.startswith("closebrowser_"):
        close_user_context(chat_id)
        session.state = "idle"; set_session(session)
        answer_callback_query(cid, "مرورگر بسته شد")
        send_message(chat_id, "🧭 بسته شد.", reply_markup=main_menu_keyboard())
    else:
        answer_callback_query(cid)


# ════════════════════════════════════
# Polling و Main
# ════════════════════════════════════
def polling_loop(stop_event):
    offset = None
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
    safe_print("✅ ربات پرو نهایی اجرا شد")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        stop_event.set()

if __name__ == "__main__":
    main()
