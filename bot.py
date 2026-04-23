import os
import zipfile
import time
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# -----------------------------
# تنظیمات ربات بله
# -----------------------------
BOT_TOKEN = "2113073642:47v_F7qGUGXaH8aFvXjBNCtGIAuHpyZbdYI"
BASE_URL = f"https://tapi.bale.ai/bot{BOT_TOKEN}/"

def send_message(chat_id, text):
    requests.post(BASE_URL + "sendMessage", data={
        "chat_id": chat_id,
        "text": text
    })

def send_action(chat_id, action):
    requests.post(BASE_URL + "sendChatAction", data={
        "chat_id": chat_id,
        "action": action
    })

def send_file(chat_id, file_path, caption=None):
    files = {"document": open(file_path, "rb")}
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    requests.post(BASE_URL + "sendDocument", data=data, files=files)

# -----------------------------
# اسکرپینگ سایت
# -----------------------------
def run_crawler(url):
    if os.path.isdir("output"):
        import shutil
        shutil.rmtree("output")
    os.makedirs("output/assets", exist_ok=True)
    os.makedirs("output/thumbnails", exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0")

        page.goto(url, timeout=60000)
        time.sleep(2)

        # اسکرول کامل صفحه
        height = page.evaluate("document.body.scrollHeight")
        step = height // 10
        for pos in range(0, height, step):
            page.evaluate(f"window.scrollTo(0, {pos})")
            time.sleep(0.4)

        # اسکرین‌شات
        page.screenshot(path="output/screenshot.png", full_page=True)

        # HTML خام
        html = page.content()
        open("output/index.html", "w", encoding="utf-8").write(html)

        soup = BeautifulSoup(html, "html.parser")

        # -----------------------------
        # دانلود تامنیل‌ها
        # -----------------------------
        thumbs = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if not src:
                continue
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("/"):
                src = url.rstrip("/") + src
            thumbs.append(src)

        for idx, link in enumerate(thumbs):
            try:
                data = requests.get(link, timeout=10).content
                open(f"output/thumbnails/thumb_{idx}.jpg", "wb").write(data)
            except:
                pass

        # -----------------------------
        # دانلود CSS و JS
        # -----------------------------
        assets = []

        for tag in soup.find_all(["link", "script"]):
            if tag.name == "link" and tag.get("href"):
                href = tag.get("href")
                if href.endswith(".css"):
                    assets.append(href)

            if tag.name == "script" and tag.get("src"):
                src = tag.get("src")
                if src.endswith(".js"):
                    assets.append(src)

        # تمیز کردن لینک‌ها
        clean_assets = []
        for link in assets:
            if link.startswith("//"):
                link = "https:" + link
            if link.startswith("/"):
                link = url.rstrip("/") + link
            clean_assets.append(link)

        # دانلود
        for idx, link in enumerate(clean_assets):
            try:
                data = requests.get(link, timeout=10).content
                ext = link.split("?")[0].split(".")[-1]
                open(f"output/assets/asset_{idx}.{ext}", "wb").write(data)
            except:
                pass

        browser.close()

    # -----------------------------
    # ZIP تامنیل‌ها
    # -----------------------------
    thumbs_zip = "thumbnails.zip"
    with zipfile.ZipFile(thumbs_zip, "w") as z:
        for f in os.listdir("output/thumbnails"):
            z.write(f"output/thumbnails/{f}", arcname=f)

    # -----------------------------
    # ZIP کل سایت
    # -----------------------------
    full_zip = "site_full.zip"
    with zipfile.ZipFile(full_zip, "w") as z:
        z.write("output/index.html", arcname="index.html")
        z.write("output/screenshot.png", arcname="screenshot.png")

        for f in os.listdir("output/assets"):
            z.write(f"output/assets/{f}", arcname=f"assets/{f}")

        for f in os.listdir("output/thumbnails"):
            z.write(f"output/thumbnails/{f}", arcname=f"thumbnails/{f}")

    return thumbs_zip, full_zip, "output/screenshot.png"

# -----------------------------
# ربات Polling ساده
# -----------------------------
def run_bot():
    offset = 0
    send_message("YOUR_CHAT_ID", "ربات آماده است 👍")

    while True:
        r = requests.get(BASE_URL + "getUpdates", params={"offset": offset})
        updates = r.json().get("result", [])

        for upd in updates:
            offset = upd["update_id"] + 1

            if "message" not in upd:
                continue

            msg = upd["message"]
            chat_id = msg["chat"]["id"]
            text = msg.get("text", "")

            if text.startswith("http"):
                send_action(chat_id, "typing")
                send_message(chat_id, "در حال پردازش لینک ... ⏳")

                thumbs_zip, full_zip, shot = run_crawler(text)

                send_action(chat_id, "upload_photo")
                send_file(chat_id, shot, "اسکرین‌شات کامل صفحه")

                send_action(chat_id, "upload_document")
                send_file(chat_id, thumbs_zip, "📁 فایل ZIP تامنیل‌ها")

                send_action(chat_id, "upload_document")
                send_file(chat_id, full_zip, "📁 فایل ZIP کل محتوا")

                send_message(chat_id, "🎉 پردازش انجام شد")

        time.sleep(1)

if __name__ == "__main__":
    run_bot()
                  
