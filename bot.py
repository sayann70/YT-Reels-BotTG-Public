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
from urllib.parse import urlparse, urlunparse, parse_qs
from PIL import Image
import requests
import random
import pathlib
from ytmusicapi import YTMusic 

# Telegram bot token
BOT_TOKEN = "ENTER_YOUR_BOT_TOKEN_HERE"

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Helper Functions ---

def escape_markdown(text: str) -> str:
    """Escape text for MarkdownV2."""
    return re.sub(r'([_*\[\]()~`>#+=|{}.!\\-])', r'\\\1', text)

def sanitize_filename(name: str) -> str:
    """Sanitize filename to remove invalid characters."""
    return re.sub(r'[\\/*?:"<>|]', "_", name)

async def upload_to_gofile(file_path: str):
    """
    Uploads a file to Gofile asynchronously with a robust retry mechanism.
    """
    max_retries = 5
    base_delay = 5  # Base delay for retries in seconds

    async with httpx.AsyncClient(timeout=60.0) as client:
        for attempt in range(max_retries):
            try:
                # 1. Get the best server from Gofile API
                try:
                    api_response = await client.get("https://api.gofile.io/getServer", timeout=20)
                    api_response.raise_for_status()
                    server = api_response.json()["data"]["server"]
                    logger.info(f"Gofile recommended server: {server}")
                except Exception as e:
                    logger.warning(f"Could not get Gofile server, using fallback. Error: {e}")
                    server = f"store{random.randint(1, 9)}"

                # 2. Upload the file
                upload_url = f"https://{server}.gofile.io/uploadFile"
                with open(file_path, "rb") as f:
                    files = {"file": (os.path.basename(file_path), f)}
                    logger.info(f"Attempt {attempt + 1}: Uploading to {upload_url}...")
                    
                    upload_response = await client.post(upload_url, files=files, timeout=300) # 5 minute timeout for upload
                    upload_response.raise_for_status()
                    
                    data = upload_response.json()
                    if data["status"] == "ok":
                        logger.info("✅ Gofile upload successful!")
                        return data["data"]["downloadPage"]
                    else:
                        logger.warning(f"Gofile API returned an error: {data.get('status')}")

            except httpx.RequestError as e:
                logger.warning(f"⚠️ Upload attempt {attempt + 1} failed with network error: {e}")
            except Exception as e:
                logger.error(f"⚠️ An unexpected error occurred during upload attempt {attempt + 1}: {e}", exc_info=True)

            # If we are here, it means the attempt failed. Wait before retrying.
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)  # Exponential backoff
                logger.info(f"Retrying in {delay} seconds...")
                await asyncio.sleep(delay)

    raise Exception("❌ All Gofile upload attempts failed after multiple retries.")


# --- Download Functions ---

async def download_content(url, context, msg):
    """Downloads video content using yt-dlp."""
    import yt_dlp
    parsed_url = urlparse(url)
    cleaned_url = urlunparse(parsed_url._replace(query=''))
    tmpdir = tempfile.mkdtemp()
    video_path, thumbnail_path = None, None
    uploader_username, post_title = "Unknown", "No Title"
    
    progress_data = {"last_update": 0}

    def progress_hook(d):
        if d['status'] == 'downloading':
            current_time = time.monotonic()
            if (current_time - progress_data["last_update"]) > 3:
                _percent_str = d.get('_percent_str', 'N/A').strip()
                _speed_str = d.get('_speed_str', 'N/A').strip()
                _eta_str = d.get('_eta_str', 'N/A').strip()
                logger.info(f"Downloading: {_percent_str} at {_speed_str} (ETA: {_eta_str})")
                progress_data["last_update"] = current_time
        elif d['status'] == 'finished':
            logger.info("Download finished. Processing...")

    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'outtmpl': os.path.join(tmpdir, '%(id)s.%(ext)s'),
        'noplaylist': True, 'quiet': True, 'no_warnings': True,
        'merge_output_format': 'mp4',
        'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
        'progress_hooks': [progress_hook],
    }

    try:
        await context.bot.edit_message_text(chat_id=msg.chat_id, message_id=msg.message_id, text="📥 Starting download...")
        
        info_dict = await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(cleaned_url, download=True))
        
        uploader_username = info_dict.get('uploader', info_dict.get('uploader_id', 'Unknown'))
        post_title = info_dict.get('title', 'No Title').strip()
        thumbnail_url = info_dict.get('thumbnail')

        await context.bot.edit_message_text(chat_id=msg.chat_id, message_id=msg.message_id, text="🖼️ Processing thumbnail...")
        if thumbnail_url:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(thumbnail_url)
                original_thumbnail_path = os.path.join(tmpdir, "thumb.jpg")
                with open(original_thumbnail_path, 'wb') as f:
                    f.write(response.content)
                
                img = Image.open(original_thumbnail_path)
                width, height = img.size
                crop_size = min(width, height)
                left, top = (width - crop_size) / 2, (height - crop_size) / 2
                img = img.crop((left, top, left + crop_size, top + crop_size)).resize((320, 320), Image.LANCZOS)
                thumbnail_path = os.path.join(tmpdir, "thumb_cropped.jpg")
                img.save(thumbnail_path)

        video_path = info_dict.get('filepath') or (info_dict.get('requested_downloads') and info_dict['requested_downloads'][0].get('filepath'))
        if not video_path or not os.path.exists(video_path):
            video_path = next((os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if f.endswith(('.mp4', '.mkv', '.webm'))), None)

        if video_path:
            new_video_path = os.path.join(tmpdir, sanitize_filename(post_title) + pathlib.Path(video_path).suffix)
            os.rename(video_path, new_video_path)
            video_path = new_video_path
        
        return video_path, uploader_username, post_title, tmpdir, thumbnail_path

    except Exception as e:
        logger.error(f"Download failed: {e}", exc_info=True)
        return None, uploader_username, post_title, tmpdir, None

