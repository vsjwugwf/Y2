#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import uuid
import math
import logging
import threading
from typing import Dict, Any, List, Optional

import requests

# ========== تنظیمات عمومی ==========
BALE_BOT_TOKEN = os.getenv("BALE_BOT_TOKEN", "").strip()
if not BALE_BOT_TOKEN:
    raise RuntimeError("متغیر محیطی BALE_BOT_TOKEN تنظیم نشده است.")

BALE_API_BASE = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}"

POLL_TIMEOUT = 25  # ثانیه برای long-polling
DATA_DIR = "."     # می‌تواند در GitHub Actions روی /tmp باشد

SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")
WORKERS_FILE = os.path.join(DATA_DIR, "workers.json")

MAX_WORKERS = 3
MAX_ZIP_SIZE_MB = 20.0
SPLIT_PART_SIZE_MB = 19.5
SPLIT_PART_SIZE_BYTES = int(SPLIT_PART_SIZE_MB * 1024 * 1024)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ========== توابع کمکی JSON (load/save) ==========

def load_json(path: str, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"خطا در خواندن {path}: {e}")
        return default


def save_json(path: str, data) -> None:
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        logging.error(f"خطا در نوشتن {path}: {e}")


# ========== مدیریت سشن‌ها (users / state machine) ==========

class SessionManager:
    def __init__(self, path: str):
        self.path = path
        self.sessions: Dict[str, Dict[str, Any]] = load_json(path, {})

    def get(self, user_id: str) -> Dict[str, Any]:
        if user_id not in self.sessions:
            self.sessions[user_id] = {
                "user_id": user_id,
                "state": "idle",        # idle, waiting_for_url, in_queue, processing, viewing_browser, cancelling
                "mode": None,           # screenshot, download, browser
                "last_url": None,
                "job_id": None,
                "status": None,
                "joined_at": time.time(),
            }
            self.save()
        return self.sessions[user_id]

    def update(self, user_id: str, **kwargs):
        sess = self.get(user_id)
        sess.update(kwargs)
        self.save()

    def save(self):
        save_json(self.path, self.sessions)


# ========== مدیریت صف ==========
class QueueManager:
    def __init__(self, path: str):
        self.path = path
        self.queue: List[Dict[str, Any]] = load_json(path, [])

    def add_job(self, job: Dict[str, Any]):
        self.queue.append(job)
        self.save()

    def pop_next(self) -> Optional[Dict[str, Any]]:
        if not self.queue:
            return None
        job = self.queue.pop(0)
        self.save()
        return job

    def remove_job(self, job_id: str):
        self.queue = [j for j in self.queue if j.get("job_id") != job_id]
        self.save()

    def get_position(self, job_id: str) -> Optional[int]:
        for idx, job in enumerate(self.queue, start=1):
            if job.get("job_id") == job_id:
                return idx
        return None

    def save(self):
        save_json(self.path, self.queue)


# ========== مدیریت ورکرها ==========
class WorkerManager:
    def __init__(self, path: str):
        self.path = path
        self.workers: List[Dict[str, Any]] = load_json(path, [])
        # در صورت خالی بودن، سه ورکر بساز
        if not self.workers:
            for i in range(MAX_WORKERS):
                self.workers.append({
                    "id": i + 1,
                    "status": "idle",  # idle, busy
                    "job_id": None,
                    "user_id": None,
                    "started_at": None,
                })
            self.save()

    def find_idle_worker(self) -> Optional[Dict[str, Any]]:
        for w in self.workers:
            if w["status"] == "idle":
                return w
        return None

    def set_worker_job(self, worker_id: int, job_id: str, user_id: str):
        for w in self.workers:
            if w["id"] == worker_id:
                w["status"] = "busy"
                w["job_id"] = job_id
                w["user_id"] = user_id
                w["started_at"] = time.time()
                break
        self.save()

    def release_worker(self, worker_id: int):
        for w in self.workers:
            if w["id"] == worker_id:
                w["status"] = "idle"
                w["job_id"] = None
                w["user_id"] = None
                w["started_at"] = None
                break
        self.save()

    def find_worker_by_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        for w in self.workers:
            if w["job_id"] == job_id:
                return w
        return None

    def save(self):
        save_json(self.path, self.workers)


