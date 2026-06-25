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
import instaloader
from urllib.parse import urlparse
import json
from datetime import datetime


# --- Load .env file ---
load_dotenv()


# --- Configuration ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_PLAYLIST_SIZE = int(os.getenv("MAX_PLAYLIST_SIZE", "50"))
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "49"))
PROGRESS_UPDATE_INTERVAL = float(os.getenv("PROGRESS_UPDATE_INTERVAL", "3.0"))
# Instagram credentials (optional, for private content)
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD")


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


def extract_instagram_shortcode(url: str) -> str:
    """Extract shortcode from Instagram URL."""
    patterns = [
        r'instagram\.com/p/([^/?]+)',
        r'instagram\.com/reel/([^/?]+)',
        r'instagram\.com/tv/([^/?]+)',
        r'instagram\.com/stories/[^/]+/([^/?]+)'
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    return None


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


# --- Instagram Download Functions ---
async def download_instagram_content(url: str, context: ContextTypes.DEFAULT_TYPE, status_msg=None):
    """Downloads Instagram content using instaloader."""
    temp_dir = tempfile.mkdtemp()

    try:
        # Extract shortcode from URL
        shortcode = extract_instagram_shortcode(url)
        if not shortcode:
            raise Exception("Could not extract Instagram shortcode from URL")

        if status_msg:
            await context.bot.edit_message_text(
                chat_id=status_msg.chat_id,
                message_id=status_msg.message_id,
                text="📱 Connecting to Instagram..."
            )

        # Initialize Instaloader with more conservative settings
        L = instaloader.Instaloader(
            download_videos=True,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            dirname_pattern=temp_dir,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            request_timeout=30,
            max_connection_attempts=3
        )

        # Add delay to avoid rate limiting
        await asyncio.sleep(2)

        # Login if credentials are provided
        if INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD:
            try:
                await asyncio.to_thread(L.login, INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
                logger.info("✅ Logged into Instagram")
                await asyncio.sleep(1)  # Small delay after login
            except Exception as e:
                logger.warning(f"Instagram login failed: {e}")

        if status_msg:
            await context.bot.edit_message_text(
                chat_id=status_msg.chat_id,
                message_id=status_msg.message_id,
                text="📥 Downloading Instagram content..."
            )

        # Get post from shortcode with retry logic
        max_retries = 3
        post = None
        for attempt in range(max_retries):
            try:
                post = await asyncio.to_thread(instaloader.Post.from_shortcode, L.context, shortcode)
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(f"Attempt {attempt + 1} failed: {e}, retrying...")
                    await asyncio.sleep(5 * (attempt + 1))  # Exponential backoff
                else:
                    raise e

        if not post:
            raise Exception("Failed to fetch Instagram post after multiple attempts")

        # Download the post
        await asyncio.to_thread(L.download_post, post, target='')

        # Find downloaded files
        downloaded_files = []
        for root, dirs, files in os.walk(temp_dir):
            for file in files:
                if file.endswith(('.jpg', '.png', '.mp4', '.webp')):
                    downloaded_files.append(os.path.join(root, file))

        if not downloaded_files:
            raise Exception("No media files found after download")

        # Extract metadata safely
        caption = getattr(post, 'caption', None) or "No caption available"
        username = getattr(post, 'owner_username', 'Unknown')
        likes = getattr(post, 'likes', 0)

        # Safe date handling
        try:
            date = post.date_local.strftime("%Y-%m-%d %H:%M")
        except:
            date = "Unknown"

        # Determine post type safely
        post_type = "Photo"  # Default
        try:
            if hasattr(post, 'is_video') and post.is_video:
                post_type = "Video"
            # Check for carousel (sidecar) safely
            try:
                sidecar_nodes = list(post.get_sidecar_nodes())
                if len(sidecar_nodes) > 1:
                    post_type = "Carousel"
            except:
                # If sidecar check fails, keep default type
                pass
        except Exception as e:
            logger.warning(f"Could not determine post type: {e}")

        # Prepare metadata text
        metadata = (
            f"*📱 Instagram {post_type}*\n"
            f"*👤 By:* {md2(username)}\n"
            f"*📅 Date:* {md2(date)}\n"
            f"*❤️ Likes:* {md2(str(likes))}\n"
            f"*📝 Caption:* {md2(caption[:200] + ('...' if len(caption) > 200 else ''))}\n"
            f"[🔗 Source]({md2(url)})"
        )

        return downloaded_files, metadata, temp_dir

    except Exception as e:
        logger.error(f"Instagram download failed: {e}", exc_info=True)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None, None, None


async def download_instagram_fallback(url: str, context: ContextTypes.DEFAULT_TYPE, status_msg=None):
    """Fallback Instagram download using yt-dlp."""
    temp_dir = tempfile.mkdtemp()

    try:
        if status_msg:
            await context.bot.edit_message_text(
                chat_id=status_msg.chat_id,
                message_id=status_msg.message_id,
                text="🔄 Trying alternative download method..."
            )

        # yt-dlp options for Instagram
        ydl_opts = {
            'format': 'best',
            'outtmpl': os.path.join(temp_dir, '%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
        }

        # Add cookies if available (you can add Instagram cookie file path to .env)
        instagram_cookie_path = os.getenv("INSTAGRAM_COOKIE_PATH")
        if instagram_cookie_path and os.path.exists(instagram_cookie_path):
            ydl_opts['cookiefile'] = instagram_cookie_path

        # Download using yt-dlp
        info_dict = await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=True))

        # Find downloaded files
        downloaded_files = []
        for root, dirs, files in os.walk(temp_dir):
            for file in files:
                if file.endswith(('.jpg', '.png', '.mp4', '.webp', '.jpeg')):
                    downloaded_files.append(os.path.join(root, file))

        if not downloaded_files:
            raise Exception("No files downloaded with fallback method")

        # Extract basic metadata from yt-dlp
        title = info_dict.get('title', 'Instagram Content')
        uploader = info_dict.get('uploader', 'Unknown')
        upload_date = info_dict.get('upload_date', '')

        # Format date
        formatted_date = "Unknown"
        if upload_date:
            try:
                date_obj = datetime.strptime(upload_date, '%Y%m%d')
                formatted_date = date_obj.strftime('%Y-%m-%d')
            except:
                formatted_date = upload_date

        # Prepare metadata
        metadata = (
            f"*📱 Instagram Content*\n"
            f"*👤 By:* {md2(uploader)}\n"
            f"*📅 Date:* {md2(formatted_date)}\n"
            f"*📝 Title:* {md2(title[:200] + ('...' if len(title) > 200 else ''))}\n"
            f"[🔗 Source]({md2(url)})"
        )

        return downloaded_files, metadata, temp_dir

    except Exception as e:
        logger.error(f"Instagram fallback download failed: {e}", exc_info=True)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return None, None, None


async def handle_instagram_content(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, status_msg):
    """Handle Instagram content download with fallback."""
    try:
        # First attempt: Use instaloader
        downloaded_files, metadata, temp_dir = await download_instagram_content(url, context, status_msg)

        # If instaloader fails, try yt-dlp as fallback
        if not downloaded_files:
            logger.info("Instaloader failed, trying yt-dlp fallback...")
            downloaded_files, metadata, temp_dir = await download_instagram_fallback(url, context, status_msg)

        if not downloaded_files:
            await context.bot.edit_message_text(
                chat_id=status_msg.chat_id,
                message_id=status_msg.message_id,
                text="❌ Failed to download Instagram content. The post might be private, deleted, or temporarily unavailable."
            )
            return

        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id,
            message_id=status_msg.message_id,
            text="📤 Sending Instagram content..."
        )

        # Send each file
        for i, file_path in enumerate(downloaded_files):
            file_size = os.path.getsize(file_path)
            file_caption = f"{metadata}" if i == 0 else f"*Part {i+1}/{len(downloaded_files)}*\n[🔗 Source]({md2(url)})"

            if file_size <= MAX_FILE_SIZE_MB * 1024 * 1024:
                if file_path.lower().endswith(('.mp4', '.mov', '.avi')):
                    # Send as video
                    with open(file_path, 'rb') as video_file:
                        await context.bot.send_video(
                            chat_id=update.effective_chat.id,
                            video=video_file,
                            caption=file_caption,
                            parse_mode='MarkdownV2',
                            write_timeout=60
                        )
                else:
                    # Send as photo
                    with open(file_path, 'rb') as photo_file:
                        await context.bot.send_photo(
                            chat_id=update.effective_chat.id,
                            photo=photo_file,
                            caption=file_caption,
                            parse_mode='MarkdownV2'
                        )
            else:
                # Upload to Gofile for large files
                upload_url = await upload_to_gofile(file_path)
                file_caption += f"\n[➡️ Download from Gofile]({md2(upload_url)})"
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=file_caption,
                    parse_mode='MarkdownV2',
                    disable_web_page_preview=True
                )

            # Small delay between multiple files
            if len(downloaded_files) > 1 and i < len(downloaded_files) - 1:
                await asyncio.sleep(1)

        # Delete status message
        await context.bot.delete_message(
            chat_id=status_msg.chat_id,
            message_id=status_msg.message_id
        )

    except Exception as e:
        logger.error(f"Error handling Instagram content: {e}", exc_info=True)
        await context.bot.edit_message_text(
            chat_id=status_msg.chat_id,
            message_id=status_msg.message_id,
            text="❌ An error occurred while processing Instagram content."
        )
    finally:
        if 'temp_dir' in locals() and temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