async def download_youtube_music(url: str, context: ContextTypes.DEFAULT_TYPE, msg):
    """Downloads a song from YouTube Music."""
    tmpdir = tempfile.mkdtemp()
    try:
        await context.bot.edit_message_text(chat_id=msg.chat_id, message_id=msg.message_id, text="🎵 Processing YouTube Music link...")
        
        ytmusic = await asyncio.to_thread(YTMusic)
        video_id = parse_qs(urlparse(url).query).get('v', [None])[0]
        if not video_id:
             path_parts = urlparse(url).path.split('/')
             if len(path_parts) >= 3 and path_parts[1] == 'watch':
                 video_id = path_parts[2]
        if not video_id:
            raise ValueError("Could not find video ID in URL.")

        song = await asyncio.to_thread(ytmusic.get_song, videoId=video_id)
        streaming_data = await asyncio.to_thread(ytmusic.get_streaming_data, videoId=video_id)
        
        stream_url = streaming_data['formats'][0]['url']
        title = song['videoDetails']['title']
        artist = song['videoDetails']['author']
        
        file_path = os.path.join(tmpdir, f"{sanitize_filename(title)}.mp3")

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.get(stream_url)
            response.raise_for_status()
            with open(file_path, 'wb') as f:
                f.write(response.content)

        return file_path, title, artist, tmpdir

    except Exception as e:
        logger.error(f"YouTube Music download failed: {e}", exc_info=True)
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None, None, None, None

# --- Telegram Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send me an Instagram, YouTube, or YouTube Music URL!")

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send a valid URL from Instagram, YouTube, or YouTube Music and I'll download the content for you.")

async def url_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    temp_dir = None
    msg = None

    try:
        if "music.youtube.com/" in url:
            msg = await update.message.reply_text("🔗 Processing your YouTube Music link...")
            file_path, title, artist, temp_dir = await download_youtube_music(url, context, msg)

            if file_path:
                await context.bot.edit_message_text(chat_id=msg.chat_id, message_id=msg.message_id, text="✅ Download complete! Sending audio...")
                caption_text = f"*Title:* {escape_markdown(title)}\n*By:* {escape_markdown(artist)}"
                
                with open(file_path, 'rb') as audio_file:
                    await update.message.reply_audio(audio=audio_file, caption=caption_text, parse_mode='MarkdownV2', title=title, performer=artist)
                
                await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
            else:
                await context.bot.edit_message_text(chat_id=msg.chat_id, message_id=msg.message_id, text="⚠️ Failed to download the YouTube Music content.")
        
        elif "instagram.com/" in url or "youtube.com/" in url or "youtu.be/" in url:
            msg = await update.message.reply_text("🔗 Processing your link...")
            video_path, uploader, title, temp_dir, thumb_path = await download_content(url, context, msg)

            if video_path:
                await context.bot.edit_message_text(chat_id=msg.chat_id, message_id=msg.message_id, text="✅ Download complete! Sending video...")
                file_size_mb = os.path.getsize(video_path) / (1024 * 1024)

                if file_size_mb > 49:
                    await context.bot.edit_message_text(chat_id=msg.chat_id, message_id=msg.message_id, text=f"📤 Video is large ({file_size_mb:.2f}MB), uploading to Gofile...")
                    gofile_link = await upload_to_gofile(video_path)
                    await update.message.reply_text(f"✅ Uploaded to Gofile:\n{gofile_link}")
                else:
                    caption = f"*Title:* {escape_markdown(title)}\n*By:* {escape_markdown(uploader)}\n[Source Link]({escape_markdown(url)})"
                    with open(video_path, 'rb') as video_file:
                        thumb_file_obj = open(thumb_path, 'rb') if thumb_path and os.path.exists(thumb_path) else None
                        await update.message.reply_video(video=video_file, caption=caption, parse_mode='MarkdownV2', thumbnail=thumb_file_obj)
                        if thumb_file_obj:
                            thumb_file_obj.close()
                
                await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
            else:
                await context.bot.edit_message_text(chat_id=msg.chat_id, message_id=msg.message_id, text="⚠️ Failed to download the content.")
        else:
            await update.message.reply_text("Please send a valid URL from Instagram, YouTube, or YouTube Music.")

    except Exception as e:
        logger.error(f"An error occurred in url_handler: {e}", exc_info=True)
        error_message = f"❌ An unexpected error occurred: {e}"
        if msg:
            try:
                await context.bot.edit_message_text(chat_id=msg.chat_id, message_id=msg.message_id, text=error_message)
            except:
                await update.message.reply_text(error_message)
        else:
            await update.message.reply_text(error_message)
            
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

# --- Main Bot Logic ---

def main():
    """Starts the bot."""
    print("Starting bot...")
    
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30).read_timeout(30).write_timeout(30)
        .build()
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_handler))

    print("Bot has started successfully!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    if BOT_TOKEN == "ENTER_YOUR_BOT_TOKEN_HERE":
        print("Please enter your bot token in the script!")
    else:
        main()