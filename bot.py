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
from urllib.parse import urlparse, parse_qs
from PIL import Image
import random
import pathlib
from ytmusicapi import YTMusic
import yt_dlp

# --- Configuration ---
BOT_TOKEN = "ENTER_YOUR_BOT_TOKEN_HERE"
MAX_PLAYLIST_SIZE = 50  # Limit playlist size to prevent abuse

# --- Logging Setup ---
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
    """Upload file to Gofile with retry mechanism."""
    max_retries = 5
    base_delay = 5

    async with httpx.AsyncClient(timeout=60.0) as client:
        for attempt in range(max_retries):
            try:
                # Get best server
                try:
                    api_response = await client.get("https://api.gofile.io/getServer", timeout=20)
                    api_response.raise_for_status()
                    server = api_response.json()["data"]["server"]
                    logger.info(f"Using Gofile server: {server}")
                except Exception as e:
                    logger.warning(f"Could not get Gofile server, using fallback. Error: {e}")
                    server = f"store{random.randint(1, 9)}"

                # Upload file
                upload_url = f"https://{server}.gofile.io/uploadFile"
                with open(file_path, "rb") as f:
                    files = {"file": (os.path.basename(file_path), f)}
                    logger.info(f"Attempt {attempt + 1}: Uploading '{os.path.basename(file_path)}' to {upload_url}...")
                    upload_response = await client.post(upload_url, files=files, timeout=300)
                    upload_response.raise_for_status()

                    data = upload_response.json()
                    if data["status"] == "ok":
                        logger.info("✅ Gofile upload successful!")
                        return data["data"]["downloadPage"]
                    else:
                        logger.warning(f"Gofile API error: {data.get('status')}")

            except Exception as e:
                logger.error(f"⚠️ Gofile upload attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.info(f"Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)

    raise Exception("❌ All Gofile upload attempts failed.")


# --- Download Functions ---

async def download_single_video(url: str, context: ContextTypes.DEFAULT_TYPE, msg_to_edit=None):
    """Downloads a single video from a URL."""
    tmpdir = tempfile.mkdtemp()

    # Progress tracking
    progress_data = {"last_update": 0}

    def progress_hook(d):
        if d['status'] == 'downloading':
            current_time = time.monotonic()
            if (current_time - progress_data["last_update"]) > 3:
                percent = d.get('_percent_str', 'N/A').strip()
                speed = d.get('_speed_str', 'N/A').strip()
                eta = d.get('_eta_str', 'N/A').strip()
                logger.info(f"Downloading: {percent} at {speed} (ETA: {eta})")
                progress_data["last_update"] = current_time

    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'outtmpl': os.path.join(tmpdir, '%(id)s.%(ext)s'),
        'noplaylist': True,  # Explicitly disable playlist processing
        'quiet': True,
        'no_warnings': True,
        'merge_output_format': 'mp4',
        'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
        'progress_hooks': [progress_hook],
    }

    try:
        if msg_to_edit:
            await context.bot.edit_message_text(
                chat_id=msg_to_edit.chat_id,
                message_id=msg_to_edit.message_id,
                text="📥 Starting download..."
            )

        info_dict = await asyncio.to_thread(
            lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=True)
        )

        # Find downloaded video file
        video_path = info_dict.get('filepath')
        if not video_path or not os.path.exists(video_path):
            # Fallback: search for video files in tmpdir
            video_files = [f for f in os.listdir(tmpdir) if f.endswith(('.mp4', '.mkv', '.webm', '.avi'))]
            if video_files:
                video_path = os.path.join(tmpdir, video_files[0])
            else:
                raise Exception("No video file found after download")

        uploader = info_dict.get('uploader', info_dict.get('uploader_id', 'Unknown'))
        title = info_dict.get('title', 'No Title')
        thumbnail_url = info_dict.get('thumbnail')

        # Process thumbnail
        thumbnail_path = None
        if thumbnail_url and msg_to_edit:
            try:
                await context.bot.edit_message_text(
                    chat_id=msg_to_edit.chat_id,
                    message_id=msg_to_edit.message_id,
                    text="🖼️ Processing thumbnail..."
                )

                async with httpx.AsyncClient(timeout=20.0) as client:
                    response = await client.get(thumbnail_url)
                    response.raise_for_status()

                    original_thumbnail_path = os.path.join(tmpdir, "thumb.jpg")
                    with open(original_thumbnail_path, 'wb') as f:
                        f.write(response.content)

                    # Crop and resize thumbnail
                    with Image.open(original_thumbnail_path) as img:
                        w, h = img.size
                        crop_size = min(w, h)
                        left = (w - crop_size) // 2
                        top = (h - crop_size) // 2

                        img_cropped = img.crop((left, top, left + crop_size, top + crop_size))
                        img_resized = img_cropped.resize((320, 320), Image.LANCZOS)

                        thumbnail_path = os.path.join(tmpdir, "thumb_cropped.jpg")
                        img_resized.save(thumbnail_path, "JPEG")
            except Exception as e:
                logger.warning(f"Thumbnail processing failed: {e}")

        # Rename video file with sanitized title
        if video_path:
            file_ext = pathlib.Path(video_path).suffix
            new_video_path = os.path.join(tmpdir, sanitize_filename(title) + file_ext)
            try:
                os.rename(video_path, new_video_path)
                video_path = new_video_path
            except OSError:
                pass  # Keep original path if rename fails

        return video_path, uploader, title, tmpdir, thumbnail_path

    except Exception as e:
        logger.error(f"Download failed for URL '{url}'. Error: {e}", exc_info=True)
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None, None, None, None, None