# --- YouTube Music Format Selection (Inline Keyboard) ---
AUDIO_FORMATS = ["mp3", "flac", "wav"]


def get_ytmusic_format_keyboard():
    """Returns inline keyboard for YouTube Music format selection."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("MP3", callback_data="ytmusicfmt|mp3"),
         InlineKeyboardButton("FLAC", callback_data="ytmusicfmt|flac"),
         InlineKeyboardButton("WAV", callback_data="ytmusicfmt|wav")]
    ])


# --- YouTube Video/Playlist Format Selection (Inline Keyboard) ---
def get_youtube_format_keyboard():
    """Returns inline keyboard for YouTube video/playlist format selection."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📹 Download Video", callback_data="ytfmt|video")],
        [InlineKeyboardButton("🎵 Download Audio", callback_data="ytfmt|audio")]
    ])


def get_youtube_audio_format_keyboard():
    """Returns inline keyboard for YouTube audio format selection."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("MP3", callback_data="ytaudiofmt|mp3"),
         InlineKeyboardButton("FLAC", callback_data="ytaudiofmt|flac"),
         InlineKeyboardButton("WAV", callback_data="ytaudiofmt|wav")]
    ])


# --- Save pending YouTube link for user on each message (stateless each time) ---
# Use context.user_data["pending_ytmusic_url"] and context.user_data["pending_youtube_url"]


async def handle_youtube_music_audio_download(update, context, url, fmt):
    """Handles YouTube Music audio download with specified format."""
    msg = await update.effective_message.reply_text("🎧 Downloading and Converting...")
    temp_dir = tempfile.mkdtemp()
    try:
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'writethumbnail': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': fmt,
                'preferredquality': '192',
            }, {
                'key': 'EmbedThumbnail',
            }, {
                'key': 'FFmpegMetadata',
            }],
        }
        info = await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=True))
        base = yt_dlp.YoutubeDL(ydl_opts).prepare_filename(info).rsplit(".", 1)[0]
        file_path = base + f".{fmt}"
        if not os.path.exists(file_path):
            # Fallback in case the extension is different
            found_files = [f for f in os.listdir(temp_dir) if f.endswith(f'.{fmt}')]
            if not found_files:
                await msg.edit_text("❌ Audio conversion failed.")
                return
            file_path = os.path.join(temp_dir, found_files[0])

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


async def handle_youtube_audio_download(update, context, url, fmt, is_playlist=False):
    """Handles YouTube audio download with specified format."""
    if is_playlist:
        # For playlists, we need to handle each video separately
        await process_audio_playlist(url, context, update.effective_message, fmt)
    else:
        # For single videos
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
                f"*By:* {md2(info.get('uploader','Unknown'))}\n"
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
                        performer=info.get("uploader")
                    )
            else:
                upload_url = await upload_to_gofile(file_path)
                caption += f"\n[➡️ Download from Gofile]({md2(upload_url)})"
                await update.effective_message.reply_text(caption, parse_mode="MarkdownV2", disable_web_page_preview=True)
            await msg.delete()
        except Exception as e:
            logger.error(f"YouTube audio download/conversion error: {e}", exc_info=True)
            await msg.edit_text("❌ Failed to download audio from YouTube.")
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


async def youtube_format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the user's button pick for YouTube video/audio format."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|", 1)
    if len(parts) < 2 or parts[0] != "ytfmt":
        await query.edit_message_text("❌ Something went wrong.")
        return

    format_type = parts[1]
    url = context.user_data.get("pending_youtube_url")
    is_playlist = context.user_data.get("pending_youtube_is_playlist", False)

    if not url:
        await query.edit_message_text("❌ Could not find the original link.")
        return

    if format_type == "video":
        await query.edit_message_text("✅ Video format selected\nDownload starting...", reply_markup=None)
        # Handle video download (existing functionality)
        if is_playlist:
            # Extract playlist info again for video download
            ydl_opts_check = {'quiet': True, 'extract_flat': True, 'force_generic_extractor': False}
            info_dict = await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(ydl_opts_check).extract_info(url, download=False))
            await process_playlist(url, info_dict, context, query.message)
        else:
            status_msg = query.message
            video_path, uploader, title, temp_dir, thumb_path = await download_single_video(url, context, status_msg)
            await _handle_video_result(video_path, uploader, title, temp_dir, thumb_path, url, context, status_msg, query.message.chat_id)

    elif format_type == "audio":
        # Store the URL and playlist info for audio format selection
        context.user_data["pending_youtube_audio_url"] = url
        context.user_data["pending_youtube_audio_is_playlist"] = is_playlist
        await query.edit_message_text("🎵 Choose audio format:", reply_markup=get_youtube_audio_format_keyboard())


async def youtube_audio_format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the user's button pick for YouTube audio format."""
    query = update.callback_query
    await query.answer()
    parts = query.data.split("|", 1)
    if len(parts) < 2 or parts[0] != "ytaudiofmt":
        await query.edit_message_text("❌ Something went wrong.")
        return

    fmt = parts[1]
    url = context.user_data.get("pending_youtube_audio_url")
    is_playlist = context.user_data.get("pending_youtube_audio_is_playlist", False)

    if not url:
        await query.edit_message_text("❌ Could not find the original link.")
        return

    await query.edit_message_text(f"✅ Audio Format: {fmt.upper()}\nDownload starting...", reply_markup=None)
    await handle_youtube_audio_download(update, context, url, fmt, is_playlist)