# ========== توابع API بله ==========
def bale_request(method: str, params: Dict[str, Any] = None, files: Dict[str, Any] = None):
    url = f"{BALE_API_BASE}/{method}"
    try:
        if files:
            resp = requests.post(url, data=params or {}, files=files, timeout=60)
        else:
            resp = requests.post(url, json=params or {}, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok", True):  # بسته به ساختار واقعی پاسخ بله
            logging.error(f"Bale API error: {data}")
        return data
    except Exception as e:
        logging.error(f"خطا در درخواست بله ({method}): {e}")
        return None


def send_message(chat_id: int, text: str, reply_markup: Dict[str, Any] = None):
    params = {"chat_id": chat_id, "text": text}
    if reply_markup:
        params["reply_markup"] = reply_markup
    return bale_request("sendMessage", params)


def send_document(chat_id: int, file_path: str, caption: str = ""):
    params = {"chat_id": chat_id, "caption": caption}
    files = {
        "document": open(file_path, "rb")
    }
    try:
        return bale_request("sendDocument", params=params, files=files)
    finally:
        files["document"].close()


def get_updates(offset: Optional[int]):
    params = {
        "timeout": POLL_TIMEOUT,
    }
    if offset is not None:
        params["offset"] = offset
    url = f"{BALE_API_BASE}/getUpdates"
    try:
        resp = requests.get(url, params=params, timeout=POLL_TIMEOUT + 5)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", [])
    except Exception as e:
        logging.error(f"خطا در getUpdates: {e}")
        return []


# ========== منوی شیشه‌ای (Inline Keyboard) ==========
def main_menu_keyboard() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "🖥 مرورگر من", "callback_data": "mode_browser"},
            ],
            [
                {"text": "📸 اسکرین‌شات از سایت", "callback_data": "mode_screenshot"},
            ],
            [
                {"text": "📥 دانلود محتوای سایت", "callback_data": "mode_download"},
            ],
            [
                {"text": "❌ لغو پردازش", "callback_data": "cancel"},
            ],
        ]
    }


# ========== تقسیم فایل ZIP به پارت‌های ۱۹.۵MB ==========
def split_zip_if_needed(zip_path: str) -> List[str]:
    """
    اگر فایل زیپ بزرگ‌تر از ۲۰MB باشد، به پارت‌های ~۱۹.۵MB تقسیم می‌کند.
    خروجی: لیست مسیر فایل‌های پارت شده یا [zip_path] اگر تقسیم نشود.
    """
    size_bytes = os.path.getsize(zip_path)
    size_mb = size_bytes / (1024 * 1024)

    if size_mb <= MAX_ZIP_SIZE_MB:
        # نیاز به تقسیم نیست
        return [zip_path]

    part_paths: List[str] = []
    total_parts = math.ceil(size_bytes / SPLIT_PART_SIZE_BYTES)

    base_dir = os.path.dirname(zip_path)
    base_name = os.path.basename(zip_path)
    name_no_ext, _ = os.path.splitext(base_name)

    with open(zip_path, "rb") as f:
        part_index = 1
        while True:
            chunk = f.read(SPLIT_PART_SIZE_BYTES)
            if not chunk:
                break
            part_name = f"{name_no_ext}_part{part_index:02d}.zip"
            part_path = os.path.join(base_dir, part_name)
            with open(part_path, "wb") as pf:
                pf.write(chunk)
            part_paths.append(part_path)
            part_index += 1

    logging.info(f"فایل ZIP به {len(part_paths)} پارت تقسیم شد (کل پارت لازم: {total_parts}).")
    return part_paths


