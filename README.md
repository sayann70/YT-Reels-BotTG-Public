# 📥 Telegram Video Downloader Bot

A powerful Telegram bot built with Python that downloads **Instagram Reels/Posts** and **YouTube videos**, with real-time progress updates and automatic **Gofile upload** for large files (50MB+).

---

## ✨ Features

- 📸 Download from Instagram (Reels/Posts)
- 📹 Download from YouTube (videos)
- ⏳ Real-time download progress updates
- 🧠 Intelligent file renaming using post titles
- 🖼️ Auto-thumbnail cropping for better previews
- ☁️ Gofile upload for videos larger than 49MB
- 🧼 MarkdownV2-safe captions
- 🧪 Clean and temporary directory management
- 🚀 Async and multithreaded performance using `asyncio`

---

## 🚀 How to Run

### 1. Clone the Repository

```bash
git clone https://github.com/sayann70/YT-Reels-BotTG
cd YT-Reels-BotTG
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

**Dependencies include:**
- `python-telegram-bot`
- `yt-dlp`
- `httpx`
- `Pillow`
- `requests`

### 3. Set Your Bot Token

Replace the `BOT_TOKEN` value in the script with your own [Telegram Bot Token](https://t.me/BotFather):

```python
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
```

---

## ▶️ Usage

Just send your bot an Instagram or YouTube video URL. It will:

1. Download the video
2. Show you the progress
3. Send the video directly (if < 50MB)
4. Or upload to Gofile and share the link (if > 50MB)

---

## 📦 Gofile Integration

Videos exceeding Telegram's 50MB limit are uploaded to [Gofile.io](https://gofile.io) automatically using their API.

---

## 🛡️ Notes & Warnings

- Ensure you follow the terms of service for YouTube, Instagram, and Gofile.
- This bot is for **educational** and **personal** use only.
- Instagram private posts are **not supported**.
- Make sure `ffmpeg` is installed and available in your system path.

---

## 🧰 Tech Stack

- **Python 3.10+**
- **python-telegram-bot (v20+)**
- **yt-dlp**
- **httpx**
- **Pillow**
- **requests**

---

## 📁 File Structure

```
├── bot.py                   # Main bot script
├── requirements.txt         # Dependencies list
├── README.md                # This file
```

---

## 📃 License

MIT License © 2025 Sayan Sarkar | sayann70
