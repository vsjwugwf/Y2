#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import time
import math
import queue
import shutil
import zipfile
import traceback
import threading
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional, List

import requests
from playwright.sync_api import sync_playwright

# ==========================
# تنظیمات پایه
# ==========================

BALE_BOT_TOKEN = os.getenv("BALE_BOT_TOKEN", "").strip()
if not BALE_BOT_TOKEN:
    print("ERROR: متغیر محیطی BALE_BOT_TOKEN تنظیم نشده است.", file=sys.stderr)
    sys.exit(1)

BALE_API_URL = "https://tapi.bale.ai/bot" + BALE_BOT_TOKEN

# برای جلوگیری از کرش در صورت قطع موقت
REQUEST_TIMEOUT = 30
LONG_POLL_TIMEOUT = 50

# تعداد ورکرها
WORKER_COUNT = 3

# حداکثر حجم هر پارت ZIP که به بله ارسال می‌کنیم (بر حسب بایت)
# 19.5 مگابایت
ZIP_PART_SIZE = int(19.5 * 1024 * 1024)

# نام فایل‌های ذخیره‌سازی
SESSIONS_FILE = "sessions.json"
QUEUE_FILE = "queue.json"
WORKERS_FILE = "workers.json"

# قفل‌ها برای دسترسی همزمان ایمن
sessions_lock = threading.Lock()
queue_lock = threading.Lock()
workers_lock = threading.Lock()
print_lock = threading.Lock()


def safe_print(*args, **kwargs):
    with print_lock:
        print(*args, **kwargs, flush=True)


# ==========================
# کلاس‌های داده
# ==========================

@dataclass
class SessionState:
    chat_id: int
    state: str = "idle"           # idle, waiting_url_screenshot, waiting_url_download, waiting_url_browser
    current_job_id: Optional[str] = None
    browser_session_id: Optional[str] = None
    last_interaction: float = time.time()


@dataclass
class Job:
    job_id: str
    chat_id: int
    mode: str           # "screenshot", "download", "browser"
    url: str
    status: str = "queued"   # queued, running, done, error, cancelled
    created_at: float = time.time()
    updated_at: float = time.time()
    error_message: Optional[str] = None


@dataclass
class WorkerInfo:
    worker_id: int
    current_job_id: Optional[str] = None
    status: str = "idle"     # idle, busy


# ==========================
# ابزارهای فایل JSON
# ==========================

