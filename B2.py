import os
import re
import time
import uuid
import queue
import sqlite3
import threading
import subprocess
import requests
from playwright.sync_api import sync_playwright

# ==========================================
# CONFIGURATION
# ==========================================
BALE_TOKEN = os.environ.get("BALE_BOT_TOKEN2", "توکن_ربات_شما")
API_URL = f"https://tapi.bale.ai/bot{BALE_TOKEN}"
MAX_FILE_SIZE_MB = 19  # محدودیت حجم بله

# ==========================================
# DATABASE & STATE MANAGEMENT
# ==========================================
class Database:
    def __init__(self):
        self.conn = sqlite3.connect("bot_data.db", check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS streams (
                id TEXT PRIMARY KEY,
                user_id INTEGER,
                url TEXT,
                page_url TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                record_duration INTEGER DEFAULT 120
            )
        ''')
        self.conn.commit()

    def save_stream(self, user_id, stream_url, page_url):
        stream_id = "dl_" + str(uuid.uuid4())[:8]
        self.cursor.execute('INSERT INTO streams (id, user_id, url, page_url) VALUES (?, ?, ?, ?)', 
                            (stream_id, user_id, stream_url, page_url))
        self.conn.commit()
        return stream_id

    def get_stream(self, stream_id):
        self.cursor.execute('SELECT url FROM streams WHERE id = ?', (stream_id,))
        row = self.cursor.fetchone()
        return row[0] if row else None

    def get_setting(self, user_id):
        self.cursor.execute('SELECT record_duration FROM user_settings WHERE user_id = ?', (user_id,))
        row = self.cursor.fetchone()
        if not row:
            self.cursor.execute('INSERT INTO user_settings (user_id) VALUES (?)', (user_id,))
            self.conn.commit()
            return 120
        return row[0]

    def set_duration(self, user_id, duration):
        self.cursor.execute('UPDATE user_settings SET record_duration = ? WHERE user_id = ?', (duration, user_id))
        self.conn.commit()

db = Database()
job_queue = queue.Queue()

# ==========================================
# BALE API MANAGER
# ==========================================
class BaleAPI:
    @staticmethod
    def send_message(chat_id, text):
        requests.post(f"{API_URL}/sendMessage", json={"chat_id": chat_id, "text": text})

    @staticmethod
    def send_document(chat_id, file_path):
        with open(file_path, 'rb') as f:
            requests.post(f"{API_URL}/sendDocument", data={"chat_id": chat_id}, files={"document": f})

# ==========================================
# MEDIA PROCESSOR (Playwright & FFmpeg)
# ==========================================
class MediaEngine:
    def __init__(self):
        self.playwright_args = [
            '--disable-gpu',
            '--disable-software-rasterizer',
            '--disable-dev-shm-usage',
            '--no-sandbox',
            '--autoplay-policy=no-user-gesture-required',
            '--mute-audio' # برای جلوگیری از مشکلات صوتی در سرور
        ]

    def sniff_network(self, url, user_id):
        """مرورگر را باز می‌کند و تمام لینک‌های ویدیو را استخراج می‌کند"""
        found_streams = set()
        
        BaleAPI.send_message(user_id, "⏳ در حال آنالیز سایت و استخراج سورس‌های مخفی ویدیو...")
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=self.playwright_args)
            page = browser.new_page()

            # شنود شبکه
            def handle_response(response):
                if response.request.resource_type in ["xhr", "fetch", "media"]:
                    r_url = response.url
                    # پیدا کردن لینک‌های استریم یا ویدیو مستقیم
                    if ".m3u8" in r_url or ".mpd" in r_url or ".mp4" in r_url:
                        # فیلتر کردن تبلیغات معروف (میتوانید کلمات بیشتری اضافه کنید)
                        if "ads" not in r_url and "tracker" not in r_url:
                            found_streams.add(r_url)

            page.on("response", handle_response)

            try:
                page.goto(url, timeout=45000, wait_until="networkidle")
                # تلاش برای کلیک روی هر دکمه پلی که در صفحه است
                page.evaluate("""() => {
                    document.querySelectorAll('video, button, .play-btn').forEach(el => {
                        try { el.click(); el.play(); } catch(e) {}
                    });
                }""")
                time.sleep(10) # صبر برای لود شدن استریم‌ها بعد از کلیک
            except Exception as e:
                print(f"Page load warning: {e}")
            finally:
                browser.close()

        return list(found_streams)

    def download_and_split(self, user_id, stream_url):
        """دانلود با بالاترین سرعت و برش ویدیو برای بله"""
        BaleAPI.send_message(user_id, "⬇️ در حال دانلود مستقیم فایل اصلی... (این کار بسیار سریع‌تر از ضبط صفحه است)")
        
        output_file = f"temp_video_{user_id}.mp4"
        
        # استفاده از ffmpeg برای دانلود m3u8 و تبدیل به mp4
        cmd = [
            'ffmpeg', '-y', '-i', stream_url, 
            '-c', 'copy', # کپی مستقیم بدون رندر مجدد (سرعت بی‌نهایت بالا)
            '-bsf:a', 'aac_adtstoasc', output_file
        ]
        
        process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        if not os.path.exists(output_file) or os.path.getsize(output_file) == 0:
            BaleAPI.send_message(user_id, "❌ خطایی در دانلود سورس ویدیو رخ داد. ممکن است لینک منقضی شده باشد.")
            return

        file_size_mb = os.path.getsize(output_file) / (1024 * 1024)
        
        if file_size_mb <= MAX_FILE_SIZE_MB:
            BaleAPI.send_message(user_id, "📤 در حال آپلود ویدیو...")
            BaleAPI.send_document(user_id, output_file)
        else:
            BaleAPI.send_message(user_id, f"✂️ حجم ویدیو {file_size_mb:.1f} مگابایت است. در حال برش به قطعات {MAX_FILE_SIZE_MB} مگابایتی...")
            self.split_and_upload(user_id, output_file)
            
        if os.path.exists(output_file):
            os.remove(output_file)

    def split_and_upload(self, user_id, input_file):
        """برش ویدیو به قطعات مساوی بر اساس حجم"""
        segment_pattern = f"chunk_{user_id}_%03d.mp4"
        cmd = [
            'ffmpeg', '-y', '-i', input_file,
            '-c', 'copy',
            '-f', 'segment',
            '-segment_time', '180', # هر 3 دقیقه (تقریباً کمتر از 20 مگابایت میشود)
            '-reset_timestamps', '1',
            segment_pattern
        ]
        subprocess.run(cmd)

        for f in sorted(os.listdir('.')):
            if f.startswith(f"chunk_{user_id}_") and f.endswith(".mp4"):
                BaleAPI.send_document(user_id, f)
                os.remove(f)
        BaleAPI.send_message(user_id, "✅ ارسال تمام قطعات با موفقیت انجام شد.")

    def record_screen(self, user_id, url, duration):
        """ضبط مستقیم از صفحه (حالت Fallback) در صورت شکست شنود"""
        BaleAPI.send_message(user_id, f"🎥 در حال ضبط صفحه به مدت {duration} ثانیه...\n(توجه: صفحه سیاه فیکس شده است)")
        output_file = f"rec_{user_id}.mp4"
        
        with sync_playwright() as p:
            # رزولوشن دقیق هماهنگ با Xvfb
            browser = p.chromium.launch(headless=False, args=self.playwright_args)
            context = browser.new_context(viewport={'width': 1280, 'height': 720})
            page = context.new_page()

            page.goto(url)
            
            # تلاش برای تمام صفحه کردن پلیر وب
            page.evaluate("""() => {
                let v = document.querySelector('video');
                if(v) { v.play(); v.requestFullscreen(); }
            }""")

            # شروع ضبط با ffmpeg از مانیتور مجازی (Xvfb)
            ffmpeg_cmd = [
                'ffmpeg', '-y',
                '-video_size', '1280x720',
                '-framerate', '24',
                '-f', 'x11grab',
                '-i', ':99.0', # آدرس مانیتور Xvfb
                '-t', str(duration), # زمان ضبط
                '-c:v', 'libx264',
                '-preset', 'ultrafast',
                '-pix_fmt', 'yuv420p',
                output_file
            ]
            
            subprocess.run(ffmpeg_cmd)
            browser.close()

        if os.path.exists(output_file):
            BaleAPI.send_message(user_id, "✂️ پردازش ویدیو...")
            self.split_and_upload(user_id, output_file)
            if os.path.exists(output_file):
                os.remove(output_file)

# ==========================================
# WORKER THREAD (Background processing)
# ==========================================
def worker():
    engine = MediaEngine()
    while True:
        task = job_queue.get()
        user_id = task['user_id']
        action = task['action']
        
        try:
            if action == 'sniff':
                url = task['url']
                streams = engine.sniff_network(url, user_id)
                
                if not streams:
                    BaleAPI.send_message(user_id, "⚠️ هیچ سورس ویدیویی مخفی پیدا نشد! احتمالاً سایت محافظت شده است.\n\nبرای فیلم‌برداری از صفحه می‌توانید از دستور زیر استفاده کنید:\n/record " + url)
                else:
                    msg = f"✅ {len(streams)} سورس ویدیو یافت شد!\nبرای دانلود، روی یکی از لینک‌های زیر کلیک کنید:\n\n"
                    for idx, s_url in enumerate(streams):
                        s_id = db.save_stream(user_id, s_url, url)
                        # ساخت دستور داینامیک برای هر ویدیو
                        type_str = "HLS(m3u8)" if ".m3u8" in s_url else "MP4"
                        msg += f"🎬 سورس {idx+1} [{type_str}]:\n/{s_id}\n\n"
                    BaleAPI.send_message(user_id, msg)

            elif action == 'download':
                stream_url = task['stream_url']
                engine.download_and_split(user_id, stream_url)
                
            elif action == 'record':
                url = task['url']
                duration = db.get_setting(user_id)
                engine.record_screen(user_id, url, duration)

        except Exception as e:
            BaleAPI.send_message(user_id, f"❌ خطای پردازش: {str(e)}")
        
        job_queue.task_done()

threading.Thread(target=worker, daemon=True).start()

# ==========================================
# TELEGRAM/BALE BOT POLLING
# ==========================================
def main():
    print("Bot started...")
    last_update_id = 0
    
    while True:
        try:
            resp = requests.get(f"{API_URL}/getUpdates", params={"offset": last_update_id, "timeout": 10}, timeout=15)
            if resp.status_code != 200:
                time.sleep(2)
                continue
                
            updates = resp.json().get("result", [])
            for update in updates:
                last_update_id = update["update_id"] + 1
                
                if "message" not in update or "text" not in update["message"]:
                    continue
                    
                msg = update["message"]
                chat_id = msg["chat"]["id"]
                text = msg["text"].strip()

                if text == "/start":
                    welcome = (
                        "👋 به ربات پیشرفته دانلود استریم خوش آمدید!\n\n"
                        "🔗 **لینک صفحه ویدیو** را بفرستید تا سورس اصلی آن را شکار کنم.\n\n"
                        "⚙️ **تنظیمات:**\n"
                        "برای تعیین زمان ضبط صفحه (مثلاً 180 ثانیه) بفرستید:\n"
                        "`/set_time 180`\n\n"
                        "برای ضبط مستقیم از صفحه بدون اسکن بفرستید:\n"
                        "`/record لینک_سایت`"
                    )
                    BaleAPI.send_message(chat_id, welcome)

                elif text.startswith("/set_time "):
                    try:
                        seconds = int(text.split()[1])
                        db.set_duration(chat_id, seconds)
                        BaleAPI.send_message(chat_id, f"⏱ زمان ضبط صفحه با موفقیت روی {seconds} ثانیه تنظیم شد.")
                    except:
                        BaleAPI.send_message(chat_id, "❌ فرمت اشتباه است. مثال: /set_time 120")

                elif text.startswith("/record "):
                    url = text.replace("/record ", "").strip()
                    job_queue.put({'user_id': chat_id, 'action': 'record', 'url': url})
                    BaleAPI.send_message(chat_id, "⏳ درخواست ضبط صفحه به صف اضافه شد...")

                elif text.startswith("/dl_"): # دستور دانلود سورس انتخاب شده
                    stream_id = text[1:] # حذف اسلش
                    stream_url = db.get_stream(stream_id)
                    if stream_url:
                        job_queue.put({'user_id': chat_id, 'action': 'download', 'stream_url': stream_url})
                        BaleAPI.send_message(chat_id, "📥 درخواست دانلود مستقیم به صف اضافه شد...")
                    else:
                        BaleAPI.send_message(chat_id, "❌ لینک استریم یافت نشد یا منقضی شده است.")

                elif text.startswith("http"):
                    job_queue.put({'user_id': chat_id, 'action': 'sniff', 'url': text})
                    BaleAPI.send_message(chat_id, "🔍 لینک دریافت شد! در حال اسکن شبکه سایت برای یافتن سورس اصلی... لطفاً صبور باشید.")

        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
