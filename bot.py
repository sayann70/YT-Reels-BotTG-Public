import os
import logging
import tempfile
import shutil
import time
import re
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import asyncio
import httpx
import pathlib
from dotenv import load_dotenv
import random
import yt_dlp
from PIL import Image

# --- Load .env file ---
load_dotenv()

# --- Configuration ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_PLAYLIST_SIZE = int(os.getenv("MAX_PLAYLIST_SIZE", "50"))
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "49"))
PROGRESS_UPDATE_INTERVAL = float(os.getenv("PROGRESS_UPDATE_INTERVAL", "3.0"))
# Optional: Path to a Netscape-formatted cookie file for Instagram.
INSTAGRAM_COOKIE_PATH = os.getenv("INSTAGRAM_COOKIE_PATH")

# --- Logging Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Helper Functions ---
def md2(text: str) -> str:
    """Escape for MarkdownV2 parse mode."""
    if not text:
        return ""
    escape_chars = r'_*\[\]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', str(text))

def sanitize_filename(name: str, max_length: int = 100) -> str:
    if not name: return "unknown_media"
    sanitized = re.sub(r'[\\/*?:"<>|]', "_", name)
    return sanitized[:max_length].strip() or "unknown_media"

def format_file_size(size_bytes: int) -> str:
    """Converts a file size in bytes to a human-readable string (KB, MB, GB)."""
    if size_bytes < 1024**2:
        return f"{size_bytes/1024:.1f} KB"
    if size_bytes < 1024**3:
        return f"{size_bytes/(1024**2):.1f} MB"
    return f"{size_bytes/(1024**3):.1f} GB"

async def upload_to_gofile(file_path: str):
    """Upload a file to Gofile.io (with retries)."""
    max_retries, base_delay = 5, 5
    async with httpx.AsyncClient(timeout=60.0) as client:
        for attempt in range(max_retries):
            try:
                # Get the best available Gofile server.
                try:
                    api_response = await client.get("https://api.gofile.io/getServer", timeout=20)
                    api_response.raise_for_status()
                    server = api_response.json()["data"]["server"]
                except Exception as e:
                    logger.warning(f"Could not get Gofile server, using fallback. Error: {e}")
                    server = f"store{random.randint(1, 9)}"

                # Perform the file upload.
                upload_url = f"https://{server}.gofile.io/uploadFile"
                with open(file_path, "rb") as f:
                    files = {"file": (os.path.basename(file_path), f)}
                    logger.info(f"Attempt {attempt + 1}: Uploading to {upload_url}...")
                    upload_response = await client.post(upload_url, files=files, timeout=300)
                    upload_response.raise_for_status()
                    data = upload_response.json()
                    if data.get("status") == "ok":
                        logger.info("✅ Gofile upload successful!")
                        return data["data"]["downloadPage"]
            except Exception as e:
                logger.error(f"⚠️ Gofile upload attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(base_delay * (2 ** attempt))
    raise Exception("❌ All Gofile upload attempts failed.")

# --- YouTube Music Format Selection (Inline Keyboard) ---
AUDIO_FORMATS = ["mp3", "flac", "wav"]

def get_format():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("MP3", callback_data="ytmusicfmt|mp3"),
         InlineKeyboardButton("FLAC", callback_data="ytmusicfmt|flac"),
         InlineKeyboardButton("WAV", callback_data="ytmusicfmt|wav")]
    ])

# --- Save pending YTM link for user on each message (stateless each time) ---
# Use context.user_data["pending_ytmusic_url"]

async def handle_youtube_music_audio_download(update, context, url, fmt):
    msg = await update.effective_message.reply_text("🎧 Downloading and Converting...")
    temp_dir = tempfile.mkdtemp()
    try:
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(temp_dir, "%(title)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": fmt,
                "preferredquality": "192",
            }]
        }
        info = await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=True))
        base = yt_dlp.YoutubeDL(ydl_opts).prepare_filename(info).rsplit(".", 1)[0]
        file_path = base + f".{fmt}"
        if not os.path.exists(file_path):
            await msg.edit_text("❌ Audio conversion failed.")
            return
        file_size = os.path.getsize(file_path)
        caption = (
            f"*Title:* {md2(info.get('title'))}\n"
            f"*By:* {md2(info.get('artist') or info.get('uploader','Unknown'))}\n"
            f"*Size:* {md2(format_file_size(file_size))}\n"
            f"[🔗 Source]({md2(url)})"
        )
        if file_size <= MAX_FILE_SIZE_MB * 1024 * 1024:
            with open(file_path, "rb") as audio_file:
                await update.effective_message.reply_audio(
                    audio=audio_file,
                    caption=caption,
                    parse_mode="MarkdownV2",
                    title=info.get("title"),
                    performer=info.get("artist") or info.get("uploader")
                )
        else:
            upload_url = await upload_to_gofile(file_path)
            caption += f"\n[➡️ Download from Gofile]({md2(upload_url)})"
            await update.effective_message.reply_text(caption, parse_mode="MarkdownV2", disable_web_page_preview=True)
        await msg.delete()
    except Exception as e:
        logger.error(f"YT Music download/conversion error: {e}", exc_info=True)
        await msg.edit_text("❌ Failed to download from YouTube Music.")
    finally:
        try:
            shutil.rmtree(temp_dir)
        except Exception: pass

