import os
import logging
import tempfile
import shutil
import time
import re
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import asyncio
import httpx
from urllib.parse import urlparse, urlunparse
from PIL import Image
import requests
import socket
import random
import pathlib

# Telegram bot token
BOT_TOKEN = "ENTER_YOUR_BOT_TOKEN_HERE"

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

last_progress_update_time = 0

# Escape text for MarkdownV2
def escape_markdown(text: str) -> str:
    return re.sub(r'([_*\[\]()~`>#+=|{}.!\\-])', r'\\\1', text)

# Sanitize filename to remove invalid characters
def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)

def upload_to_gofile(file_path: str):
    fallback_servers = ["store1", "store2", "store3", "store4", "store5"]
    tried_servers = []

    # First, try getting a recommended server
    try:
        response = requests.get("https://api.gofile.io/getServer", timeout=10)
        response.raise_for_status()
        server = response.json()["data"]["server"]
        tried_servers.append(server)
    except Exception as e:
        logger.warning(f"❌ Failed to get Gofile server dynamically: {e}")
        server = random.choice(fallback_servers)
        tried_servers.append(server)

    # Try upload, retry with fallbacks if NameResolutionError or other connection issues
    for attempt, srv in enumerate([server] + fallback_servers):
        if srv in tried_servers:
            continue
        try:
            url = f"https://{srv}.gofile.io/uploadFile"
            with open(file_path, "rb") as f:
                files = {"file": f}
                response = requests.post(url, files=files, timeout=30)
                response.raise_for_status()
                data = response.json()
                if data["status"] != "ok":
                    raise Exception("Gofile API returned error")
                return data["data"]["downloadPage"]
        except requests.exceptions.RequestException as e:
            logger.warning(f"⚠️ Upload attempt {attempt + 1} to {srv} failed: {e}")
        except Exception as e:
            logger.warning(f"⚠️ Unexpected upload failure at {srv}: {e}")

    raise Exception("❌ All Gofile upload attempts failed. Try again later.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me an Instagram Reel/Post or YouTube video URL, and I'll download the content for you!")

async def download_content(url, context, msg):
    import yt_dlp
    parsed_url = urlparse(url)
    cleaned_url = urlunparse(parsed_url._replace(query=''))
    tmpdir = tempfile.mkdtemp()
    video_path = None
    thumbnail_path = None
    uploader_username = "Unknown"
    post_title = "No Title"

    # Create a simple progress tracker that doesn't interfere with bot operations
    progress_data = {"last_update": 0}

    def progress_hook(d):
        if d['status'] == 'downloading':
            current_time = time.monotonic()
            if (current_time - progress_data["last_update"]) < 3:  # Update every 3 seconds
                return
            
            _percent_str = d.get('_percent_str', 'N/A').strip()
            _speed_str = d.get('_speed_str', 'N/A').strip()
            _eta_str = d.get('_eta_str', 'N/A').strip()
            progress_message = f"Downloading: {_percent_str} at {_speed_str} (ETA: {_eta_str})"
            
            # Store progress info for potential async update
            progress_data["message"] = progress_message
            progress_data["last_update"] = current_time
            logger.info(progress_message)

        elif d['status'] == 'finished':
            logger.info("Download finished. Preparing content...")

    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'outtmpl': os.path.join(tmpdir, '%(id)s.%(ext)s'),
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'merge_output_format': 'mp4',
        'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
        'progress_hooks': [progress_hook],
    }

    try:
        logger.info(f"Attempting to download: {cleaned_url}")
        
        def _blocking_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(cleaned_url, download=True)

        # Update message before starting download
        try:
            await context.bot.edit_message_text(
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                text="📥 Starting download..."
            )
        except Exception as e:
            logger.warning(f"Failed to update message: {e}")

        info_dict = await asyncio.to_thread(_blocking_download)
        uploader_username = info_dict.get('uploader', info_dict.get('uploader_id', 'Unknown'))
        post_title = info_dict.get('title', 'No Title').strip()
        thumbnail_url = info_dict.get('thumbnail')

        # Update message after download
        try:
            await context.bot.edit_message_text(
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                text="🖼️ Processing thumbnail..."
            )
        except Exception as e:
            logger.warning(f"Failed to update message: {e}")

        if thumbnail_url:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.get(thumbnail_url)
                    response.raise_for_status()
                    original_thumbnail_path = os.path.join(tmpdir, "thumb.jpg")
                    with open(original_thumbnail_path, 'wb') as f:
                        f.write(response.content)

                    thumbnail_path = os.path.join(tmpdir, "thumb_cropped.jpg")
                    img = Image.open(original_thumbnail_path)
                    width, height = img.size
                    crop_size = min(width, height)
                    left = (width - crop_size) / 2
                    top = (height - crop_size) / 2
                    right = (width + crop_size) / 2
                    bottom = (height + crop_size) / 2
                    img = img.crop((left, top, right, bottom)).resize((320, 320), Image.LANCZOS)
                    img.save(thumbnail_path)
            except Exception as e:
                logger.warning(f"Thumbnail error: {e}")
                thumbnail_path = None

        video_path = info_dict.get('filepath')
        if not video_path and info_dict.get('requested_downloads'):
            video_path = info_dict['requested_downloads'][0].get('filepath')

        if not video_path or not os.path.exists(video_path):
            found_files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if f.endswith(('.mp4', '.mkv', '.webm'))]
            if found_files:
                video_path = found_files[0]

        # Rename file to sanitized post_title + extension
        if video_path and os.path.exists(video_path):
            ext = pathlib.Path(video_path).suffix
            safe_title = sanitize_filename(post_title)
            new_video_path = os.path.join(tmpdir, safe_title + ext)
            os.rename(video_path, new_video_path)
            video_path = new_video_path

        return video_path, uploader_username, post_title, tmpdir, thumbnail_path

    except Exception as e:
        logger.error(f"Download failed: {e}", exc_info=True)
        return None, uploader_username, post_title, tmpdir, None