async def download_youtube_music(url: str, context: ContextTypes.DEFAULT_TYPE, msg):
    """Downloads an audio track from YouTube Music."""
    tmpdir = tempfile.mkdtemp()

    try:
        await context.bot.edit_message_text(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            text="🎵 Processing YouTube Music link..."
        )

        ytmusic = YTMusic()

        # Extract video ID from URL
        parsed_url = urlparse(url)
        video_id = parse_qs(parsed_url.query).get('v', [None])[0]

        if not video_id and 'music.youtube.com/watch' in url:
            path_parts = parsed_url.path.split('/')
            if len(path_parts) > 2:
                video_id = path_parts[-1]

        if not video_id:
            raise ValueError("Could not extract video ID from URL")

        # Get song info and streaming data
        song = await asyncio.to_thread(ytmusic.get_song, videoId=video_id)
        streaming_data = await asyncio.to_thread(ytmusic.get_streaming_data, videoId=video_id)

        if not streaming_data.get('formats'):
            raise Exception("No streaming formats available")

        stream_url = streaming_data['formats'][0]['url']
        title = song['videoDetails']['title']
        artist = song['videoDetails']['author']

        file_path = os.path.join(tmpdir, f"{sanitize_filename(title)}.mp3")

        # Download audio stream
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("GET", stream_url) as response:
                response.raise_for_status()
                with open(file_path, 'wb') as f:
                    async for chunk in response.aiter_bytes():
                        f.write(chunk)

        return file_path, title, artist, tmpdir

    except Exception as e:
        logger.error(f"YouTube Music download failed: {e}", exc_info=True)
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None, None, None, None

