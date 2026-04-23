#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات بله – مرورگر تعاملی، دانلودر هوشمند و اسکرین‌شات حرفه‌ای
متصل به Tack Server (LLAN / LLAN)
"""

import os, sys, json, time, math, queue, shutil, zipfile, hashlib, uuid
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

TACK_SERVER = "https://tk-server.ir"
TACK_USER = "LLAN"
TACK_PASS = "LLAN"
TACK_EDIT_URL = f"{TACK_SERVER}/json/edit.php"
TACK_GET_URL_PREFIX = f"{TACK_SERVER}/json/"

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
    state: str = "idle"
    current_job_id: Optional[str] = None
    browser_url: Optional[str] = None
    last_interaction: float = time.time()
    cancel_requested: bool = False


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
# TackServerDB (اتصال به سرور JSON)
# ════════════════════════════════════
class TackServerDB:

    @staticmethod
    def _make_key(chat_id: int) -> str:
        return f"{TACK_USER}-{chat_id}"

    @staticmethod
    def save_session(session: SessionState) -> bool:
        key = TackServerDB._make_key(session.chat_id)
        payload = {
            "name": key,
            "pass": TACK_PASS,
            "data": json.dumps(asdict(session), ensure_ascii=False)
        }
        try:
            resp = requests.post(TACK_EDIT_URL, data=payload, timeout=15)
            return resp.status_code == 200 and resp.json().get("ok", False)
        except Exception as e:
            safe_print(f"TackServerDB save error: {e}")
            return False

    @staticmethod
    def load_session(chat_id: int) -> Optional[SessionState]:
        key = TackServerDB._make_key(chat_id)
        url = f"{TACK_GET_URL_PREFIX}{key}.json"
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data or isinstance(data, list):
                return None
            return SessionState(**data)
        except:
            return None
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
# منوهای شیشه‌ای
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
# Playwright – راه‌اندازی و Context
# ════════════════════════════════════
browser_contexts: Dict[str, Any] = {}
browser_contexts_lock = threading.Lock()


def setup_browser():
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-dev-shm-usage", "--disable-gpu",
            "--no-zygote", "--disable-web-security",
        ]
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
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"
        )
        browser_contexts[ctx_key] = {
            "context": context,
            "playwright": pw,
            "browser": browser,
            "last_used": time.time()
        }
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
# ابزارهای استخراج صفحه
# ════════════════════════════════════
def extract_clickable_links(page) -> List[Tuple[str, str]]:
    links = page.evaluate("""
        () => {
            const results = [];
            const anchors = document.querySelectorAll('a[href]');
            for (const a of anchors) {
                const text = (a.textContent || '').trim().substring(0, 30);
                if (text && a.href) results.push([text, a.href]);
            }
            const unique = [];
            const seen = new Set();
            for (const [text, href] of results) {
                if (!seen.has(href)) {
                    seen.add(href);
                    unique.push([text, href]);
                }
            }
            return unique.slice(0, 80);
        }
    """)
    return [(item[0], item[1]) for item in links]


def is_direct_file_url(url: str) -> bool:
    file_extensions = ['.zip', '.rar', '.7z', '.pdf', '.mp4', '.mkv', '.avi',
                       '.mp3', '.exe', '.apk', '.dmg', '.iso', '.tar', '.gz', '.bz2', '.xz']
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in file_extensions)


def get_filename_from_url(url: str) -> str:
    path = unquote(urlparse(url).path)
    name = os.path.basename(path)
    if not name or '.' not in name:
        name = "downloaded_file"
    return name


# ════════════════════════════════════
# خزنده برای یافتن لینک دانلود
# ════════════════════════════════════
def crawl_for_download_link(start_url: str, max_depth: int = 1, max_pages: int = 10) -> Optional[str]:
    visited = set()
    to_visit = queue.Queue()
    to_visit.put((start_url, 0))
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })

    page_count = 0
    while not to_visit.empty():
        url_cur, depth = to_visit.get()
        if url_cur in visited:
            continue
        if depth > max_depth or page_count > max_pages:
            break
        visited.add(url_cur)
        page_count += 1
        try:
            resp = session.get(url_cur, timeout=15)
        except:
            continue
        if resp.status_code != 200:
            continue

        if is_direct_file_url(url_cur):
            return url_cur

        if "text/html" in resp.headers.get("Content-Type", ""):
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = urljoin(url_cur, a["href"])
                if is_direct_file_url(href):
                    return href
                if depth + 1 <= max_depth:
                    p = urlparse(href)
                    if p.netloc == urlparse(url_cur).netloc:
                        to_visit.put((href, depth + 1))
    return None


# ════════════════════════════════════
# تقسیم فایل (اسپلیت)
# ════════════════════════════════════
def split_file_binary(file_path: str, part_prefix: str, original_ext: str) -> List[str]:
    dir_name = os.path.dirname(file_path) or "."
    part_paths = []

    with open(file_path, "rb") as f:
        part_index = 1
        while True:
            chunk = f.read(ZIP_PART_SIZE)
            if not chunk:
                break
            part_name = f"{part_prefix}.part{part_index:03d}{original_ext}"
            part_path = os.path.join(dir_name, part_name)
            with open(part_path, "wb") as pf:
                pf.write(chunk)
            part_paths.append(part_path)
            part_index += 1

    return part_paths


def create_zip_and_split(source_path: str, base_name: str) -> List[str]:
    dir_name = os.path.dirname(source_path) or "."
    zip_name = f"{base_name}.zip"
    zip_path = os.path.join(dir_name, zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(source_path, arcname=os.path.basename(source_path))

    if os.path.getsize(zip_path) <= ZIP_PART_SIZE:
        return [zip_path]
    parts = split_file_binary(zip_path, base_name, ".zip")
    os.remove(zip_path)
    return parts


# ════════════════════════════════════
# اسکرین‌شات‌ها
# ════════════════════════════════════
def take_screenshot_fullpage(context, url: str, out_path: str):
    page = context.new_page()
    try:
        page.goto(url, timeout=90000, wait_until="networkidle")
        page.wait_for_timeout(2000)
        page.screenshot(path=out_path, full_page=True)
    finally:
        page.close()


def take_screenshot_4k(context, url: str, out_path: str):
    page = context.new_page()
    try:
        page.set_viewport_size({"width": 3840, "height": 2160})
        page.goto(url, timeout=90000, wait_until="networkidle")
        page.wait_for_timeout(3000)
        page.screenshot(path=out_path, full_page=True)
    finally:
        page.close()
# ════════════════════════════════════
# مدیریت صف جاب‌ها
# ════════════════════════════════════
QUEUE_FILE = "queue.json"


def load_queue() -> List[Dict]:
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []


def save_queue(data: List[Dict]):
    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, QUEUE_FILE)


def enqueue_job(job: Job):
    with queue_lock:
        q = load_queue()
        q.append(asdict(job))
        save_queue(q)


def find_job_by_id(job_id: str) -> Optional[Job]:
    with queue_lock:
        q = load_queue()
        for item in q:
            if item["job_id"] == job_id:
                return Job(**item)
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


def pop_next_queued_job() -> Optional[Job]:
    with queue_lock:
        q = load_queue()
        for i, item in enumerate(q):
            if item["status"] == "queued":
                job = Job(**item)
                q[i]["status"] = "running"
                q[i]["updated_at"] = time.time()
                save_queue(q)
                return job
    return None


def get_job_queue_position(job_id: str) -> Optional[int]:
    with queue_lock:
        q = load_queue()
        pos = 1
        for item in q:
            if item["status"] == "queued":
                if item["job_id"] == job_id:
                    return pos
                pos += 1
    return None


# ════════════════════════════════════
# مدیریت Workerها
# ════════════════════════════════════
WORKERS_FILE = "workers.json"


def load_workers() -> List[Dict]:
    try:
        with open(WORKERS_FILE, "r") as f:
            return json.load(f)
    except:
        workers = []
        for i in range(WORKER_COUNT):
            workers.append(asdict(WorkerInfo(worker_id=i)))
        return workers


def save_workers(data: List[Dict]):
    tmp = WORKERS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, WORKERS_FILE)


def find_idle_worker() -> Optional[WorkerInfo]:
    with workers_lock:
        workers = load_workers()
        for w in workers:
            if w["status"] == "idle":
                return WorkerInfo(**w)
    return None


def set_worker_busy(worker_id: int, job_id: str):
    with workers_lock:
        workers = load_workers()
        for w in workers:
            if w["worker_id"] == worker_id:
                w["status"] = "busy"
                w["current_job_id"] = job_id
                break
        save_workers(workers)


def set_worker_idle(worker_id: int):
    with workers_lock:
        workers = load_workers()
        for w in workers:
            if w["worker_id"] == worker_id:
                w["status"] = "idle"
                w["current_job_id"] = None
                break
        save_workers(workers)


# ════════════════════════════════════
# حلقهٔ Worker
# ════════════════════════════════════
def worker_loop(worker_id: int, stop_event: threading.Event):
    safe_print(f"[Worker {worker_id}] شروع به کار")
    while not stop_event.is_set():
        idle_worker = find_idle_worker()
        if idle_worker and idle_worker.worker_id == worker_id:
            job = pop_next_queued_job()
            if job is None:
                time.sleep(2)
                continue
            set_worker_busy(worker_id, job.job_id)
            safe_print(f"[Worker {worker_id}] پردازش job {job.job_id}")
            try:
                process_job(worker_id, job)
            except Exception as e:
                safe_print(f"[Worker {worker_id}] خطای بحرانی: {e}")
                traceback.print_exc()
            finally:
                set_worker_idle(worker_id)
        else:
            time.sleep(2)
    safe_print(f"[Worker {worker_id}] متوقف شد")


# ════════════════════════════════════
# هستهٔ پردازش Job (نسخهٔ اصلاح‌شده)
# ════════════════════════════════════
def process_job(worker_id: int, job: Job):
    chat_id = job.chat_id
    session = TackServerDB.load_session(chat_id) or SessionState(chat_id=chat_id)

    # اگر این job یک زیر-عملیات دانلود واقعی است
    if job.mode == "download_execute":
        job_dir = os.path.join("jobs_data", job.job_id)
        os.makedirs(job_dir, exist_ok=True)
        try:
            execute_download(job, job_dir)
        except Exception as e:
            safe_print(f"download_execute error: {e}")
            traceback.print_exc()
            send_message(chat_id, f"❌ خطا در دانلود:\n{e}")
            job.status = "error"
            job.error_message = str(e)
            update_job(job)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)
        return

    session.current_job_id = job.job_id
    TackServerDB.save_session(session)

    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        if session.cancel_requested:
            raise InterruptedError("لغو توسط کاربر")

        if job.mode == "screenshot":
            send_message(chat_id, f"📸 در حال گرفتن اسکرین‌شات از:\n{job.url}")
            context = get_or_create_context(chat_id)
            screenshot_path = os.path.join(job_dir, "screenshot.png")
            take_screenshot_fullpage(context, job.url, screenshot_path)

            kb = {
                "inline_keyboard": [
                    [{"text": "🖼️ دریافت نسخه 4K", "callback_data": f"req4k_{job.job_id}"}]
                ]
            }
            send_photo(chat_id, screenshot_path, caption=f"✅ اسکرین‌شات از:\n{job.url}")
            send_message(chat_id, "اگر کیفیت فعلی راضی‌کننده نیست، می‌توانی نسخهٔ 4K را دریافت کنی:", reply_markup=kb)
            job.status = "done"
            update_job(job)

        elif job.mode == "4k_screenshot":
            send_message(chat_id, f"🔍 در حال گرفتن اسکرین‌شات 4K از:\n{job.url}")
            context = get_or_create_context(chat_id)
            shot_path = os.path.join(job_dir, "screenshot_4k.png")
            take_screenshot_4k(context, job.url, shot_path)
            send_photo(chat_id, shot_path, caption=f"✅ اسکرین‌شات 4K از:\n{job.url}")
            job.status = "done"
            update_job(job)

        elif job.mode == "download":
            handle_download(job, job_dir)

        elif job.mode == "browser":
            handle_browser(job, job_dir)

        elif job.mode == "browser_click":
            handle_browser_click(job, job_dir, session)

        else:
            send_message(chat_id, "❌ حالت ناشناخته.")
            job.status = "error"
            job.error_message = "حالت ناشناخته"
            update_job(job)

    except InterruptedError:
        send_message(chat_id, "⏹️ عملیات لغو شد.")
        job.status = "cancelled"
        update_job(job)
    except Exception as e:
        safe_print(f"[Worker {worker_id}] exception: {e}")
        traceback.print_exc()
        send_message(chat_id, f"❌ خطا در پردازش:\n{e}")
        job.status = "error"
        job.error_message = str(e)
        update_job(job)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        # فقط اگر job تموم شده باشه (منتظر کاربر نیست) session ریست بشه
        final_job = find_job_by_id(job.job_id)
        if final_job and final_job.status in ("done", "error", "cancelled"):
            session = TackServerDB.load_session(chat_id) or SessionState(chat_id=chat_id)
            session.state = "idle"
            session.current_job_id = None
            session.cancel_requested = False
            TackServerDB.save_session(session)
            send_message(chat_id, "🔄 پردازش به پایان رسید.", reply_markup=main_menu_keyboard())
# ════════════════════════════════════
# توابع دانلود و مرورگر
# ════════════════════════════════════
def handle_download(job: Job, job_dir: str):
    chat_id = job.chat_id
    url = job.url

    # ۱. لینک مستقیم؟
    if is_direct_file_url(url):
        direct_link = url
    else:
        send_message(chat_id, "🔎 لینک مستقیم نیست. در حال جستجوی فایل در صفحه...")
        direct_link = crawl_for_download_link(url)
        if not direct_link:
            send_message(chat_id, "❌ هیچ فایل قابل دانلودی پیدا نشد.")
            job.status = "error"
            job.error_message = "No downloadable file found"
            update_job(job)
            return

    # ۲. اطلاعات فایل
    try:
        head = requests.head(direct_link, timeout=15, allow_redirects=True)
        content_type = head.headers.get("Content-Type", "unknown")
        content_length = head.headers.get("Content-Length")
        size_str = f"{int(content_length)/(1024*1024):.2f} MB" if content_length else "نامشخص"
    except:
        content_type = "unknown"
        size_str = "نامشخص"

    filename = get_filename_from_url(direct_link)

    info_text = (
        f"📄 **فایل پیدا شد:**\n"
        f"نام: `{filename}`\n"
        f"نوع: `{content_type}`\n"
        f"حجم: `{size_str}`\n\n"
        f"چگونه دانلود شود؟"
    )

    kb = {
        "inline_keyboard": [
            [
                {"text": "📦 دانلود ZIP", "callback_data": f"dlzip_{job.job_id}"},
                {"text": "📄 دانلود اصلی", "callback_data": f"dlraw_{job.job_id}"}
            ],
            [{"text": "❌ لغو دانلود", "callback_data": f"canceljob_{job.job_id}"}]
        ]
    }
    send_message(chat_id, info_text, reply_markup=kb)

    # job رو در حالت انتظار کاربر بذار
    job.status = "awaiting_user"
    job.extra = {"direct_link": direct_link, "filename": filename}
    update_job(job)


def execute_download(job: Job, job_dir: str):
    chat_id = job.chat_id
    extra = job.extra or {}
    url = extra["direct_link"]
    filename = extra["filename"]
    pack_as_zip = extra.get("pack_zip", False)

    file_path = os.path.join(job_dir, filename)
    send_message(chat_id, "⏳ در حال دانلود فایل...")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(file_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

    if pack_as_zip:
        send_message(chat_id, "📦 در حال فشرده‌سازی و ارسال...")
        parts = create_zip_and_split(file_path, filename)
        label = "ZIP"
    else:
        send_message(chat_id, "📤 در حال تقسیم و ارسال فایل اصلی...")
        base, ext = os.path.splitext(filename)
        parts = split_file_binary(file_path, base, ext)
        label = "اصلی"

    for idx, part_path in enumerate(parts, 1):
        cap = f"{label} - پارت {idx} از {len(parts)}"
        send_document(chat_id, part_path, caption=cap)

    job.status = "done"
    update_job(job)


def handle_browser(job: Job, job_dir: str):
    chat_id = job.chat_id
    url = job.url
    context = get_or_create_context(chat_id)
    page = context.new_page()
    try:
        page.goto(url, timeout=90000, wait_until="networkidle")
        page.wait_for_timeout(2000)
        screenshot_path = os.path.join(job_dir, "browser.png")
        page.screenshot(path=screenshot_path, full_page=True)
        links = extract_clickable_links(page)

        if links:
            kb = page_actions_keyboard(links, chat_id)
            kb["inline_keyboard"].append(
                [{"text": "❌ بستن مرورگر", "callback_data": f"closebrowser_{chat_id}"}]
            )
            caption = f"🌐 {url}\n\nبرای حرکت روی لینک‌ها کلیک کن:"
        else:
            kb = main_menu_keyboard()
            caption = f"🌐 {url}\n\nهیچ لینکی یافت نشد."

        send_photo(chat_id, screenshot_path, caption=caption, reply_markup=kb)
        job.status = "done"
        update_job(job)
    finally:
        page.close()


def handle_browser_click(job: Job, job_dir: str, session: SessionState):
    """کلیک روی لینک در مرورگر"""
    handle_browser(job, job_dir)


# ════════════════════════════════════
# مدیریت Callbackها
# ════════════════════════════════════
def handle_callback_query(callback_query: Dict[str, Any]):
    cq_id = callback_query["id"]
    message = callback_query.get("message")
    data = callback_query.get("data", "")
    if not message:
        answer_callback_query(cq_id, "پیام نامعتبر")
        return
    chat_id = message["chat"]["id"]
    session = TackServerDB.load_session(chat_id) or SessionState(chat_id=chat_id)

    # منوی اصلی
    if data == "menu_screenshot":
        session.state = "waiting_url_screenshot"
        TackServerDB.save_session(session)
        answer_callback_query(cq_id, "لینک سایت را بفرستید")
        send_message(chat_id, "📸 لطفاً URL سایت مورد نظر را ارسال کنید:")

    elif data == "menu_download":
        session.state = "waiting_url_download"
        TackServerDB.save_session(session)
        answer_callback_query(cq_id, "لینک سایت / فایل را بفرستید")
        send_message(chat_id, "📥 لطفاً URL را بفرستید:")

    elif data == "menu_browser":
        session.state = "waiting_url_browser"
        TackServerDB.save_session(session)
        answer_callback_query(cq_id, "لینک را وارد کنید")
        send_message(chat_id, "🧭 آدرس سایت را بفرستید:")

    elif data == "menu_cancel":
        session.state = "idle"
        session.cancel_requested = True
        session.current_job_id = None
        TackServerDB.save_session(session)
        close_user_context(chat_id)
        answer_callback_query(cq_id, "وضعیت ریست شد")
        send_message(chat_id, "✅ عملیات لغو شد.", reply_markup=main_menu_keyboard())

    # اسکرین‌شات 4K
    elif data.startswith("req4k_"):
        job_id = data[6:]
        job = find_job_by_id(job_id)
        if job and job.status == "done":
            new_job = Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="4k_screenshot", url=job.url)
            enqueue_job(new_job)
            answer_callback_query(cq_id, "درخواست 4K ثبت شد")
            send_message(chat_id, "🖼️ درخواست اسکرین‌شات 4K ثبت شد...")
        else:
            answer_callback_query(cq_id, "امکان درخواست 4K وجود ندارد")

    # دانلود ZIP
    elif data.startswith("dlzip_"):
        job_id = data[6:]
        job = find_job_by_id(job_id)
        if job and job.status == "awaiting_user" and job.extra:
            new_job = Job(
                job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download_execute", url=job.url,
                extra={"direct_link": job.extra["direct_link"], "filename": job.extra["filename"], "pack_zip": True}
            )
            enqueue_job(new_job)
            # job اصلی رو ببند
            job.status = "done"
            update_job(job)
            answer_callback_query(cq_id, "دانلود ZIP آغاز شد")
            send_message(chat_id, "📦 دانلود ZIP آغاز می‌شود...")
        else:
            answer_callback_query(cq_id, "گزینه منقضی شده")

    # دانلود اصلی
    elif data.startswith("dlraw_"):
        job_id = data[6:]
        job = find_job_by_id(job_id)
        if job and job.status == "awaiting_user" and job.extra:
            new_job = Job(
                job_id=str(uuid.uuid4()), chat_id=chat_id, mode="download_execute", url=job.url,
                extra={"direct_link": job.extra["direct_link"], "filename": job.extra["filename"], "pack_zip": False}
            )
            enqueue_job(new_job)
            job.status = "done"
            update_job(job)
            answer_callback_query(cq_id, "دانلود اصلی آغاز شد")
            send_message(chat_id, "📄 دانلود فرمت اصلی آغاز می‌شود...")
        else:
            answer_callback_query(cq_id, "گزینه منقضی شده")

    # لغو دانلود
    elif data.startswith("canceljob_"):
        job_id = data[10:]
        job = find_job_by_id(job_id)
        if job:
            job.status = "cancelled"
            update_job(job)
        answer_callback_query(cq_id, "دانلود لغو شد")
        send_message(chat_id, "❌ دانلود لغو شد.", reply_markup=main_menu_keyboard())

    # ناوبری مرورگر
    elif data.startswith("nav_"):
        parts = data.split("_", 2)
        if len(parts) >= 3:
            cb_id = f"{parts[0]}_{parts[1]}_{parts[2]}"
            with callback_map_lock:
                url = callback_map.pop(cb_id, None)
            if url:
                new_job = Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode="browser_click", url=url)
                enqueue_job(new_job)
                answer_callback_query(cq_id, "در حال بارگذاری...")
            else:
                answer_callback_query(cq_id, "لینک منقضی شده")

    # بستن مرورگر
    elif data.startswith("closebrowser_"):
        close_user_context(chat_id)
        answer_callback_query(cq_id, "مرورگر بسته شد")
        send_message(chat_id, "🧭 مرورگر بسته شد.", reply_markup=main_menu_keyboard())

    else:
        answer_callback_query(cq_id, "دستور ناشناخته")


# ════════════════════════════════════
# مدیریت پیام‌های متنی
# ════════════════════════════════════
def handle_text_message(chat_id: int, text: str):
    text = text.strip()
    session = TackServerDB.load_session(chat_id) or SessionState(chat_id=chat_id)

    if text == "/start":
        session.state = "idle"
        session.cancel_requested = False
        session.current_job_id = None
        TackServerDB.save_session(session)
        send_message(chat_id, "سلام 👋 به ربات مرورگر/دانلودر خوش آمدید!", reply_markup=main_menu_keyboard())
        return

    if text == "/cancel":
        session.state = "idle"
        session.cancel_requested = True
        session.current_job_id = None
        TackServerDB.save_session(session)
        close_user_context(chat_id)
        send_message(chat_id, "⏹️ تمام عملیات‌ها لغو و وضعیت ریست شد.", reply_markup=main_menu_keyboard())
        return

    if session.state.startswith("waiting_url_"):
        url = text
        if not (url.startswith("http://") or url.startswith("https://")):
            send_message(chat_id, "❌ لطفاً یک URL معتبر وارد کنید.")
            return

        mode_map = {
            "waiting_url_screenshot": "screenshot",
            "waiting_url_download": "download",
            "waiting_url_browser": "browser",
        }
        mode = mode_map.get(session.state, "screenshot")

        job = Job(job_id=str(uuid.uuid4()), chat_id=chat_id, mode=mode, url=url)
        enqueue_job(job)

        session.state = "idle"
        session.current_job_id = job.job_id
        TackServerDB.save_session(session)

        idle_worker = find_idle_worker()
        if idle_worker:
            send_message(chat_id, "✅ درخواست ثبت شد و به‌زودی پردازش می‌شود.")
        else:
            pos = get_job_queue_position(job.job_id) or "؟"
            send_message(chat_id, f"✅ درخواست در صف قرار گرفت.\n🎫 نوبت شما: {pos}")
        return

    send_message(chat_id, "لطفاً از منوی زیر استفاده کنید:", reply_markup=main_menu_keyboard())


# ════════════════════════════════════
# حلقهٔ اصلی Polling و Main
# ════════════════════════════════════
def polling_loop(stop_event: threading.Event):
    offset = None
    safe_print("[Polling] شروع دریافت به‌روزرسانی‌ها")
    while not stop_event.is_set():
        try:
            updates = get_updates(offset=offset, timeout=LONG_POLL_TIMEOUT)
        except Exception as e:
            safe_print(f"[Polling] خطا: {e}")
            time.sleep(5)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                if "message" in upd:
                    msg = upd["message"]
                    chat_id = msg["chat"]["id"]
                    if "text" in msg:
                        handle_text_message(chat_id, msg["text"])
                elif "callback_query" in upd:
                    handle_callback_query(upd["callback_query"])
            except Exception as e:
                safe_print(f"[Polling] خطا: {e}")
                traceback.print_exc()
    safe_print("[Polling] متوقف شد")


def ensure_dirs():
    os.makedirs("jobs_data", exist_ok=True)


def main():
    ensure_dirs()
    stop_event = threading.Event()

    worker_threads = []
    for i in range(WORKER_COUNT):
        t = threading.Thread(target=worker_loop, args=(i, stop_event), daemon=True)
        t.start()
        worker_threads.append(t)

    poll_thread = threading.Thread(target=polling_loop, args=(stop_event,), daemon=True)
    poll_thread.start()

    safe_print("✅ ربات اجرا شد. برای توقف Ctrl+C بزنید.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        safe_print("در حال توقف...")
        stop_event.set()
        time.sleep(2)


if __name__ == "__main__":
    main()