# ========== جایگاه Playwright ==========
# در مرحله بعدی این بخش را تکمیل می‌کنیم (اسکرین‌شات، دانلود کامل سایت، مرورگر من).
# فعلاً اسکلت آماده می‌گذاریم تا ساختار اصلی bot.py کامل باشد.

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logging.warning("Playwright نصب نشده است. بخش مرورگر/اسکرین‌شات فعلاً کار نخواهد کرد.")


def run_playwright_job(job: Dict[str, Any], mode: str, output_dir: str) -> Dict[str, Any]:
    """
    این تابع بعداً با منطق واقعی پر می‌شود:
    - اگر mode == 'screenshot' → اسکرین‌شات فول‌پیج
    - اگر mode == 'download' → دانلود HTML/CSS/JS/Images → ساخت ZIP
    - اگر mode == 'browser' → مرورگر من (کنترل مرحله‌ای)
    فعلاً یک اسکلت برمی‌گرداند.
    """
    result = {
        "success": False,
        "error": None,
        "zip_path": None,
        "screenshot_path": None,
    }

    if not PLAYWRIGHT_AVAILABLE:
        result["error"] = "Playwright در محیط فعلی در دسترس نیست."
        return result

    url = job.get("url")
    if not url:
        result["error"] = "آدرس URL مشخص نشده است."
        return result

    # TODO: پیاده‌سازی کامل در مرحله بعد
    result["error"] = "منطق Playwright هنوز پیاده‌سازی نشده است."
    return result


# ========== پردازش کار توسط ورکر ==========
def worker_process_job(worker_id: int,
                       job: Dict[str, Any],
                       mode: str,
                       session_mgr: SessionManager,
                       queue_mgr: QueueManager,
                       worker_mgr: WorkerManager):
    user_id = job["user_id"]
    chat_id = job["chat_id"]
    job_id = job["job_id"]

    logging.info(f"Worker {worker_id} شروع پردازش job {job_id} برای کاربر {user_id}")

    session_mgr.update(user_id, state="processing", status="processing", job_id=job_id)

    # اینجا منطق واقعی Playwright اجرا می‌شود:
    # output_dir = os.path.join(DATA_DIR, "jobs", job_id)
    # os.makedirs(output_dir, exist_ok=True)
    # result = run_playwright_job(job, mode, output_dir)

    # فعلاً شبیه‌سازی:
    time.sleep(3)
    dummy_zip_path = os.path.join(DATA_DIR, f"{job_id}.zip")
    # یک فایل کوچک ساختگی برای تست:
    with open(dummy_zip_path, "wb") as f:
        f.write(os.urandom(500_000))  # حدود 0.5MB

    # تقسیم فایل در صورت نیاز
    part_paths = split_zip_if_needed(dummy_zip_path)

    if len(part_paths) == 1:
        send_message(chat_id, "✅ پردازش انجام شد. در حال ارسال فایل ZIP…")
        send_document(chat_id, part_paths[0], caption="فایل ZIP سایت شما")
    else:
        send_message(chat_id,
                     f"✅ پردازش انجام شد. فایل بزرگ بود (>{MAX_ZIP_SIZE_MB}MB)، "
                     f"در {len(part_paths)} پارت ~{SPLIT_PART_SIZE_MB}MB تقسیم شد. در حال ارسال پارت‌ها…")

        total = len(part_paths)
        for idx, part in enumerate(part_paths, start=1):
            send_message(chat_id, f"📦 در حال ارسال پارت {idx} از {total} …")
            send_document(chat_id, part, caption=f"site_part{idx:02d}.zip")

        send_message(chat_id,
                     "✅ تمام پارت‌ها ارسال شد.\n"
                     "همه‌ی پارت‌ها را در یک پوشه دانلود کنید و با ابزار unzip/extract آن‌ها را کنار هم باز کنید.")

    # آزاد کردن ورکر
    worker_mgr.release_worker(worker_id)
    session_mgr.update(user_id, state="idle", status="done", job_id=None)
    logging.info(f"Worker {worker_id} پردازش job {job_id} را تمام کرد.")

    # بعد از پایان این job، اگر کسی در صف است، می‌توانیم در حلقه‌ی اصلی job بعدی را به یک ورکر آزاد بدهیم.
    # فعلاً مدیریت صف در حلقه اصلی انجام می‌شود.