def load_json_file(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        safe_print(f"ERROR loading {path}: {e}")
        return default


def save_json_file(path: str, data):
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        safe_print(f"ERROR saving {path}: {e}")


# ==========================
# مدیریت Session ها
# ==========================

def load_sessions() -> Dict[str, Any]:
    with sessions_lock:
        data = load_json_file(SESSIONS_FILE, {})
        return data


def save_sessions(sessions: Dict[str, Any]):
    with sessions_lock:
        save_json_file(SESSIONS_FILE, sessions)


def get_or_create_session(chat_id: int) -> SessionState:
    sessions = load_sessions()
    key = str(chat_id)
    if key in sessions:
        d = sessions[key]
        return SessionState(
            chat_id=chat_id,
            state=d.get("state", "idle"),
            current_job_id=d.get("current_job_id"),
            browser_session_id=d.get("browser_session_id"),
            last_interaction=d.get("last_interaction", time.time()),
        )
    else:
        session = SessionState(chat_id=chat_id)
        sessions[key] = asdict(session)
        save_sessions(sessions)
        return session


def update_session(session: SessionState):
    sessions = load_sessions()
    key = str(session.chat_id)
    session.last_interaction = time.time()
    sessions[key] = asdict(session)
    save_sessions(sessions)


# ==========================
# مدیریت صف Jobها
# ==========================

def load_queue() -> Dict[str, Any]:
    with queue_lock:
        data = load_json_file(QUEUE_FILE, {"jobs": []})
        if "jobs" not in data:
            data["jobs"] = []
        return data


def save_queue(q: Dict[str, Any]):
    with queue_lock:
        save_json_file(QUEUE_FILE, q)


def enqueue_job(job: Job):
    q = load_queue()
    q["jobs"].append(asdict(job))
    save_queue(q)


def find_job(job_id: str) -> Optional[Job]:
    q = load_queue()
    for j in q["jobs"]:
        if j["job_id"] == job_id:
            return Job(**j)
    return None


def update_job(job: Job):
    q = load_queue()
    found = False
    for idx, j in enumerate(q["jobs"]):
        if j["job_id"] == job.job_id:
            q["jobs"][idx] = asdict(job)
            found = True
            break
    if not found:
        q["jobs"].append(asdict(job))
    save_queue(q)


def pop_next_queued_job() -> Optional[Job]:
    q = load_queue()
    for idx, j in enumerate(q["jobs"]):
        if j["status"] == "queued":
            job = Job(**j)
            q["jobs"][idx]["status"] = "running"
            q["jobs"][idx]["updated_at"] = time.time()
            save_queue(q)
            return job
    return None


def get_job_queue_position(job_id: str) -> Optional[int]:
    q = load_queue()
    position = 1
    for j in q["jobs"]:
        if j["status"] == "queued":
            if j["job_id"] == job_id:
                return position
            position += 1
    return None


# ==========================
# مدیریت Workerها
# ==========================

def load_workers() -> Dict[str, Any]:
    with workers_lock:
        data = load_json_file(WORKERS_FILE, {"workers": []})
        if "workers" not in data or len(data["workers"]) != WORKER_COUNT:
            # initialize
            data["workers"] = []
            for i in range(WORKER_COUNT):
                w = WorkerInfo(worker_id=i, current_job_id=None, status="idle")
                data["workers"].append(asdict(w))
            save_json_file(WORKERS_FILE, data)
        return data


def save_workers(data: Dict[str, Any]):
    with workers_lock:
        save_json_file(WORKERS_FILE, data)


def find_idle_worker() -> Optional[WorkerInfo]:
    data = load_workers()
    for w in data["workers"]:
        if w["status"] == "idle":
            return WorkerInfo(**w)
    return None


def set_worker_busy(worker_id: int, job_id: str):
    data = load_workers()
    for idx, w in enumerate(data["workers"]):
        if w["worker_id"] == worker_id:
            data["workers"][idx]["status"] = "busy"
            data["workers"][idx]["current_job_id"] = job_id
            break
    save_workers(data)


def set_worker_idle(worker_id: int):
    data = load_workers()
    for idx, w in enumerate(data["workers"]):
        if w["worker_id"] == worker_id:
            data["workers"][idx]["status"] = "idle"
            data["workers"][idx]["current_job_id"] = None
            break
    save_workers(data)


# ==========================
# توابع کمکی Bot API بله
# ==========================

def bale_request(method: str, params: Optional[Dict[str, Any]] = None, files=None) -> Any:
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


def send_message(chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None):
    params = {"chat_id": chat_id, "text": text}
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup)
    return bale_request("sendMessage", params=params)


def send_document(chat_id: int, file_path: str, caption: str = ""):
    with open(file_path, "rb") as f:
        files = {
            "document": (os.path.basename(file_path), f)
        }
        params = {"chat_id": chat_id}
        if caption:
            params["caption"] = caption
        return bale_request("sendDocument", params=params, files=files)


def send_photo(chat_id: int, file_path: str, caption: str = ""):
    with open(file_path, "rb") as f:
        files = {
            "photo": (os.path.basename(file_path), f)
        }
        params = {"chat_id": chat_id}
        if caption:
            params["caption"] = caption
        return bale_request("sendPhoto", params=params, files=files)


def answer_callback_query(callback_query_id: str, text: str = "", show_alert: bool = False):
    params = {"callback_query_id": callback_query_id}
    if text:
        params["text"] = text
    if show_alert:
        params["show_alert"] = True
    return bale_request("answerCallbackQuery", params=params)


def get_updates(offset: Optional[int] = None, timeout: int = LONG_POLL_TIMEOUT) -> List[Dict[str, Any]]:
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    result = bale_request("getUpdates", params=params)
    if result is None:
        return []
    return result


# ==========================
# Inline Keyboard (منوی شیشه‌ای)
# ==========================

def main_menu_keyboard() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "🧭 مرورگر من", "callback_data": "menu_browser"},
            ],
            [
                {"text": "📸 اسکرین‌شات از سایت", "callback_data": "menu_screenshot"},
            ],
            [
                {"text": "📥 دانلود محتوای سایت", "callback_data": "menu_download"},
            ],
            [
                {"text": "❌ لغو پردازش", "callback_data": "menu_cancel"},
            ],
        ]
    }