async def process_audio_playlist(url: str, context: ContextTypes.DEFAULT_TYPE, msg, fmt: str):
    """Processes and downloads all audio from a given playlist URL."""
    # Extract playlist info
    ydl_opts_check = {'quiet': True, 'extract_flat': True, 'force_generic_extractor': False}
    playlist_info = await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(ydl_opts_check).extract_info(url, download=False))

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
        text=f"✅ Audio Playlist detected: {playlist_title}\nFound {original_total_videos} videos.\nStarting audio download..."
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

        # Send a status message for the current audio being downloaded.
        status_msg = await context.bot.send_message(
            chat_id=msg.chat_id,
            text=f"🎧 Downloading audio {i}/{total_videos}: {video_title[:50]}{'...' if len(video_title) > 50 else ''}"
        )

        try:
            temp_dir = tempfile.mkdtemp()
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

            info = await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(video_url, download=True))
            base = yt_dlp.YoutubeDL(ydl_opts).prepare_filename(info).rsplit(".", 1)[0]
            file_path = base + f".{fmt}"

            if os.path.exists(file_path):
                file_size = os.path.getsize(file_path)
                caption = (
                    f"*{i}/{total_videos} Title:* {md2(info.get('title'))}\n"
                    f"*By:* {md2(info.get('uploader','Unknown'))}\n"
                    f"*Size:* {md2(format_file_size(file_size))}\n"
                    f"[🔗 Source]({md2(video_url)})"
                )

                if file_size <= MAX_FILE_SIZE_MB * 1024 * 1024:
                    with open(file_path, "rb") as audio_file:
                        await context.bot.send_audio(
                            chat_id=msg.chat_id,
                            audio=audio_file,
                            caption=caption,
                            parse_mode="MarkdownV2",
                            title=info.get("title"),
                            performer=info.get("uploader")
                        )
                else:
                    upload_url = await upload_to_gofile(file_path)
                    caption += f"\n[➡️ Download from Gofile]({md2(upload_url)})"
                    await context.bot.send_message(
                        chat_id=msg.chat_id,
                        text=caption,
                        parse_mode="MarkdownV2",
                        disable_web_page_preview=True
                    )
                successful_downloads += 1
            else:
                failed_downloads += 1
                await context.bot.edit_message_text(
                    chat_id=status_msg.chat_id,
                    message_id=status_msg.message_id,
                    text=f"❌ Error on audio {i}/{total_videos}"
                )
                continue

            # Clean up temp directory
            shutil.rmtree(temp_dir, ignore_errors=True)

            # Delete the status message
            await context.bot.delete_message(
                chat_id=status_msg.chat_id,
                message_id=status_msg.message_id
            )

            await asyncio.sleep(2)  # To avoid Telegram flood limits

        except Exception as e:
            failed_downloads += 1
            logger.error(f"Failed processing audio {i} from playlist. URL: {video_url}, Error: {e}", exc_info=True)
            await context.bot.edit_message_text(
                chat_id=status_msg.chat_id,
                message_id=status_msg.message_id,
                text=f"❌ Error on audio {i}/{total_videos}"
            )
            if 'temp_dir' in locals():
                shutil.rmtree(temp_dir, ignore_errors=True)

    # Send a final summary message to the user.
    summary_text = f"✅ Audio Playlist {playlist_title} complete!\n📊 Downloaded: {successful_downloads}/{total_videos}"
    if failed_downloads > 0:
        summary_text += f"\n⚠️ Failed: {failed_downloads}"
    await context.bot.send_message(chat_id=msg.chat_id, text=summary_text)


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
        text=f"✅ Video Playlist detected: {playlist_title}\nFound {original_total_videos} videos.\nStarting download..."
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
    summary_text = f"✅ Video Playlist {playlist_title} complete!\n📊 Downloaded: {successful_downloads}/{total_videos}"
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
        "• Instagram \\(posts, reels, stories\\)\n\n"
        "*How to Use:*\n"
        "Just send me a link and I\\'ll download it for you\\!\n\n"
        "*Instagram Features:*\n"
        "• Downloads videos, photos, and carousels\n"
        "• Includes metadata \\(caption, likes, date\\)\n"
        "• Supports only public content\n\n"
        "*YouTube Features:*\n"
        "• Choose between video or audio for YouTube links\n"
        "• Multiple audio formats \\(MP3, FLAC, WAV\\)\n"
        "• Automatic playlist detection\n\n"
        "*Playlist Limit:*\n"
        f"The bot will process a maximum of *{MAX_PLAYLIST_SIZE}* videos from a single playlist\\."
    )
    await update.message.reply_text(welcome_text, parse_mode='MarkdownV2')


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📱 *Supported platforms:*\n"
        "• YouTube videos \\& playlists\n"
        "• YouTube Music\n"
        "• Instagram \\(posts, reels, stories, carousels\\)\n\n"
        "🎵 *YouTube Features:*\n"
        "• Choose Video or Audio download\n"
        "• Audio formats: MP3, FLAC, WAV\n"
        "• Playlist support for both formats\n\n"
        "📸 *Instagram Features:*\n"
        "• Download posts, reels, stories\n"
        "• Carousel \\(multiple photos/videos\\) support\n"
        "• Rich metadata \\(caption, likes, date, author\\)\n"
        f"⚙️ *Settings:*\n"
        f"• Max playlist videos: *{MAX_PLAYLIST_SIZE}*\n"
        f"• Max file size: *{MAX_FILE_SIZE_MB}MB*\n\n"
        "🔐 *Instagram Login:*\n"
        f"• Login status: {'✅ Configured' if INSTAGRAM_USERNAME else '❌ Not configured'}\n"
        "• Required for private content and stories\n\n"
        "⚠️ _Note: Some private or age\\-restricted content cannot be downloaded\\._"
    )
    await update.message.reply_text(help_text, parse_mode='MarkdownV2')