async def url_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if "instagram.com/" not in url and "youtube.com/" not in url and "youtu.be/" not in url:
        return

    msg = await update.message.reply_text("🔗 Processing your link...")
    video_path, uploader_username, post_title, temp_dir, thumbnail_path = await download_content(url, context, msg)

    if video_path and os.path.exists(video_path):
        safe_title = escape_markdown(post_title)
        safe_uploader = escape_markdown(uploader_username)
        safe_url = escape_markdown(url)
        caption_text = f"*Title:* {safe_title}\n*By:* {safe_uploader}\n[Source Link]({safe_url})"

        try:
            await context.bot.edit_message_text(
                chat_id=msg.chat_id, 
                message_id=msg.message_id, 
                text="✅ Download complete! Sending video..."
            )
            file_size_mb = os.path.getsize(video_path) / (1024 * 1024)

            if file_size_mb > 49:
                await context.bot.edit_message_text(
                    chat_id=msg.chat_id, 
                    message_id=msg.message_id, 
                    text=f"📤 Video is too large ({file_size_mb:.2f}MB), uploading to Gofile..."
                )
                try:
                    gofile_link = upload_to_gofile(video_path)
                    await update.message.reply_text(f"✅ Uploaded to Gofile:\n{gofile_link}")
                except Exception as e:
                    await update.message.reply_text(f"❌ Failed to upload to Gofile:\n{str(e)}")
            else:
                with open(video_path, 'rb') as video_file:
                    if thumbnail_path and os.path.exists(thumbnail_path):
                        with open(thumbnail_path, 'rb') as thumb_file:
                            await update.message.reply_video(
                                video=video_file, 
                                caption=caption_text, 
                                parse_mode='MarkdownV2', 
                                thumbnail=thumb_file
                            )
                    else:
                        await update.message.reply_video(
                            video=video_file, 
                            caption=caption_text, 
                            parse_mode='MarkdownV2'
                        )
                        
            # Delete the status message after successful upload
            try:
                await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
            except Exception as e:
                logger.warning(f"Could not delete status message: {e}")
                
        except Exception as e:
            logger.error(f"Sending video failed: {e}", exc_info=True)
            await update.message.reply_text("⚠️ Error sending video.")
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
    else:
        await context.bot.edit_message_text(
            chat_id=msg.chat_id, 
            message_id=msg.message_id, 
            text="⚠️ Failed to download the content."
        )
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me an Instagram or YouTube video URL and I'll download it for you."
    )

def main():
    max_retries = 3
    retry_delay = 5  # seconds
    
    for attempt in range(max_retries):
        try:
            print(f"Attempt {attempt + 1} to start bot...")
            
            # Create the application with the bot token and timeouts  
            app = (
                Application.builder()
                .token(BOT_TOKEN)
                .connect_timeout(30)
                .read_timeout(30)
                .write_timeout(30)
                .pool_timeout(30)
                .get_updates_connect_timeout(42)
                .get_updates_read_timeout(42)
                .build()
            )
            
            # Add handlers
            app.add_handler(CommandHandler("start", start))
            app.add_handler(CommandHandler("help", help_handler))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_handler))

            print("Bot started successfully!")
            
            # Run polling with error handling
            app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                close_loop=False
            )
            break
            
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed: {e}")
            print(f"Attempt {attempt + 1} failed: {e}")
            
            if attempt < max_retries - 1:
                print(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                print("All attempts failed. Please check your bot token and internet connection.")
                print("You can test your token with:")
                print(f"curl -X GET 'https://api.telegram.org/bot{BOT_TOKEN}/getMe'")

if __name__ == "__main__":
    main()