async def process_playlist(url: str, playlist_info: dict, context: ContextTypes.DEFAULT_TYPE, msg):
    """Processes and downloads all videos from a playlist."""
    playlist_title = playlist_info.get('title', 'Unnamed Playlist')
    videos = playlist_info.get('entries', [])
    total_videos = len(videos)

    # Limit playlist size
    if total_videos > MAX_PLAYLIST_SIZE:
        await context.bot.edit_message_text(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            text=f"⚠️ Playlist too large! Maximum {MAX_PLAYLIST_SIZE} videos allowed. Found {total_videos} videos.",
            parse_mode='MarkdownV2'
        )
        return

    await context.bot.edit_message_text(
        chat_id=msg.chat_id,
        message_id=msg.message_id,
        text=f"✅ Playlist detected: *{escape_markdown(playlist_title)}*\nFound *{total_videos}* videos\\. Starting download\\.\\.\\.",
        parse_mode='MarkdownV2'
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

        status_msg = await context.bot.send_message(
            chat_id=msg.chat_id,
            text=f"📥 Downloading video {i}/{total_videos}: *{escape_markdown(video_title[:50])}*{'...' if len(video_title) > 50 else ''}",
            parse_mode='MarkdownV2'
        )

        try:
            video_path, uploader, title, temp_dir, thumb_path = await download_single_video(
                video_url, context, status_msg
            )

            if video_path and os.path.exists(video_path):
                file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
                caption = f"*{i}/{total_videos}:* {escape_markdown(title[:50])}{'...' if len(title) > 50 else ''}\n*By:* {escape_markdown(uploader)}"

                if file_size_mb > 49:
                    await context.bot.edit_message_text(
                        chat_id=status_msg.chat_id,
                        message_id=status_msg.message_id,
                        text=f"📤 Large file ({file_size_mb:.1f}MB) - uploading to Gofile..."
                    )
                    gofile_link = await upload_to_gofile(video_path)
                    await context.bot.send_message(
                        chat_id=msg.chat_id,
                        text=f"{caption}\n🔗 [Download Link]({gofile_link})",
                        parse_mode='MarkdownV2',
                        disable_web_page_preview=True
                    )
                else:
                    await context.bot.edit_message_text(
                        chat_id=status_msg.chat_id,
                        message_id=status_msg.message_id,
                        text=f"📤 Sending video {i}/{total_videos}..."
                    )

                    with open(video_path, 'rb') as video_file:
                        thumb_obj = None
                        if thumb_path and os.path.exists(thumb_path):
                            thumb_obj = open(thumb_path, 'rb')

                        try:
                            await context.bot.send_video(
                                chat_id=msg.chat_id,
                                video=video_file,
                                caption=caption,
                                parse_mode='MarkdownV2',
                                thumbnail=thumb_obj,
                                write_timeout=60
                            )
                        finally:
                            if thumb_obj:
                                thumb_obj.close()

                successful_downloads += 1
                await context.bot.delete_message(chat_id=status_msg.chat_id, message_id=status_msg.message_id)
            else:
                failed_downloads += 1
                await context.bot.edit_message_text(
                    chat_id=status_msg.chat_id,
                    message_id=status_msg.message_id,
                    text=f"⚠️ Failed to download video {i}/{total_videos}"
                )

            # Cleanup
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

            # Rate limiting
            await asyncio.sleep(2)

        except Exception as e:
            failed_downloads += 1
            logger.error(f"Failed processing video {i} from playlist. URL: {video_url}, Error: {e}", exc_info=True)
            await context.bot.edit_message_text(
                chat_id=status_msg.chat_id,
                message_id=status_msg.message_id,
                text=f"❌ Error on video {i}/{total_videos}"
            )

    # Final summary
    summary = f"✅ Playlist complete!\n📊 Downloaded: {successful_downloads}/{total_videos}"
    if failed_downloads > 0:
        summary += f"\n⚠️ Failed: {failed_downloads}"

    await context.bot.send_message(chat_id=msg.chat_id, text=summary)


# --- Telegram Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    welcome_text = (
        "👋 Welcome to the Media Downloader Bot!\n\n"
        "📱 Supported platforms:\n"
        "• YouTube (videos & playlists)\n"
        "• YouTube Music\n"
        "• Instagram\n\n"
        "📋 Just send me a link and I'll download it for you!\n"
        f"📊 Playlist limit: {MAX_PLAYLIST_SIZE} videos"
    )
    await update.message.reply_text(welcome_text)

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    help_text = (
        "🔗 How to use:\n"
        "1. Send me a URL from YouTube, Instagram, or YouTube Music\n"
        "2. For playlists, I'll download all videos sequentially\n"
        "3. Large files (>49MB) will be uploaded to Gofile\n\n"
        "⚠️ Note: Private or restricted content cannot be downloaded"
    )
    await update.message.reply_text(help_text)

