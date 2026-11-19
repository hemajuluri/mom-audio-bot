#!/bin/bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# 1. Check if ffmpeg is installed
if ! command -v ffmpeg &> /dev/null && [ ! -f /opt/homebrew/bin/ffmpeg ]; then
    echo "⚠️ FFmpeg is not installed."
    echo "Installing FFmpeg via Homebrew..."
    brew install ffmpeg
fi

# 2. Check virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

# 3. Check requirements
if ! python3 -c "import telegram" &> /dev/null; then
    echo "Installing python-telegram-bot..."
    pip install -r requirements.txt
fi

# 4. Load .env if present
if [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
fi

echo "🚀 Starting Mom's Audio Looper Bot..."
echo "Mac sleep prevention active (caffeinate)."
echo "Press Ctrl+C to stop."

caffeinate -i python3 loop_bot.py