# ========== مدیریت ورودی‌ها (پیام‌ها / callback ها) ==========
def handle_start(chat_id: int, user_id: str, session_mgr: SessionManager):
    session_mgr.update(user_id, state="idle", mode=None, last_url=None, job_id=None, status=None)
    send_message(
        chat_id,
        "سلام! 👋\n"
        "من ربات «مرور و دانلود سایت» هستم.\n"
        "از منوی زیر یکی از گزینه‌ها را انتخاب کن:",
        reply_markup=main_menu_keyboard(),
    )


def handle_text_message(chat_id: int, user_id: str, text: str,
                        session_mgr: SessionManager,
                        queue_mgr: QueueManager,
                        worker_mgr: WorkerManager):
    sess = session_mgr.get(user_id)
    state = sess.get("state")
    mode = sess.get("mode")

    if text.startswith("/start"):
        handle_start(chat_id, user_id, session_mgr)
        return

    if state == "waiting_for_url" and mode in ("screenshot", "download", "browser"):
        url = text.strip()
        # TODO: اعتبارسنجی ساده URL
        job_id = str(uuid.uuid4())
        sess = session_mgr.get(user_id)
        sess["last_url"] = url
        sess["job_id"] = job_id
        sess["state"] = "in_queue"
        sess["status"] = "queued"
        session_mgr.save()

        job = {
            "job_id": job_id,
            "user_id": user_id,
            "chat_id": chat_id,
            "url": url,
            "mode": mode,
            "created_at": time.time(),
        }

        # سعی می‌کنیم ببینیم آیا ورکر آزاد هست
        idle_worker = worker_mgr.find_idle_worker()
        if idle_worker is not None:
            wid = idle_worker["id"]
            worker_mgr.set_worker_job(wid, job_id, user_id)
            send_message(chat_id, "✅ درخواست شما بلافاصله در حال پردازش است…")
            # اجرای پردازش در یک نخ جداگانه:
            t = threading.Thread(target=worker_process_job,
                                 args=(wid, job, mode, session_mgr, queue_mgr, worker_mgr),
                                 daemon=True)
            t.start()
        else:
            # می‌رود در صف
            queue_mgr.add_job(job)
            pos = queue_mgr.get_position(job_id)
            send_message(chat_id,
                         f"درخواست شما در صف قرار گرفت. 🎫\n"
                         f"موقعیت شما در صف: {pos}")
        return

    # اگر در حالت دیگری است و متن ارسال کرد:
    send_message(chat_id,
                 "پیام شما دریافت شد، اما الان منتظر URL نیستم.\n"
                 "برای شروع دوباره /start را بفرست یا از منوی شیشه‌ای استفاده کن.",
                 reply_markup=main_menu_keyboard())


