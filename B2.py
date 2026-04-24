import os
import time
import json
import threading
import subprocess
import requests
import queue
from playwright.sync_api import sync_playwright

# ==========================================
# پیکربندی و متغیرهای سراسری
# ==========================================
BALE_TOKEN = "427718610:i79QB6avOiOAygLqy3e9GO3JXTJOttkWRvw"
BASE_URL = f"https://tapi.bale.ai/bot{BALE_TOKEN}"

# صف کارها برای پردازش در پس‌زمینه (جلوگیری از قفل شدن ربات)
job_queue = queue.Queue()

# مدیریت وضعیت کاربران (ذخیره در مموری یا فایل)
user_sessions = {}

# ==========================================
# کلاس‌های داده و مدیریت وضعیت
# ==========================================
class UserSettings:
    def __init__(self):
        self.method = "direct" # direct or record
        self.quality = "720p"

class SessionState:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.state = "IDLE" # IDLE, WAITING_FOR_LINK, DOWNLOADING
        self.settings = UserSettings()

class Job:
    def __init__(self, chat_id, url, settings):
        self.chat_id = chat_id
        self.url = url
        self.settings = settings

def get_session(chat_id):
    if chat_id not in user_sessions:
        user_sessions[chat_id] = SessionState(chat_id)
    return user_sessions[chat_id]

# ==========================================
# توابع ارتباط با API بله
# ==========================================
def bale_request(method, payload=None, files=None):
    try:
        url = f"{BASE_URL}/{method}"
        if files:
            response = requests.post(url, data=payload, files=files, timeout=300)
        else:
            response = requests.post(url, json=payload, timeout=10)
        return response.json()
    except Exception as e:
        print(f"API Error: {e}")
        return None

def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    bale_request("sendMessage", payload)

def send_video(chat_id, video_path):
    with open(video_path, 'rb') as v:
        bale_request("sendVideo", payload={"chat_id": chat_id}, files={"video": v})

# ==========================================
# کیبوردها و رابط کاربری
# ==========================================
def main_menu_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "📥 ارسال لینک جدید", "callback_data": "new_link"}],
            [{"text": "⚙️ تنظیمات", "callback_data": "settings"}, {"text": "📊 وضعیت من", "callback_data": "status"}]
        ]
    }

def settings_keyboard(session):
    method_text = "حالت: دانلود مستقیم" if session.settings.method == "direct" else "حالت: ضبط صفحه"
    return {
        "inline_keyboard": [
            [{"text": method_text, "callback_data": "toggle_method"}],
            [{"text": "🔙 بازگشت", "callback_data": "main_menu"}]
        ]
    }

# ==========================================
# هسته پردازش ویدیو (Playwright + FFmpeg)
# ==========================================
def process_job(job: Job):
    send_message(job.chat_id, f"🔄 پردازش لینک آغاز شد...\nحالت: {job.settings.method}")
    
    m3u8_link = None
    try:
        with sync_playwright() as p:
            # استفاده از آرگومان‌های ضد شناسایی (Stealth-like)
            browser = p.chromium.launch(
                headless=False, # برای Xvfb در سرور
                args=["--disable-blink-features=AutomationControlled", "--mute-audio"]
            )
            context = browser.new_context(viewport={"width": 1280, "height": 720})
            page = context.new_page()

            # شنود شبکه برای پیدا کردن لینک‌های استریم
            def intercept(request):
                nonlocal m3u8_link
                if ".m3u8" in request.url or ".mpd" in request.url:
                    m3u8_link = request.url

            page.on("request", intercept)
            page.goto(job.url, timeout=60000)
            
            # بای‌پس پاپ‌آپ‌ها و تلاش برای کلیک
            page.evaluate("() => { const v = document.querySelector('video'); if(v) v.play(); }")
            page.wait_for_timeout(10000)
            browser.close()

        # مرحله دانلود یا ضبط
        output_file = f"video_{job.chat_id}_{int(time.time())}.mp4"
        
        if m3u8_link and job.settings.method == "direct":
            send_message(job.chat_id, "✅ لینک استریم پیدا شد. در حال دانلود...")
            subprocess.run(["ffmpeg", "-y", "-i", m3u8_link, "-c", "copy", output_file])
        else:
            send_message(job.chat_id, "⚠️ استفاده از حالت ضبط صفحه نمایش (Screen Record)...")
            # فرض بر اجرای Xvfb در سرور
            display_env = os.environ.get("DISPLAY", ":99")
            rec_proc = subprocess.Popen([
                "ffmpeg", "-y", "-video_size", "1280x720", "-framerate", "25",
                "-f", "x11grab", "-i", display_env, "-c:v", "libx264", "-preset", "veryfast", output_file
            ])
            time.sleep(300) # ضبط ۵ دقیقه (نمونه)
            rec_proc.terminate()
            rec_proc.wait()

        # برش ویدیو برای بله (حداکثر ۲۰ مگابایت)
        send_message(job.chat_id, "✂️ در حال قطعه‌بندی ویدیو...")
        subprocess.run([
            "ffmpeg", "-y", "-i", output_file, "-c", "copy", "-f", "segment", 
            "-segment_time", "150", "-reset_timestamps", "1", f"chunk_{job.chat_id}_%03d.mp4"
        ])
        
        # ارسال قطعات
        import glob
        chunks = sorted(glob.glob(f"chunk_{job.chat_id}_*.mp4"))
        for chunk in chunks:
            send_video(job.chat_id, chunk)
            os.remove(chunk)
            
        if os.path.exists(output_file):
            os.remove(output_file)
            
        send_message(job.chat_id, "🎉 عملیات با موفقیت پایان یافت!", reply_markup=main_menu_keyboard())

    except Exception as e:
        send_message(job.chat_id, f"❌ خطایی رخ داد: {str(e)}", reply_markup=main_menu_keyboard())
    finally:
        session = get_session(job.chat_id)
        session.state = "IDLE"

