# Use lightweight Python base image
FROM python:3.11-slim

# Install system dependencies: FFmpeg and ffprobe
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy dependency definition
COPY requirements.txt .

# Install Python libraries
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot application files
COPY loop_bot.py .

# Run the bot
CMD ["python", "loop_bot.py"]