async def url_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main handler for text/URL input."""
    if not update.message or not update.message.text:
        return
    url = update.message.text.strip()

    # Handle YouTube Music links (direct audio format selection)
    if "music.youtube.com/" in url:
        context.user_data["pending_ytmusic_url"] = url
        await update.message.reply_text(
            "🎵 Choose audio format:",
            reply_markup=get_ytmusic_format_keyboard()
        )
        return

    # Check if URL is supported
    if not re.search(r"(instagram\.com|youtube\.com|youtu\.be)", url):
        logger.info(f"Ignoring non-URL message from user {update.message.from_user.id}")
        return

    status_msg = await update.message.reply_text("🔗 Processing your link...")
    temp_dir = None
    try:
        # Handle Instagram links (direct download with enhanced features)
        if "instagram.com" in url:
            await handle_instagram_content(update, context, url, status_msg)
            return

        # Handle YouTube links (video/audio choice)
        if "youtube.com/" in url or "youtu.be/" in url:
            ydl_opts_check = {'quiet': True, 'extract_flat': True, 'force_generic_extractor': False}
            info_dict = await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(ydl_opts_check).extract_info(url, download=False))

            is_playlist = info_dict.get('_type') == 'playlist'
            context.user_data["pending_youtube_url"] = url
            context.user_data["pending_youtube_is_playlist"] = is_playlist

            if is_playlist:
                playlist_title = info_dict.get('title', 'Unnamed Playlist')
                video_count = len(info_dict.get('entries', []))
                await context.bot.edit_message_text(
                    chat_id=status_msg.chat_id,
                    message_id=status_msg.message_id,
                    text=f"🎬 *Playlist Detected:* {md2(playlist_title)}\n📊 *Videos:* {video_count}\n\n*Choose download format:*",
                    parse_mode='MarkdownV2',
                    reply_markup=get_youtube_format_keyboard()
                )
            else:
                video_title = info_dict.get('title', 'Unknown Title')
                await context.bot.edit_message_text(
                    chat_id=status_msg.chat_id,
                    message_id=status_msg.message_id,
                    text=f"🎬 *Video:* {md2(video_title)}\n\n*Choose download format:*",
                    parse_mode='MarkdownV2',
                    reply_markup=get_youtube_format_keyboard()
                )
            return

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
    print("🤖 Starting Enhanced Telegram Media Downloader Bot...")
    if not BOT_TOKEN:
        print("❌ ERROR: Please set your bot token in the BOT_TOKEN environment variable!")
        return

    # Check Instagram configuration
    if INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD:
        print("✅ Instagram credentials configured - private content supported")
    else:
        print("⚠️ Instagram credentials not configured - only public content supported")
        print("   Set INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD environment variables for full access")

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
    app.add_handler(CallbackQueryHandler(youtube_format_callback, pattern=r"^ytfmt\|"))
    app.add_handler(CallbackQueryHandler(youtube_audio_format_callback, pattern=r"^ytaudiofmt\|"))
    print("✅ Bot started successfully! Send /start to begin.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
