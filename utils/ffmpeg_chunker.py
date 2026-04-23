import subprocess
import os
import glob

def download_and_chunk(stream_url, output_prefix="video"):
    """
    دانلود ویدیو از استریم و تکه‌تکه کردن آن با FFmpeg
    """
    output_dir = "chunks"
    os.makedirs(output_dir, exist_ok=True)
    
    # الگوی نام‌گذاری پارت‌ها: chunks/video_part_000.mp4
    output_pattern = os.path.join(output_dir, f"{output_prefix}_part_%03d.mp4")
    
    # دستور FFmpeg: کپی مستقیم استریم و قطعه‌بندی هر 300 ثانیه (5 دقیقه)
    command = [
        "ffmpeg",
        "-y",                     # بازنویسی در صورت وجود
        "-i", stream_url,         # لینک مستقیم یوتیوب (استخراج شده توسط هسته)
        "-c", "copy",             # بدون رندر مجدد (بسیار سریع)
        "-f", "segment",          # فرمت قطعه‌بندی
        "-segment_time", "300",   # طول هر قطعه به ثانیه
        "-reset_timestamps", "1", 
        output_pattern
    ]
    
    try:
        # اجرای FFmpeg
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        # جمع‌آوری و مرتب‌سازی لیست پارت‌های تولید شده
        chunks = sorted(glob.glob(os.path.join(output_dir, f"{output_prefix}_part_*.mp4")))
        return {"status": "success", "chunks": chunks}
        
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode('utf-8', errors='ignore')
        return {"status": "error", "error_message": f"FFmpeg failed: {error_msg[-200:]}"}
      
