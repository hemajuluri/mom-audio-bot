# Mom's Audio Looper Telegram Bot

A Telegram bot designed specifically so your mom can send/forward any audio file or voice message and easily repeat it, either with **1-tap quick buttons** or by typing any number (like `7`).

---

## ✨ Features

- **Tap-a-Button Convenience**: When your mom sends an audio, the bot immediately shows quick-tap buttons:
  - `[ 🔁 Repeat 3x ]` `[ 🔁 Repeat 5x ]` `[ 🔁 Repeat 7x ]`
  - `[ 🔁 Repeat 11x ]` `[ 🔁 Repeat 21x ]` `[ 🔁 Repeat 50x ]`
- **Custom Typing**: She can also just reply with any number (e.g., `8`, `15`).
- **Supports All Formats**: Music files (`.mp3`, `.m4a`, `.wav`), Telegram voice notes, and audio documents.
- **Lossless & Fast**: Uses `ffmpeg -stream_loop` with stream copying (`-c copy`) for zero quality loss and instant repeats (with fallback re-encoding if needed).
- **Exact Filename & Metadata**: Keeps the original filename and performer/song title.
- **Private Access Control**: Optionally restrict access to only your and your mom's Telegram accounts (`ALLOWED_USER_IDS`).

---

## 🚀 How to Run It on Your Mac

### 1. Ensure FFmpeg is installed
```bash
brew install ffmpeg
```

### 2. Start the Bot
Inside this directory (`/Users/sasinandan/Desktop/Mom_audio`), simply run:
```bash
./run.sh
```
This script will:
1. Verify FFmpeg.
2. Install dependencies (`python-telegram-bot`) into the local `.venv`.
3. Load your bot token from `.env`.
4. Keep your Mac awake using `caffeinate` so the bot stays active.

---

## 🔒 Making It Private (Recommended)

1. Open Telegram, start your bot, and send `/start`.
2. The bot will print your Telegram User ID. Have your mom do the same to get her ID.
3. Open `.env` in this directory and add the IDs:
   ```env
   ALLOWED_USER_IDS=123456789,987654321
   ```
4. Restart the bot (`Ctrl+C` then `./run.sh`). Now only you and your mom can use it!