async def ytmusic_format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the user's button pick for YT Music format."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|", 1)
    if len(parts) < 2 or parts[0] != "ytmusicfmt":
        await query.edit_message_text("❌ Something went wrong.")
        return
    fmt = parts[1]
    url = context.user_data.get("pending_ytmusic_url")
    if not url:
        await query.edit_message_text("❌ Could not find the original link.")
        return
    await query.edit_message_text(f"✅ Format: {fmt.upper()}\nDownload starting...", reply_markup=None)
    await handle_youtube_music_audio_download(update, context, url, fmt)

async def download_single_video(url: str, context: ContextTypes.DEFAULT_TYPE, status_msg=None, cookie_path: str = None):
    """Downloads a single video from a given URL using yt-dlp."""
    tmpdir = tempfile.mkdtemp()
    progress_data = {"last_update": 0}
    def progress_hook(d):
        if d['status'] == 'downloading':
            current_time = time.monotonic()
            if (current_time - progress_data["last_update"]) > PROGRESS_UPDATE_INTERVAL:
                percent = d.get('_percent_str', 'N/A').strip()
                speed = d.get('_speed_str', 'N/A').strip()
                eta = d.get('_eta_str', 'N/A').strip()
                logger.info(f"Downloading: {percent} at {speed} (ETA: {eta})")
                progress_data["last_update"] = current_time

    # yt-dlp options for downloading the best quality video and audio.
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

    # If a cookie path is provided (for Instagram), add it to the options.
    if cookie_path and os.path.exists(cookie_path):
        ydl_opts['cookiefile'] = cookie_path
    try:
        if status_msg:
            # Inform the user that the download is starting.
            await context.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text="📥 Starting download...")

        # Run the blocking yt-dlp download in a separate thread.
        info_dict = await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=True))

        # Locate the downloaded file path.
        video_path = info_dict.get('filepath')
        if not video_path or not os.path.exists(video_path):
            video_files = [f for f in os.listdir(tmpdir) if f.endswith(('.mp4', '.mkv', '.webm', '.avi'))]
            if video_files:
                video_path = os.path.join(tmpdir, video_files[0])
            else:
                raise Exception("No video file found after download")

        # Extract metadata.
        uploader = info_dict.get('uploader', info_dict.get('uploader_id', 'Unknown'))
        title = info_dict.get('title', 'No Title')
        thumbnail_url = info_dict.get('thumbnail')

        # Process the thumbnail: download, crop to a square, and resize.
        thumbnail_path = None
        if thumbnail_url:
            try:
                if status_msg:
                    await context.bot.edit_message_text(chat_id=status_msg.chat_id, message_id=status_msg.message_id, text="🖼️ Processing thumbnail...")
                async with httpx.AsyncClient(timeout=20.0) as client:
                    response = await client.get(thumbnail_url)
                    response.raise_for_status()
                    original_thumbnail_path = os.path.join(tmpdir, "thumb.jpg")
                    with open(original_thumbnail_path, 'wb') as f:
                        f.write(response.content)
                    with Image.open(original_thumbnail_path) as img:
                        w, h = img.size
                        crop_size = min(w, h)
                        left, top = (w - crop_size) // 2, (h - crop_size) // 2
                        img_cropped = img.crop((left, top, left + crop_size, top + crop_size))
                        img_resized = img_cropped.resize((320, 320), Image.LANCZOS)
                        thumbnail_path = os.path.join(tmpdir, "thumb_cropped.jpg")
                        img_resized.save(thumbnail_path, "JPEG")
            except Exception as e:
                logger.warning(f"Thumbnail processing failed: {e}")

        # Rename the video file to a sanitized version of its title.
        if video_path:
            file_ext = pathlib.Path(video_path).suffix
            new_video_path = os.path.join(tmpdir, sanitize_filename(title) + file_ext)
            try:
                os.rename(video_path, new_video_path)
                video_path = new_video_path
            except OSError:
                pass
        return video_path, uploader, title, tmpdir, thumbnail_path
    except Exception as e:
        logger.error(f"Download failed for URL '{url}'. Error: {e}", exc_info=True)
        # Clean up the temporary directory on failure.
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None, None, None, None, None

