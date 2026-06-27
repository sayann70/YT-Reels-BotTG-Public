# 📥 Telegram Media Downloader Bot

A Telegram bot that downloads videos and audio from **YouTube**, **YouTube Music**, and **Instagram** — with support for playlists, multiple audio formats, and a self-hosted Bot API server for files up to **2 GB**.

---

## ✨ Features

- 📹 **YouTube** — Download videos or extract audio (MP3, FLAC, WAV)
- 🎵 **YouTube Music** — Direct audio download with format selection
- 📸 **Instagram** — Posts, Reels, Stories, and Carousels with metadata
- 📦 **Playlist support** — Batch download entire YouTube playlists
- 🏠 **Self-Hosted API** — Bypass Telegram's 50 MB limit (up to 2 GB)
- 🏓 **Admin /ping** — Live system stats, API latency, and active task monitoring
- ☁️ **Gofile fallback** — Auto-uploads oversized files to [Gofile.io](https://gofile.io)
- ⚡ **Concurrent processing** — Handles multiple users simultaneously

---

## 🚀 Quick Start (Official Telegram API)

This is the simplest setup. Files larger than **50 MB** will be uploaded to Gofile.

### 1. Clone & Install

```bash
git clone https://github.com/sayann70/YT-Reels-BotTG-Public YT-Reels-BotTG && cd YT-Reels-BotTG
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and set your bot token:

```env
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
```

### 3. Run

```bash
python3 bot.py
```

That's it! Send a YouTube or Instagram link to your bot.

---

## 🏠 Self-Hosted Bot API (2 GB Upload Limit)

To send files larger than 50 MB directly through Telegram, you need to run your own [Telegram Bot API Server](https://github.com/tdlib/telegram-bot-api).

### Prerequisites

- **Docker** installed on your machine
- **API ID & Hash** from [my.telegram.org](https://my.telegram.org) → "API development tools"

### 1. Set up the Local API Server

A `docker-compose.yml` is included in this repo. Add your API credentials to `.env`:

```env
TELEGRAM_API_ID = 12345678
TELEGRAM_API_HASH = your_api_hash_here
USE_LOCAL_API = true
```

Start the server:

```bash
sudo docker compose up -d
```

### 2. Log Out from Official API

**Required once** before switching to the local server:

```bash
curl https://api.telegram.org/bot<YOUR_BOT_TOKEN>/logOut
```

Wait **10 minutes**, then start the bot:

```bash
python3 bot.py
```

### 3. Switching Back to Official API

Set `USE_LOCAL_API = false` in `.env`, call the logout endpoint on your local server, wait 10 minutes, and restart.

---

## ⚙️ Configuration

All settings are in `.env` (see [.env.example](.env.example) for reference):

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | *required* | Telegram Bot Token from [@BotFather](https://t.me/BotFather) |
| `ADMIN_ID` | `0` | Your Telegram user ID (send `/ping` to find out) |
| `USE_LOCAL_API` | `false` | Enable self-hosted Bot API server |
| `LOCAL_API_SERVER` | `http://localhost:8081` | Address of local API server |
| `TELEGRAM_API_ID` | — | API ID from my.telegram.org (for Docker) |
| `TELEGRAM_API_HASH` | — | API Hash from my.telegram.org (for Docker) |
| `MAX_FILE_SIZE_MB` | Auto | `49` (official) or `1999` (local) — auto-detected |
| `MAX_PLAYLIST_SIZE` | `50` | Max videos per playlist |
| `INSTAGRAM_USERNAME` | — | Instagram login (optional, for private content) |
| `INSTAGRAM_PASSWORD` | — | Instagram password |

---

## 🤖 Bot Commands

| Command | Access | Description |
|---|---|---|
| `/start` | Everyone | Welcome message and usage info |
| `/help` | Everyone | Supported platforms and settings |
| `/ping` | Admin only | System stats, API latency, active tasks |

---

## 📁 File Structure

```
├── bot.py               # Main bot script
├── .env.example         # Configuration template (DO NOT MAKE THIS PUBLIC AFTER CONFIGURATION!!!)
├── docker-compose.yml   # Local Bot API server (Docker)
├── requirements.txt     # Python dependencies
└── README.md            # This file
```

---

## 🧰 Tech Stack

- **Python 3.10+**
- **python-telegram-bot** (v20+)
- **yt-dlp** — YouTube & Instagram downloads
- **instaloader** — Instagram metadata & downloads
- **httpx** — Async HTTP client
- **Pillow** — Thumbnail processing
- **psutil** — System monitoring for /ping
- **Docker** — Self-hosted Bot API server (optional)

---

## 🛡️ Notes

- Ensure `ffmpeg` is installed and available in your system PATH.
- This bot is for **educational** and **personal** use only.
- Follow the terms of service for YouTube, Instagram, and Telegram.
- Instagram private posts require login credentials.

---

## 📃 License

MIT License © 2025 Sayan Sarkar | sayann70