# ==========================
# Playwright – ابزارهای اصلی
# ==========================

def setup_browser():
    """
    راه‌اندازی Playwright و کروم‌لِس (یا کرومیوم).
    در GitHub Actions نیاز است قبلش playwright install در workflow اجرا شود.
    """
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-zygote",
            "--disable-web-security",
        ]
    )
    return playwright, browser


def close_browser(playwright, browser):
    try:
        browser.close()
    except Exception:
        pass
    try:
        playwright.stop()
    except Exception:
        pass


def take_fullpage_screenshot(url: str, out_path: str):
    playwright, browser = setup_browser()
    try:
        page = browser.new_page(
            viewport={"width": 1280, "height": 720},
        )
        page.goto(url, timeout=90000, wait_until="networkidle")
        # کمی اسکرول برای لود lazy content
        page.wait_for_timeout(3000)
        page.screenshot(path=out_path, full_page=True)
    finally:
        close_browser(playwright, browser)


def crawl_site_and_zip(url: str, out_zip_path: str, max_depth: int = 1, max_pages: int = 30):
    """
    دانلود ساده‌ی سایت (HTML + منابع استاتیک) تا عمق مشخص.
    اینجا یک کراولر خیلی ساده می‌نویسیم که برای بیشتر سایت‌ها کفایت کند.
    توجه: این یک دانلودر کامل HTTrack نیست، ولی برای نیاز پروژه کفایت می‌کند.
    """
    from urllib.parse import urlparse, urljoin
    from bs4 import BeautifulSoup  # باید در requirements نصب شود

    # dir موقت
    work_dir = "site_tmp"
    if os.path.exists(work_dir):
        shutil.rmtree(work_dir)
    os.makedirs(work_dir, exist_ok=True)

    visited = set()
    to_visit = queue.Queue()
    to_visit.put((url, 0))

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    })

    parsed_root = urlparse(url)
    root_netloc = parsed_root.netloc

    page_count = 0

    def save_binary(resource_url: str, base_dir: str):
        try:
            r = session.get(resource_url, timeout=30)
            if r.status_code != 200:
                return
            rel_path = resource_url.split("://", 1)[-1]
            # جلوگیری از خیلی طولانی شدن
            rel_path = rel_path.replace("?", "_").replace("&", "_")
            if len(rel_path) > 180:
                rel_path = rel_path[:180]
            full_path = os.path.join(base_dir, rel_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "wb") as f:
                f.write(r.content)
        except Exception as e:
            safe_print(f"save_binary error {resource_url}: {e}")

    while not to_visit.empty():
        url_cur, depth = to_visit.get()
        if url_cur in visited:
            continue
        if depth > max_depth:
            continue
        visited.add(url_cur)
        page_count += 1
        if page_count > max_pages:
            break

        safe_print(f"Crawling {url_cur} (depth={depth})")
        try:
            resp = session.get(url_cur, timeout=40)
        except Exception as e:
            safe_print(f"Error fetching {url_cur}: {e}")
            continue

        if resp.status_code != 200:
            continue

        # ذخیره HTML
        rel = url_cur.replace("://", "_").replace("/", "_").replace("?", "_").replace("&", "_")
        if len(rel) > 150:
            rel = rel[:150]
        html_name = f"{rel}.html"
        html_path = os.path.join(work_dir, html_name)
        with open(html_path, "wb") as f:
            f.write(resp.content)

        # استخراج لینک‌ها و منابع
        content_type = resp.headers.get("Content-Type", "")
        if "html" not in content_type:
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        # لینک‌ها برای ادامه‌ی کراول
        for a in soup.find_all("a", href=True):
            href = a["href"]
            link = urljoin(url_cur, href)
            p = urlparse(link)
            if p.netloc == root_netloc and link not in visited:
                if depth + 1 <= max_depth:
                    to_visit.put((link, depth + 1))

        # منابع: img, script, link rel=stylesheet
        for img in soup.find_all("img", src=True):
            src = urljoin(url_cur, img["src"])
            save_binary(src, work_dir)
        for s in soup.find_all("script", src=True):
            src = urljoin(url_cur, s["src"])
            save_binary(src, work_dir)
        for l in soup.find_all("link", href=True):
            rel_attr = (l.get("rel") or [""])[0]
            if "stylesheet" in rel_attr or l["href"].endswith(".css"):
                src = urljoin(url_cur, l["href"])
                save_binary(src, work_dir)

    # ساخت ZIP
    with zipfile.ZipFile(out_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(work_dir):
            for file in files:
                full = os.path.join(root, file)
                arcname = os.path.relpath(full, work_dir)
                zf.write(full, arcname)

    # پاکسازی پوشه‌ی موقت
    shutil.rmtree(work_dir, ignore_errors=True)


# ==========================
# تقسیم فایل ZIP به پارت
# ==========================

def split_zip_if_needed(zip_path: str, base_name: str) -> List[str]:
    """
    اگر zip_path بزرگ‌تر از ZIP_PART_SIZE باشد، آن را به پارت‌های site_partXX.zip تقسیم می‌کند.
    خروجی: لیست مسیر فایل‌ها (پارت‌ها یا همان خود ZIP اگر کوچک بود).
    """
    size = os.path.getsize(zip_path)
    if size <= ZIP_PART_SIZE:
        return [zip_path]

    dir_name = os.path.dirname(zip_path) or "."
    part_paths = []

    with open(zip_path, "rb") as f:
        part_index = 1
        while True:
            chunk = f.read(ZIP_PART_SIZE)
            if not chunk:
                break
            part_name = f"{base_name}_part{part_index:02d}.zip"
            part_path = os.path.join(dir_name, part_name)
            with open(part_path, "wb") as pf:
                pf.write(chunk)
            part_paths.append(part_path)
            part_index += 1

    return part_paths


# ==========================
# Worker – پردازش Jobها با Playwright
# ==========================

def worker_loop(worker_id: int, stop_event: threading.Event):
    safe_print(f"[Worker {worker_id}] started")
    while not stop_event.is_set():
        # پیدا کردن Job در حال ران که به این ورکر assign شده
        idle_worker = find_idle_worker()
        if idle_worker and idle_worker.worker_id == worker_id:
            # سعی کن یک job جدید از صف برداری
            job = pop_next_queued_job()
            if job is None:
                # هیچ job در صف نیست
                time.sleep(2)
                continue
            # این ورکر را busy کن
            set_worker_busy(worker_id, job.job_id)
            safe_print(f"[Worker {worker_id}] picked job {job.job_id}")
            try:
                process_job(worker_id, job)
            except Exception as e:
                safe_print(f"[Worker {worker_id}] ERROR processing job {job.job_id}: {e}")
                traceback.print_exc()
                job.status = "error"
                job.error_message = str(e)
                job.updated_at = time.time()
                update_job(job)
            finally:
                set_worker_idle(worker_id)
        else:
            # این ورکر در فایل workers به عنوان busy ثبت شده یا داده هنوز sync نشده
            time.sleep(2)

    safe_print(f"[Worker {worker_id}] stopped")


def process_job(worker_id: int, job: Job):
    """
    اجرای واقعی Job بر اساس mode:
    - screenshot: اسکرین‌شات فول پیج
    - download: دانلود سایت و zip
    - browser: فعلاً شبیه screenshot (فاز ۱)
    """
    chat_id = job.chat_id
    mode = job.mode
    url = job.url

    send_message(chat_id, f"🔄 شروع پردازش ({mode}) برای URL:\n{url}")

    # پوشه‌ی کاری اختصاصی برای هر job
    job_dir = os.path.join("jobs_data", job.job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        if mode == "screenshot" or mode == "browser":
            # Screenshot
            screenshot_path = os.path.join(job_dir, "screenshot.png")
            take_fullpage_screenshot(url, screenshot_path)
            send_photo(chat_id, screenshot_path, caption=f"✅ اسکرین‌شات از:\n{url}")
            job.status = "done"
            job.updated_at = time.time()
            update_job(job)

        elif mode == "download":
            # Download site
            zip_path = os.path.join(job_dir, "site.zip")
            crawl_site_and_zip(url, zip_path, max_depth=1, max_pages=30)
            base_name = "site"
            parts = split_zip_if_needed(zip_path, base_name)

            if len(parts) == 1:
                send_document(chat_id, parts[0], caption=f"✅ فایل ZIP سایت:\n{url}")
            else:
                send_message(chat_id, f"📦 سایت به {len(parts)} پارت تقسیم شد (حدوداً ۱۹.۵ مگابایت هر پارت).")
                for idx, p in enumerate(parts, start=1):
                    cap = f"پارت {idx} از {len(parts)} – محتواى سایت:\n{url}"
                    send_document(chat_id, p, caption=cap)

            job.status = "done"
            job.updated_at = time.time()
            update_job(job)

        else:
            send_message(chat_id, "❌ حالت ناشناخته برای job.")
            job.status = "error"
            job.error_message = "unknown mode"
            job.updated_at = time.time()
            update_job(job)

    except Exception as e:
        safe_print(f"[Worker {worker_id}] exception: {e}")
        traceback.print_exc()
        send_message(chat_id, f"❌ خطا در پردازش:\n{e}")
        job.status = "error"
        job.error_message = str(e)
        job.updated_at = time.time()
        update_job(job)

    finally:
        # پاک کردن پوشه job اگر خواستی
        shutil.rmtree(job_dir, ignore_errors=True)
        send_message(chat_id, "✅ پردازش به پایان رسید. برای شروع دوباره، /start را بزنید یا از منوی شیشه‌ای استفاده کن.")


# ==========================
# State Machine – مدیریت مکالمه
# ==========================

def handle_start_command(chat_id: int):
    session = get_or_create_session(chat_id)
    session.state = "idle"
    session.current_job_id = None
    update_session(session)

    text = (
        "سلام 👋\n"
        "من ربات «مرورگر/دانلودر سایت» هستم.\n\n"
        "از منوی شیشه‌ای زیر می‌تونی این کارها رو انجام بدی:\n"
        "🧭 مرورگر من (فعلاً اسکرین‌شات از URL دلخواه)\n"
        "📸 اسکرین‌شات از سایت\n"
        "📥 دانلود محتوای سایت (HTML/CSS/JS/تصاویر در قالب ZIP)\n"
        "❌ لغو پردازش\n"
    )
    send_message(chat_id, text, reply_markup=main_menu_keyboard())


def create_job_id(chat_id: int) -> str:
    return f"{chat_id}_{int(time.time()*1000)}"


def queue_new_job(chat_id: int, mode: str, url: str) -> Job:
    job_id = create_job_id(chat_id)
    job = Job(job_id=job_id, chat_id=chat_id, mode=mode, url=url)
    enqueue_job(job)
    return job


def check_and_assign_worker_for_new_job(job: Job):
    # آیا worker خالی داریم؟
    idle = find_idle_worker()
    if idle:
        # job در pop_next_queued_job انتخاب می‌شود، اینجا فقط نوتیف بده
        pos = get_job_queue_position(job.job_id)
        if pos is None or pos == 1:
            send_message(job.chat_id, "✅ درخواستت در صف ثبت شد و به‌زودی توسط یکی از ورکرها پردازش می‌شود.")
        else:
            send_message(job.chat_id, f"✅ درخواستت در صف ثبت شد.\n🎫 نوبت فعلی شما: {pos}")
    else:
        pos = get_job_queue_position(job.job_id)
        if pos is None:
            pos = "؟"
        send_message(job.chat_id, f"✅ درخواستت در صف ثبت شد.\n"
                                  f"در حال حاضر {WORKER_COUNT} پردازش در حال اجراست.\n"
                                  f"🎫 نوبت فعلی شما: {pos}")


def handle_text_message(chat_id: int, text: str):
    session = get_or_create_session(chat_id)

    # اگر پیام /start است
    if text.strip() == "/start":
        handle_start_command(chat_id)
        return

    # بر اساس state رفتار کنیم
    if session.state in ["waiting_url_screenshot", "waiting_url_download", "waiting_url_browser"]:
        url = text.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            send_message(chat_id, "❌ لطفاً یک URL معتبر شروع‌شده با http:// یا https:// ارسال کن.")
            return

        if session.state == "waiting_url_screenshot":
            mode = "screenshot"
        elif session.state == "waiting_url_download":
            mode = "download"
        else:
            mode = "browser"

        job = queue_new_job(chat_id, mode, url)
        session.current_job_id = job.job_id
        session.state = "idle"
        update_session(session)

        check_and_assign_worker_for_new_job(job)
    else:
        # در حالت idle، اگر متن دلخواه فرستاد، منو را یادآوری کن
        send_message(chat_id, "برای استفاده از ربات از منوی زیر استفاده کن:", reply_markup=main_menu_keyboard())


def handle_callback_query(callback_query: Dict[str, Any]):
    cq_id = callback_query["id"]
    message = callback_query.get("message")
    data = callback_query.get("data", "")
    if not message:
        answer_callback_query(cq_id, "پیام نامعتبر.")
        return
    chat_id = message["chat"]["id"]

    session = get_or_create_session(chat_id)

    if data == "menu_screenshot":
        session.state = "waiting_url_screenshot"
        update_session(session)
        answer_callback_query(cq_id, "لینک سایتی که می‌خوای ازش اسکرین‌شات گرفته بشه رو بفرست.")
        send_message(chat_id, "📸 لطفاً URL سایتی که می‌خوای ازش اسکرین‌شات گرفته بشه رو بفرست:")

    elif data == "menu_download":
        session.state = "waiting_url_download"
        update_session(session)
        answer_callback_query(cq_id, "لینک سایتی که می‌خوای دانلود بشه رو بفرست.")
        send_message(chat_id, "📥 لطفاً URL سایتی که می‌خوای محتوایش دانلود شود را بفرست:")

    elif data == "menu_browser":
        session.state = "waiting_url_browser"
        update_session(session)
        answer_callback_query(cq_id, "لینک سایتی که می‌خوای باز بشه رو بفرست.")
        send_message(chat_id, "🧭 لطفاً URL سایتی که می‌خوای در «مرورگر من» باز شود را بفرست.\n"
                              "در این نسخه، نتیجه به صورت اسکرین‌شات فول‌پیج برات ارسال می‌شود.")

    elif data == "menu_cancel":
        # این نسخه فقط سشن را reset می‌کند. اگر خواستی می‌تونی job جاری را هم لغو کنی.
        session.state = "idle"
        session.current_job_id = None
        update_session(session)
        answer_callback_query(cq_id, "وضعیت فعلی پاک شد.")
        send_message(chat_id, "✅ وضعیت فعلی پاک شد. از منوی شیشه‌ای، گزینه‌ی جدید انتخاب کن.", reply_markup=main_menu_keyboard())

    else:
        answer_callback_query(cq_id, "دستور ناشناخته.")
        send_message(chat_id, "❌ دستور ناشناخته.", reply_markup=main_menu_keyboard())


# ==========================
# حلقه‌ی اصلی Polling
# ==========================

def polling_loop(stop_event: threading.Event):
    offset = None
    safe_print("[Polling] started")
    while not stop_event.is_set():
        try:
            updates = get_updates(offset=offset, timeout=LONG_POLL_TIMEOUT)
        except Exception as e:
            safe_print(f"[Polling] get_updates error: {e}")
            time.sleep(5)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                if "message" in upd:
                    msg = upd["message"]
                    chat_id = msg["chat"]["id"]
                    if "text" in msg:
                        text = msg["text"]
                        handle_text_message(chat_id, text)
                elif "callback_query" in upd:
                    handle_callback_query(upd["callback_query"])
            except Exception as e:
                safe_print(f"[Polling] error handling update: {e}")
                traceback.print_exc()

    safe_print("[Polling] stopped")


def ensure_dirs():
    os.makedirs("jobs_data", exist_ok=True)


def main():
    ensure_dirs()

    stop_event = threading.Event()

    # راه‌اندازی ورکرها
    worker_threads = []
    for i in range(WORKER_COUNT):
        t = threading.Thread(target=worker_loop, args=(i, stop_event), daemon=True)
        t.start()
        worker_threads.append(t)

    # راه‌اندازی polling
    polling_thread = threading.Thread(target=polling_loop, args=(stop_event,), daemon=True)
    polling_thread.start()

    safe_print("Bot started. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        safe_print("Stopping...")
        stop_event.set()
        time.sleep(2)


if __name__ == "__main__":
    main()