# ==========================================
# کارگر پس‌زمینه (Worker Thread)
# ==========================================
def worker():
    while True:
        job = job_queue.get()
        if job is None: break
        try:
            process_job(job)
        except Exception as e:
            print(f"Worker error: {e}")
        job_queue.task_done()

# ==========================================
# مدیریت آپدیت‌ها (Router)
# ==========================================
def handle_callback(update):
    callback = update.get("callback_query", {})
    chat_id = callback.get("message", {}).get("chat", {}).get("id")
    data = callback.get("data", "")
    session = get_session(chat_id)

    if data == "new_link":
        session.state = "WAITING_FOR_LINK"
        send_message(chat_id, "🔗 لطفا لینک ویدیو (مثلا از سایت Hanime) را ارسال کنید:")
    
    elif data == "settings":
        send_message(chat_id, "تنظیمات ربات:", reply_markup=settings_keyboard(session))
        
    elif data == "toggle_method":
        session.settings.method = "record" if session.settings.method == "direct" else "direct"
        send_message(chat_id, "تنظیمات بروز شد.", reply_markup=settings_keyboard(session))
        
    elif data == "main_menu":
        session.state = "IDLE"
        send_message(chat_id, "🏠 منوی اصلی", reply_markup=main_menu_keyboard())
        
    elif data == "status":
        pos = list(job_queue.queue).index(next((j for j in job_queue.queue if j.chat_id == chat_id), None)) if any(j.chat_id == chat_id for j in job_queue.queue) else -1
        status_text = f"وضعیت شما: {session.state}\n"
        status_text += f"جایگاه در صف: {pos + 1}" if pos >= 0 else "شما در صف نیستید."
        send_message(chat_id, status_text, reply_markup=main_menu_keyboard())

def handle_message(update):
    msg = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "")
    session = get_session(chat_id)

    if text == "/start":
        session.state = "IDLE"
        send_message(chat_id, "سلام! به ربات استخراج ویدیو خوش آمدید. 🎬\nلطفاً از منوی زیر انتخاب کنید:", reply_markup=main_menu_keyboard())
        return

    if session.state == "WAITING_FOR_LINK":
        if text.startswith("http"):
            session.state = "DOWNLOADING"
            new_job = Job(chat_id, text, session.settings)
            job_queue.put(new_job)
            queue_pos = job_queue.qsize()
            send_message(chat_id, f"✅ لینک به صف پردازش اضافه شد.\nشماره شما در صف: {queue_pos}")
        else:
            send_message(chat_id, "❌ لینک نامعتبر است. لطفا یک لینک با http شروع کنید.")

# ==========================================
# حلقه اصلی اجرای ربات
# ==========================================
def main_loop():
    last_update_id = 0
    print("Bot is polling...")
    
    # راه‌اندازی Worker Thread
    threading.Thread(target=worker, daemon=True).start()

    while True:
        try:
            resp = bale_request("getUpdates", {"offset": last_update_id, "timeout": 10})
            if resp and resp.get("ok"):
                for update in resp["result"]:
                    last_update_id = update["update_id"] + 1
                    
                    if "callback_query" in update:
                        handle_callback(update)
                    elif "message" in update:
                        handle_message(update)
                        
        except Exception as e:
            print(f"Polling Error: {e}")
            time.sleep(5) # Backoff در صورت خطا
        time.sleep(1)

if __name__ == "__main__":
    main_loop()
              