async def process_playlist(url: str, playlist_info: dict, context: ContextTypes.DEFAULT_TYPE, msg):
    """Processes and downloads all videos from a given playlist URL."""
    playlist_title = playlist_info.get('title', 'Unnamed Playlist')
    videos = playlist_info.get('entries', [])
    original_total_videos = len(videos)

    # Enforce the maximum playlist size limit.
    if original_total_videos > MAX_PLAYLIST_SIZE:
        await context.bot.edit_message_text(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            text=f"⚠️ Playlist too large! Max {MAX_PLAYLIST_SIZE} videos allowed. Found {original_total_videos} videos."
        )
        return
    videos = videos[:MAX_PLAYLIST_SIZE]
    total_videos = len(videos)
    await context.bot.edit_message_text(
        chat_id=msg.chat_id,
        message_id=msg.message_id,
        text=f"✅ Playlist detected: {playlist_title}\nFound {original_total_videos} videos.\nStarting download..."
    )
    successful_downloads = 0
    failed_downloads = 0
    for i, video_entry in enumerate(videos, 1):
        video_url = video_entry.get('url') or video_entry.get('webpage_url')
        video_title = video_entry.get('title', 'Unknown Title')
        if not video_url:
            logger.warning(f"No URL found for video {i}: {video_title}")
            failed_downloads += 1
            continue

        # Send a status message for the current video being downloaded.
        status_msg = await context.bot.send_message(
            chat_id=msg.chat_id,
            text=f"📥 Downloading video {i}/{total_videos}: {video_title[:50]}{'...' if len(video_title) > 50 else ''}"
        )
        try:
            video_path, uploader, title, temp_dir, thumb_path = await download_single_video(video_url, context, status_msg)

            # The result handler sends the video/Gofile link and cleans up.
            success = await _handle_video_result(
                video_path, uploader, title, temp_dir, thumb_path,
                video_url, context, status_msg, chat_id=msg.chat_id, prefix=f"{i}/{total_videos} "
            )
            if success:
                successful_downloads += 1
            else:
                failed_downloads += 1
            await asyncio.sleep(2)  # To avoid Telegram flood limits
        except Exception as e:
            failed_downloads += 1
            logger.error(f"Failed processing video {i} from playlist. URL: {video_url}, Error: {e}", exc_info=True)
            await context.bot.edit_message_text(
                chat_id=status_msg.chat_id,
                message_id=status_msg.message_id,
                text=f"❌ Error on video {i}/{total_videos}"
            )

    # Send a final summary message to the user.
    summary_text = f"✅ Playlist {playlist_title} complete!\n📊 Downloaded: {successful_downloads}/{total_videos}"
    if failed_downloads > 0:
        summary_text += f"\n⚠️ Failed: {failed_downloads}"
    await context.bot.send_message(chat_id=msg.chat_id, text=summary_text)

async def _handle_video_result(video_path, uploader, title, temp_dir, thumb_path, url, context, status_msg, chat_id, prefix=""):
    try:
        if video_path and os.path.exists(video_path):
            file_size_bytes = os.path.getsize(video_path)
            caption = (
                f"*{prefix}Title:* {md2(title)}\n"
                f"*By:* {md2(uploader)}\n"
                f"*Size:* {md2(format_file_size(file_size_bytes))}\n"
                f"[🔗 Source]({md2(url)})"
            )
            file_size_mb = file_size_bytes / (1024 * 1024)
            if file_size_mb > MAX_FILE_SIZE_MB:
                await context.bot.edit_message_text(
                    chat_id=status_msg.chat_id,
                    message_id=status_msg.message_id,
                    text=f"📤 Large file ({file_size_mb:.1f}MB) - uploading to Gofile."
                )
                gofile_link = await upload_to_gofile(video_path)
                caption += f"\n[➡️ Download from Gofile]({md2(gofile_link)})"
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=caption,
                    parse_mode='MarkdownV2',
                    disable_web_page_preview=True
                )
            else:
                await context.bot.edit_message_text(
                    chat_id=status_msg.chat_id,
                    message_id=status_msg.message_id,
                    text="✅ Sending video..."
                )
                with open(video_path, 'rb') as video_file:
                    thumb_obj = None
                    if thumb_path and os.path.exists(thumb_path):
                        thumb_obj = open(thumb_path, 'rb')
                    try:
                        await context.bot.send_video(
                            chat_id=chat_id,
                            video=video_file,
                            caption=caption,
                            parse_mode='MarkdownV2',
                            thumbnail=thumb_obj,
                            write_timeout=60
                        )
                    finally:
                        if thumb_obj:
                            thumb_obj.close()

            # Delete the "Downloading..." status message.
            await context.bot.delete_message(
                chat_id=status_msg.chat_id,
                message_id=status_msg.message_id
            )
            return True
        else:
            # Handle the case where the download failed and no file was produced.
            await context.bot.edit_message_text(
                chat_id=status_msg.chat_id,
                message_id=status_msg.message_id,
                text="⚠️ Failed to download the content. The link may be private, invalid, or temporarily unavailable."
            )
            return False
    except Exception as e:
        logger.error(f"Error in _handle_video_result: {e}", exc_info=True)
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id,
            message_id=status_msg.message_id,
            text="❌ An error occurred while processing the video."
        )
        return False
    finally:
        # Crucial cleanup step: always remove the temporary directory.
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

