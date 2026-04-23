import asyncio
import json
import sys
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

async def run_ghost_browser(url):
    stream_url = None
    title = "Unknown Title"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled', 
                '--no-sandbox', 
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage'
            ]
        )
        context = await browser.new_context()
        # تزریق اسکریپت برای مخفی کردن webdriver
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = await context.new_page()

        # شنود ترافیک شبکه (Network Interception)
        async def handle_request(request):
            nonlocal stream_url
            if "googlevideo.com/videoplayback" in request.url and not stream_url:
                stream_url = request.url

        page.on("request", handle_request)

        try:
            # استفاده از domcontentloaded برای جلوگیری از توقف بیهوده و Timeout
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            
            # تلاش برای دریافت عنوان ویدیو
            try:
                title = await page.title()
                title = title.replace(" - YouTube", "")
            except:
                pass
            
            # چند ثانیه صبر کوتاه تا در صورت لزوم درخواست‌های شبکه ثبت شوند
            for _ in range(50):
                if stream_url:
                    break
                await asyncio.sleep(0.1)
            
            if stream_url:
                return json.dumps({"status": "success", "stream_url": stream_url, "title": title})
            else:
                raise Exception("ترافیک استریم ویدیو یافت نشد.")
                
        except Exception as e:
            # مدیریت خطا و ثبت اسکرین‌شات
            screenshot_path = "./error_screenshot.png"
            try:
                await page.screenshot(path=screenshot_path, full_page=True)
            except:
                screenshot_path = "Failed to take screenshot"
            
            return json.dumps({
                "status": "error", 
                "error_message": str(e), 
                "screenshot_path": screenshot_path
            })
        finally:
            await browser.close()

if __name__ == "__main__":
    # غیرفعال کردن لاگ‌های اضافی خطا در خروجی استاندارد
    sys.tracebacklimit = 0
    
    try:
        input_data = json.loads(sys.argv[1])
        target_url = input_data.get("url")
        if not target_url:
            raise ValueError("URL در ورودی JSON یافت نشد.")
            
        # اجرای منطق و پرینت کردن تنها یک خط JSON
        result_json = asyncio.run(run_ghost_browser(target_url))
        print(result_json)
        
    except Exception as base_error:
        print(json.dumps({
            "status": "error", 
            "error_message": f"خطای ورودی/سیستمی: {str(base_error)}", 
            "screenshot_path": None
        }))
      