def handle_callback_query(callback_query: Dict[str, Any],
                          session_mgr: SessionManager,
                          queue_mgr: QueueManager,
                          worker_mgr: WorkerManager):
    cq_id = callback_query.get("id")
    from_user = callback_query.get("from", {})
    user_id = str(from_user.get("id"))
    message = callback_query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    data = callback_query.get("data")

    if not chat_id:
        return

    # اگر بله نیاز به answerCallbackQuery داشته باشد، باید اینجا صدا بزنیم.
    # bale_request("answerCallbackQuery", {"callback_query_id": cq_id})

    sess = session_mgr.get(user_id)

    if data == "mode_browser":
        session_mgr.update(user_id, mode="browser", state="waiting_for_url", status="await_url")
        send_message(chat_id, "🔗 لطفاً آدرس سایت مورد نظر برای «مرورگر من» را بفرست.")
    elif data == "mode_screenshot":
        session_mgr.update(user_id, mode="screenshot", state="waiting_for_url", status="await_url")
        send_message(chat_id, "📸 لطفاً آدرس سایت مورد نظر برای اسکرین‌شات فول‌پیج را بفرست.")
    elif data == "mode_download":
        session_mgr.update(user_id, mode="download", state="waiting_for_url", status="await_url")
        send_message(chat_id, "📥 لطفاً آدرس سایت مورد نظر برای دانلود محتوای کامل را بفرست.")
    elif data == "cancel":
        job_id = sess.get("job_id")
        if job_id:
            # اول بررسی کنیم آیا در صف است
            pos = queue_mgr.get_position(job_id)
            if pos is not None:
                # در صف است → حذف
                queue_mgr.remove_job(job_id)
                session_mgr.update(user_id, state="idle", status="cancelled", job_id=None)
                send_message(chat_id, "❌ درخواست شما قبل از پردازش از صف حذف شد.")
                return

            # اگر در حال پردازش است، ورکر را پیدا کنیم:
            w = worker_mgr.find_worker_by_job(job_id)
            if w:
                # در نسخه ساده: فقط فلگ می‌گذاریم و بعداً در منطق Playwright چک می‌کنیم
                # فعلاً کار را ساده می‌گیریم و به کاربر پیام می‌دهیم که ممکن است چند لحظه طول بکشد.
                send_message(chat_id,
                             "⏳ درخواست لغو پردازش ارسال شد. "
                             "ممکن است چند ثانیه طول بکشد تا پردازش متوقف و ورکر آزاد شود.")
                # برای نسخه فعلی که کار ساختگی است، فرض می‌کنیم نصفه راه لغو نمی‌کنیم
                # فقط پس از اتمام job، کاربر استیت idle می‌شود.
            else:
                send_message(chat_id, "در حال حاضر پردازشی برای لغو کردن پیدا نشد.")
        else:
            send_message(chat_id, "در حال حاضر پردازشی در جریان نیست که لغو شود.")
    else:
        send_message(chat_id, "دستور نامشخص بود.", reply_markup=main_menu_keyboard())


# ========== حلقه اصلی Polling ==========
def main():
    logging.info("ربات شروع به کار کرد (polling)...")
    session_mgr = SessionManager(SESSIONS_FILE)
    queue_mgr = QueueManager(QUEUE_FILE)
    worker_mgr = WorkerManager(WORKERS_FILE)

    last_update_id = None

    while True:
        updates = get_updates(last_update_id + 1 if last_update_id else None)
        for upd in updates:
            last_update_id = upd.get("update_id", last_update_id)

            if "message" in upd:
                msg = upd["message"]
                chat_id = msg["chat"]["id"]
                from_user = msg.get("from", {})
                user_id = str(from_user.get("id"))
                text = msg.get("text", "")

                handle_text_message(chat_id, user_id, text, session_mgr, queue_mgr, worker_mgr)

            elif "callback_query" in upd:
                handle_callback_query(upd["callback_query"], session_mgr, queue_mgr, worker_mgr)

        # پس از پردازش آپدیت‌ها، اگر ورکر آزاد داریم و صف خالی نیست، job بعدی را بدهیم
        idle_worker = worker_mgr.find_idle_worker()
        while idle_worker is not None:
            job = queue_mgr.pop_next()
            if job is None:
                break
            wid = idle_worker["id"]
            worker_mgr.set_worker_job(wid, job["job_id"], job["user_id"])
            send_message(job["chat_id"],
                         "✅ نوبت شما رسید. درخواست شما وارد پردازش شد…")
            t = threading.Thread(target=worker_process_job,
                                 args=(wid, job, job["mode"], session_mgr, queue_mgr, worker_mgr),
                                 daemon=True)
            t.start()
            idle_worker = worker_mgr.find_idle_worker()

        time.sleep(1)


if __name__ == "__main__":
    main()