# --- Telegram Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command with a welcome message."""
    welcome_text = (
        "👋 *Welcome to the Media Downloader Bot\\!*\n\n"
        "*Supported Platforms:*\n"
        "• YouTube videos \\& playlists\n"
        "• YouTube Music\n"
        "• Instagram\n\n"
        "*How to Use:*\n"
        "Just send me a link and I\\'ll download it for you\\!\n\n"
        "*Playlist Limit:*\n"
        f"The bot will process a maximum of *{MAX_PLAYLIST_SIZE}* videos from a single playlist\\."
    )
    await update.message.reply_text(welcome_text, parse_mode='MarkdownV2')

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📱 *Supported platforms:*\n"
        "• YouTube videos \\& playlists\n"
        "• YouTube Music\n"
        "• Instagram\n\n"
        f"⚙️ *Settings:*\n"
        f"• Max playlist videos: *{MAX_PLAYLIST_SIZE}*\n"
        f"• Max file size: *{MAX_FILE_SIZE_MB}MB*\n\n"
        "⚠️ _Note: Private or age\\-restricted content cannot be downloaded\\._"
    )
    await update.message.reply_text(help_text, parse_mode='MarkdownV2')

async def url_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main handler for text/URL input."""
    if not update.message or not update.message.text:
        return
    url = update.message.text.strip()
    # Always set pending link for the button handler on YT Music
    if "music.youtube.com/" in url:
        context.user_data["pending_ytmusic_url"] = url
        await update.message.reply_text(
            "🎵 Choose audio format:",
            reply_markup=get_format()
        )
        return

    if not re.search(r"(instagram\.com|youtube\.com|youtu\.be|music\.youtube\.com)", url):
        logger.info(f"Ignoring non-URL message from user {update.message.from_user.id}")
        return
    status_msg = await update.message.reply_text("🔗 Processing your link...")
    temp_dir = None
    try:
        if "youtube.com/" in url or "youtu.be/" in url:
            ydl_opts_check = {'quiet': True, 'extract_flat': True, 'force_generic_extractor': False}
            info_dict = await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(ydl_opts_check).extract_info(url, download=False))
            if info_dict.get('_type') == 'playlist':
                await process_playlist(url, info_dict, context, status_msg)
                return
            else:
                video_path, uploader, title, temp_dir, thumb_path = await download_single_video(url, context, status_msg)
                await _handle_video_result(video_path, uploader, title, temp_dir, thumb_path, url, context, status_msg, update.message.chat_id)
        elif "instagram.com" in url:
            video_path, uploader, title, temp_dir, thumb_path = await download_single_video(
                url, context, status_msg, cookie_path=INSTAGRAM_COOKIE_PATH
            )
            await _handle_video_result(
                video_path, uploader, title, temp_dir, thumb_path,
                url, context, status_msg, update.message.chat_id
            )
    except Exception as e:
        logger.error(f"Error in url_handler: {e}", exc_info=True)
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id,
            message_id=status_msg.message_id,
            text="❌ An unexpected error occurred. Please try again later."
        )
    finally:
        # This cleanup block runs regardless of success or failure, ensuring no temp files are left behind.
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

# --- Run Bot ---
def main():
    print("🤖 Starting Telegram Media Downloader Bot...")
    if not BOT_TOKEN:
        print("❌ ERROR: Please set your bot token in the BOT_TOKEN environment variable!")
        return

    # Build the bot application.
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )

    # Register the command and message handlers.
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_handler))
    app.add_handler(CallbackQueryHandler(ytmusic_format_callback, pattern=r"^ytmusicfmt\|"))
    print("✅ Bot started successfully! Send /start to begin.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()