#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات بله – مرورگر تعاملی، دانلودر هوشمند، اسکرین‌شات 4K
با سیستم اشتراک پرو (۵ کد)
نسخه‌ی سبک و سریع – ذخیره‌سازی محلی
"""

import os, sys, json, time, math, queue, shutil, zipfile, uuid
import threading, traceback
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional, List, Tuple
from urllib.parse import urlparse, urljoin, unquote

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ════════════════════════════════════
# تنظیمات اصلی
# ════════════════════════════════════
BALE_BOT_TOKEN = os.getenv("BALE_BOT_TOKEN", "").strip()
if not BALE_BOT_TOKEN:
    print("ERROR: BALE_BOT_TOKEN not set", file=sys.stderr)
    sys.exit(1)

BALE_API_URL = "https://tapi.bale.ai/bot" + BALE_BOT_TOKEN
REQUEST_TIMEOUT = 30
LONG_POLL_TIMEOUT = 50
WORKER_COUNT = 3
ZIP_PART_SIZE = int(19.5 * 1024 * 1024)

# ۵ کد ثابت اشتراک پرو
PRO_CODES = ["PRO2024A", "PRO2024B", "PRO2024C", "PRO2024D", "PRO2024E"]

# قفل‌ها
print_lock = threading.Lock()
queue_lock = threading.Lock()
workers_lock = threading.Lock()
callback_map: Dict[str, str] = {}
callback_map_lock = threading.Lock()


def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs, flush=True)


# ════════════════════════════════════
# کلاس‌های داده
# ════════════════════════════════════
@dataclass
class SessionState:
    chat_id: int
    state: str = "idle"                     # idle, waiting_url_screenshot, ...
    is_pro: bool = False                    # اشتراک ویژه
    current_job_id: Optional[str] = None
    browser_url: Optional[str] = None
    last_interaction: float = time.time()
    cancel_requested: bool = False

@dataclass
class Job:
    job_id: str
    chat_id: int
    mode: str                               # screenshot, download, browser, browser_click, 4k_screenshot, download_execute
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

def load_sessions() -> Dict[str, Any]:
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_sessions(data: Dict[str, Any]):
    tmp = SESSIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SESSIONS_FILE)

def get_session(chat_id: int) -> SessionState:
    data = load_sessions()
    key = str(chat_id)
    if key in data:
        return SessionState(**data[key])
    return SessionState(chat_id=chat_id)

def set_session(session: SessionState):
    data = load_sessions()
    data[str(session.chat_id)] = asdict(session)
    save_sessions(data)

def is_pro(chat_id: int) -> bool:
    return get_session(chat_id).is_pro


# ════════════════════════════════════
# ابزارهای API بله
# ════════════════════════════════════
def bale_request(method: str, params: Optional[Dict] = None, files=None) -> Any:
    url = f"{BALE_API_URL}/{method}"
    try:
        if files:
            resp = requests.post(url, data=params or {}, files=files, timeout=REQUEST_TIMEOUT)
        else:
            resp = requests.post(url, json=params or {}, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            safe_print(f"Bale API error {method}: {resp.status_code} {resp.text}")
            return None
        data = resp.json()
        if not data.get("ok", False):
            safe_print(f"Bale API not ok {method}: {data}")
            return None
        return data["result"]
    except Exception as e:
        safe_print(f"Exception in bale_request {method}: {e}")
        return None

def send_message(chat_id: int, text: str, reply_markup=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup)
    return bale_request("sendMessage", params=params)

def send_document(chat_id: int, file_path: str, caption: str = ""):
    with open(file_path, "rb") as f:
        files = {"document": (os.path.basename(file_path), f)}
        params = {"chat_id": chat_id}
        if caption:
            params["caption"] = caption
        return bale_request("sendDocument", params=params, files=files)

def send_photo(chat_id: int, file_path: str, caption: str = "", reply_markup=None):
    with open(file_path, "rb") as f:
        files = {"photo": (os.path.basename(file_path), f)}
        params = {"chat_id": chat_id}
        if caption:
            params["caption"] = caption
        if reply_markup:
            params["reply_markup"] = json.dumps(reply_markup)
        return bale_request("sendPhoto", params=params, files=files)

def answer_callback_query(cq_id: str, text: str = "", show_alert: bool = False):
    params = {"callback_query_id": cq_id}
    if text:
        params["text"] = text
    if show_alert:
        params["show_alert"] = True
    return bale_request("answerCallbackQuery", params=params)

def get_updates(offset=None, timeout=LONG_POLL_TIMEOUT) -> List[Dict]:
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    result = bale_request("getUpdates", params=params)
    if result is None:
        return []
    return result


# ════════════════════════════════════
# منوها
# ════════════════════════════════════
def main_menu_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🧭 مرورگر من", "callback_data": "menu_browser"}],
            [{"text": "📸 اسکرین‌شات از سایت", "callback_data": "menu_screenshot"}],
            [{"text": "📥 دانلود محتوای سایت", "callback_data": "menu_download"}],
            [{"text": "❌ لغو / تنظیم مجدد", "callback_data": "menu_cancel"}]
        ]
    }

def page_actions_keyboard(urls: List[Tuple[str, str]], chat_id: int) -> Dict[str, Any]:
    keyboard_rows = []
    for idx, (text, href) in enumerate(urls):
        if len(text) > 25:
            text = text[:22] + "..."
        cb_id = f"nav_{chat_id}_{idx}"
        with callback_map_lock:
            callback_map[cb_id] = href
        keyboard_rows.append([{"text": text, "callback_data": cb_id}])
    return {"inline_keyboard": keyboard_rows}


# ════════════════════════════════════
# Playwright
# ════════════════════════════════════
browser_contexts: Dict[str, Any] = {}
browser_contexts_lock = threading.Lock()

def setup_browser():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-zygote"]
    )
    return playwright, browser

def get_or_create_context(chat_id: int):
    ctx_key = str(chat_id)
    with browser_contexts_lock:
        existing = browser_contexts.get(ctx_key)
        if existing and time.time() - existing["last_used"] < 600:
            existing["last_used"] = time.time()
            return existing["context"]
        if existing:
            try:
                existing["context"].close()
            except:
                pass
        pw, browser = setup_browser()
        context = browser.new_context(viewport={"width": 1280, "height": 720})
        browser_contexts[ctx_key] = {"context": context, "playwright": pw, "browser": browser, "last_used": time.time()}
        return context

def close_user_context(chat_id: int):
    ctx_key = str(chat_id)
    with browser_contexts_lock:
        existing = browser_contexts.pop(ctx_key, None)
    if existing:
        try:
            existing["context"].close()
        except:
            pass
        try:
            existing["browser"].close()
        except:
            pass
        try:
            existing["playwright"].stop()
        except:
            pass


# ════════════════════════════════════
# ابزارهای استخراج صفحه و فایل
# ════════════════════════════════════
def extract_clickable_links(page) -> List[Tuple[str, str]]:
    links = page.evaluate("""
        () => {
            const res = [];
            document.querySelectorAll('a[href]').forEach(a => {
                const text = (a.textContent || '').trim().substring(0, 30);
                if (text && a.href) res.push([text, a.href]);
            });
            // حذف تکراری
            const seen = new Set();
            const unique = [];
            for (const [t,h] of res) {
                if (!seen.has(h)) { seen.add(h); unique.push([t,h]); }
            }
            return unique.slice(0, 80);
        }
    """)
    return [(item[0], item[1]) for item in links]

def is_direct_file_url(url: str) -> bool:
    exts = ['.zip','.rar','.7z','.pdf','.mp4','.mkv','.avi','.mp3','.exe','.apk','.dmg','.iso','.tar','.gz','.bz2','.xz']
    path = urlparse(url).path.lower()
    return any(path.endswith(e) for e in exts)

def get_filename_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    name = os.path.basename(path)
    return name if (name and '.' in name) else "downloaded_file"

def crawl_for_download_link(start_url: str) -> Optional[str]:
    visited = set()
    to_visit = queue.Queue()
    to_visit.put((start_url, 0))
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    pc = 0
    while not to_visit.empty():
        url_cur, depth = to_visit.get()
        if url_cur in visited or depth > 1 or pc > 10: break
        visited.add(url_cur); pc += 1
        try:
            r = session.get(url_cur, timeout=10)
        except: continue
        if is_direct_file_url(url_cur): return url_cur
        if "text/html" in r.headers.get("Content-Type", ""):
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = urljoin(url_cur, a["href"])
                if is_direct_file_url(href): return href
                to_visit.put((href, depth+1))
    return None

# تقسیم فایل
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
    if os.path.getsize(zp) <= ZIP_PART_SIZE: return [zp]
    parts = split_file_binary(zp, base, ".zip")
    os.remove(zp)
    return parts

# اسکرین‌شات
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
# مدیریت صف و Worker
# ════════════════════════════════════
QUEUE_FILE = "queue.json"

def load_queue(): 
    try: 
        with open(QUEUE_FILE, "r") as f: return json.load(f)
    except: return []

def save_queue(data): 
    tmp = QUEUE_FILE+".tmp"
    with open(tmp, "w") as f: json.dump(data, f, ensure_ascii=False, indent=2)
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

def find_job(job_id: str) -> Optional[Job]:
    q = load_queue()
    for item in q:
        if item["job_id"] == job_id: return Job(**item)
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

def job_queue_position(job_id: str) -> int:
    q = load_queue()
    pos = 1
    for item in q:
        if item["status"] == "queued":
            if item["job_id"] == job_id: return pos
            pos += 1
    return -1

# Workers
WORKERS_FILE = "workers.json"
def load_workers(): 
    try: 
        with open(WORKERS_FILE) as f: return json.load(f)
    except:
        return [asdict(WorkerInfo(i)) for i in range(WORKER_COUNT)]
def save_workers(data): 
    tmp = WORKERS_FILE+".tmp"
    with open(tmp, "w") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, WORKERS_FILE)

def find_idle_worker():
    with workers_lock:
        wlist = load_workers()
        for w in wlist:
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
                safe_print(f"Worker {worker_id} error: {e}")
                traceback.print_exc()
            finally:
                set_worker_idle(worker_id)
        else:
            time.sleep(2)


# ════════════════════════════════════
# هستهٔ پردازش Job
# ════════════════════════════════════
def process_job(worker_id, job: Job):
    chat_id = job.chat_id
    session = get_session(chat_id)

    if job.mode == "download_execute":
        job_dir = os.path.join("jobs_data", job.job_id)
        os.makedirs(job_dir, exist_ok=True)
        try:
            execute_download(job, job_dir)
        except Exception as e:
            safe_print(f"dl execute error: {e}")
            send_message(chat_id, f"❌ خطا: {e}")
            job.status = "error"; job.error_message = str(e); update_job(job)
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
            kb = {"inline_keyboard": [[{"text": "🖼️ 4K", "callback_data": f"req4k_{job.job_id}"}]]}
            send_photo(chat_id, spath, caption=f"✅ {job.url}")
            send_message(chat_id, "کیفیت بالاتر؟", reply_markup=kb)
            job.status = "done"; update_job(job)

        elif job.mode == "4k_screenshot":
            send_message(chat_id, "🔍 4K...")
            ctx = get_or_create_context(chat_id)
            spath = os.path.join(job_dir, "screenshot_4k.png")
            screenshot_4k(ctx, job.url, spath)
            send_photo(chat_id, spath, caption=f"✅ 4K {job.url}")
            job.status = "done"; update_job(job)

        elif job.mode == "download":
            handle_download(job, job_dir)

        elif job.mode in ("browser", "browser_click"):
            handle_browser(job, job_dir)

        else:
            send_message(chat_id, "❌ حالت نامعتبر")
            job.status = "error"; update_job(job)

    except InterruptedError:
        send_message(chat_id, "⏹️ لغو شد."); job.status = "cancelled"; update_job(job)
    except Exception as e:
        send_message(chat_id, f"❌ خطا: {e}"); job.status = "error"; job.error_message = str(e); update_job(job)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        final = find_job(job.job_id)
        if final and final.status in ("done","error","cancelled"):
            s = get_session(chat_id)
            s.state = "idle"; s.current_job_id = None; s.cancel_requested = False
            set_session(s)
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
            send_message(chat_id, "❌ یافت نشد"); job.status = "error"; update_job(job); return

    try:
        head = requests.head(direct_link, timeout=10, allow_redirects=True)
        size_str = f"{int(head.headers.get('Content-Length',0))/1024/1024:.2f} MB" if 'Content-Length' in head.headers else "نامشخص"
        ftype = head.headers.get('Content-Type', 'unknown')
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
    with requests.get(url, stream=True, timeout=120) as r:
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
        links = extract_clickable_links(page)
        if links:
            kb = page_actions_keyboard(links, chat_id)
            kb["inline_keyboard"].append([{"text":"❌ بستن","callback_data":f"closebrowser_{chat_id}"}])
            cap = f"🌐 {job.url}"
        else:
            kb = main_menu_keyboard(); cap = f"🌐 {job.url}\nبدون لینک"
        send_photo(chat_id, spath, caption=cap, reply_markup=kb)
        job.status = "done"; update_job(job)
    finally:
        page.close()


# ════════════════════════════════════
# مدیریت پیام‌ها و Callback
# ════════════════════════════════════
def handle_message(chat_id: int, text: str):
    session = get_session(chat_id)
    if text.strip() == "/start":
        session.state = "idle"; session.cancel_requested = False; set_session(session)
        if not session.is_pro:
            send_message(chat_id, "👋 خوش آمدید!\nبرای استفاده از ربات یکی از کدهای اشتراک پرو را وارد کنید:")
        else:
            send_message(chat_id, "منوی اصلی:", reply_markup=main_menu_keyboard())
        return

    if text.strip() == "/cancel":
        session.state = "idle"; session.cancel_requested = True; session.current_job_id = None
        set_session(session)
        close_user_context(chat_id)
        send_message(chat_id, "⏹️ لغو شد.", reply_markup=main_menu_keyboard())
        return

    # اگر کاربر پرو نباشه، فقط کد قبول می‌کنیم
    if not session.is_pro:
        if text.strip() in PRO_CODES:
            session.is_pro = True; set_session(session)
            send_message(chat_id, "✅ کد تأیید شد! اکنون میتوانید از ربات استفاده کنید.", reply_markup=main_menu_keyboard())
        else:
            send_message(chat_id, "⛔ کد نامعتبر. لطفاً یکی از کدهای پرو را وارد کنید:")
        return

    # کاربر پرو است
    if session.state.startswith("waiting_url_"):
        url = text.strip()
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
        if find_idle_worker():
            send_message(chat_id, "✅ در صف قرار گرفت.")
        else:
            pos = job_queue_position(job.job_id)
            send_message(chat_id, f"✅ در صف (نوبت {pos})")
        return

    # حالت پیش‌فرض
    send_message(chat_id, "از منو استفاده کنید:", reply_markup=main_menu_keyboard())

def handle_callback(cq: Dict):
    cid = cq["id"]; msg = cq.get("message"); data = cq.get("data","")
    if not msg: answer_callback_query(cid); return
    chat_id = msg["chat"]["id"]
    session = get_session(chat_id)

    if data == "menu_screenshot":
        if not session.is_pro: answer_callback_query(cid,"اشتراک پرو نیاز است"); return
        session.state = "waiting_url_screenshot"; set_session(session)
        send_message(chat_id, "📸 URL اسکرین‌شات:")
    elif data == "menu_download":
        if not session.is_pro: answer_callback_query(cid,"اشتراک پرو نیاز است"); return
        session.state = "waiting_url_download"; set_session(session)
        send_message(chat_id, "📥 URL دانلود:")
    elif data == "menu_browser":
        if not session.is_pro: answer_callback_query(cid,"اشتراک پرو نیاز است"); return
        session.state = "waiting_url_browser"; set_session(session)
        send_message(chat_id, "🧭 URL مرورگر:")
    elif data == "menu_cancel":
        session.state = "idle"; session.cancel_requested = True; session.current_job_id = None
        set_session(session)
        close_user_context(chat_id)
        send_message(chat_id, "✅ لغو شد.", reply_markup=main_menu_keyboard())
    elif data.startswith("req4k_"):
        jid = data[6:]
        job = find_job(jid)
        if job and job.status == "done":
            new_job = Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="4k_screenshot", url=job.url)
            enqueue(new_job)
            send_message(chat_id, "🖼️ درخواست 4K ثبت شد.")
    elif data.startswith("dlzip_") or data.startswith("dlraw_"):
        jid = data[6:] if data.startswith("dlzip_") else data[6:]
        job = find_job(jid)
        if job and job.status == "awaiting_user" and job.extra:
            is_zip = data.startswith("dlzip_")
            new_job = Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download_execute", url=job.url,
                          extra={"direct_link": job.extra["direct_link"], "filename": job.extra["filename"], "pack_zip": is_zip})
            enqueue(new_job)
            job.status = "done"; update_job(job)
            send_message(chat_id, "⬇️ دانلود آغاز شد...")
    elif data.startswith("canceljob_"):
        jid = data[10:]
        job = find_job(jid)
        if job: job.status = "cancelled"; update_job(job)
        send_message(chat_id, "❌ لغو شد.", reply_markup=main_menu_keyboard())
    elif data.startswith("nav_"):
        parts = data.split("_", 2)
        if len(parts) >= 3:
            cb = f"{parts[0]}_{parts[1]}_{parts[2]}"
            with callback_map_lock: url = callback_map.pop(cb, None)
            if url:
                enqueue(Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="browser", url=url))
    elif data.startswith("closebrowser_"):
        close_user_context(chat_id)
        send_message(chat_id, "🧭 بسته شد.", reply_markup=main_menu_keyboard())
    answer_callback_query(cid)


# ════════════════════════════════════
# Polling و main
# ════════════════════════════════════
def polling_loop(stop_event):
    offset = None
    while not stop_event.is_set():
        try:
            updates = get_updates(offset=offset, timeout=LONG_POLL_TIMEOUT)
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
    safe_print("ربات پرو اجرا شد")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        stop_event.set()

if __name__ == "__main__":
    main()
