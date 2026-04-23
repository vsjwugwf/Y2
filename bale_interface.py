import subprocess
import json
import requests
import os
import sys

# توکن بله از متغیرهای محیطی گیت‌هاب اکشنز خوانده می‌شود
BALE_TOKEN = os.environ.get("BALE_TOKEN")
BALE_API_URL = f"https://tapi.bale.ai/bot{BALE_TOKEN}"

def send_message(chat_id, text):
    """ارسال پیام متنی به کاربر در بله"""
    url = f"{BALE_API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Error sending message: {e}")

def send_document(chat_id, file_path, caption=""):
    """ارسال فایل (مثل اسکرین‌شات یا ویدیو) به بله"""
    url = f"{BALE_API_URL}/sendDocument"
    try:
        with open(file_path, 'rb') as file:
            files = {'document': file}
            data = {'chat_id': chat_id, 'caption': caption}
            requests.post(url, data=data, files=files)
    except Exception as e:
        print(f"Error sending document: {e}")

def extract_link_from_core(youtube_url):
    """اجرای فایل هسته و دریافت خروجی JSON"""
    input_data = json.dumps({"url": youtube_url})
    try:
        # اجرای core_browser.py به عنوان یک پراسس جداگانه
        result = subprocess.run(
            [sys.executable, "core_browser.py", input_data],
            capture_output=True,
            text=True,
            check=True
        )
        # خواندن خروجی چاپ شده (stdout)
        return json.loads(result.stdout)
    except Exception as e:
        return {"status": "error", "error_message": str(e), "screenshot_path": None}

def process_request(chat_id, youtube_url):
    """مدیریت کل فرآیند برای یک درخواست"""
    send_message(chat_id, "⏳ در حال ارتباط با سرورهای یوتیوب (مرورگر ارواح)...")
    
    # 1. استخراج لینک
    core_result = extract_link_from_core(youtube_url)
    
    # 2. بررسی نتیجه
    if core_result.get("status") == "success":
        stream_url = core_result.get("stream_url")
        title = core_result.get("title")
        
        send_message(chat_id, f"✅ لینک استخراج شد!\n🎬 عنوان: {title}\n\n📥 در حال شروع دانلود و پارت‌بندی...")
        
        # TODO: در مرحله بعدی، کد مربوط به دانلود با ffmpeg و تکه‌تکه کردن (chunking) اینجا اضافه می‌شود.
        # موقتاً لینک مستقیم را ارسال می‌کنیم تا کارکرد را تست کنیم:
        send_message(chat_id, f"لینک مستقیم استریم (اعتبار موقت):\n{stream_url}")
        
    else:
        error_msg = core_result.get("error_message")
        screenshot_path = core_result.get("screenshot_path")
        
        send_message(chat_id, f"❌ خطایی رخ داد:\n{error_msg}")
        
        if screenshot_path and screenshot_path != "Failed to take screenshot" and os.path.exists(screenshot_path):
            send_document(chat_id, screenshot_path, "📸 اسکرین‌شات از وضعیت مرورگر هنگام خطا")

if __name__ == "__main__":
    # این فایل با دو آرگومان اجرا می‌شود: آیدی چت و لینک یوتیوب
    # مثال اجرایی در گیت‌هاب اکشنز: python bale_interface.py 123456789 https://youtube.com/watch?v=...
    if len(sys.argv) < 3:
        print("Usage: python bale_interface.py <chat_id> <youtube_url>")
        sys.exit(1)
        
    user_chat_id = sys.argv[1]
    target_url = sys.argv[2]
    
    if not BALE_TOKEN:
        print("Error: BALE_TOKEN environment variable is not set.")
        sys.exit(1)
        
    process_request(user_chat_id, target_url)
  