async def url_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle URL messages."""
    if not update.message or not update.message.text:
        return

    url = update.message.text.strip()

    # Check if message contains supported URL
    if not re.search(r"(instagram\.com|youtube\.com|youtu\.be|music\.youtube\.com)", url):
        logger.info(f"Ignoring non-URL message from user {update.message.from_user.id}")
        return

    msg = await update.message.reply_text("🔗 Processing your link...")
    temp_dir = None

    try:
        # Handle YouTube Music
        if "music.youtube.com/" in url:
            file_path, title, artist, temp_dir = await download_youtube_music(url, context, msg)

            if file_path and os.path.exists(file_path):
                await context.bot.edit_message_text(
                    chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    text="✅ Download complete! Sending audio..."
                )

                caption = f"*Title:* {escape_markdown(title)}\n*By:* {escape_markdown(artist)}"

                with open(file_path, 'rb') as audio_file:
                    await update.message.reply_audio(
                        audio=audio_file,
                        caption=caption,
                        parse_mode='MarkdownV2',
                        title=title,
                        performer=artist
                    )

                await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
            else:
                await context.bot.edit_message_text(
                    chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    text="⚠️ Failed to download from YouTube Music."
                )

        # Handle YouTube (check for playlist first)
        elif "youtube.com/" in url or "youtu.be/" in url:
            # Check if it's a playlist
            ydl_opts_check = {
                'quiet': True,
                'extract_flat': True,
                'force_generic_extractor': False
            }

            info_dict = await asyncio.to_thread(
                lambda: yt_dlp.YoutubeDL(ydl_opts_check).extract_info(url, download=False)
            )

            if info_dict.get('_type') == 'playlist':
                await process_playlist(url, info_dict, context, msg)
                return  # Playlist function handles everything
            else:
                # Single video
                video_path, uploader, title, temp_dir, thumb_path = await download_single_video(url, context, msg)
                await _handle_single_video_result(
                    video_path, uploader, title, temp_dir, thumb_path,
                    url, context, msg, update
                )

        # Handle Instagram and other single videos
        else:
            video_path, uploader, title, temp_dir, thumb_path = await download_single_video(url, context, msg)
            await _handle_single_video_result(
                video_path, uploader, title, temp_dir, thumb_path,
                url, context, msg, update
            )

    except Exception as e:
        logger.error(f"Error in url_handler: {e}", exc_info=True)
        await context.bot.edit_message_text(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            text="❌ An unexpected error occurred. Please try again later."
        )
    finally:
        # Cleanup
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

async def _handle_single_video_result(video_path, uploader, title, temp_dir, thumb_path, url, context, msg, update):
    """Handle the result of a single video download."""
    if video_path and os.path.exists(video_path):
        file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
        caption = f"*Title:* {escape_markdown(title)}\n*By:* {escape_markdown(uploader)}\n[🔗 Source]({escape_markdown(url)})"

        if file_size_mb > 49:
            await context.bot.edit_message_text(
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                text=f"📤 Large file ({file_size_mb:.1f}MB) - uploading to Gofile..."
            )
            gofile_link = await upload_to_gofile(video_path)
            await update.message.reply_text(
                f"✅ Uploaded to Gofile:\n{gofile_link}",
                disable_web_page_preview=True
            )
        else:
            await context.bot.edit_message_text(
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                text="✅ Download complete! Sending video..."
            )

            with open(video_path, 'rb') as video_file:
                thumb_obj = None
                if thumb_path and os.path.exists(thumb_path):
                    thumb_obj = open(thumb_path, 'rb')

                try:
                    await update.message.reply_video(
                        video=video_file,
                        caption=caption,
                        parse_mode='MarkdownV2',
                        thumbnail=thumb_obj,
                        write_timeout=60
                    )
                finally:
                    if thumb_obj:
                        thumb_obj.close()

        await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
    else:
        await context.bot.edit_message_text(
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            text="⚠️ Failed to download the content. The link may be private, invalid, or temporarily unavailable."
        )


# --- Main Bot Logic ---
def main():
    """Start the bot."""
    print("🤖 Starting Telegram Media Downloader Bot...")

    if BOT_TOKEN == "ENTER_YOUR_BOT_TOKEN_HERE":
        print("❌ ERROR: Please set your bot token in the BOT_TOKEN variable!")
        return

    # Build application
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )

    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_handler))

    print("✅ Bot started successfully! Send /start to begin.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()