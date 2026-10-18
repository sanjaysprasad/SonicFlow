FROM python:3.11-slim

# Install system dependencies:
# - ffmpeg: audio/video transcoding & muxing
# - nodejs: executes YouTube JavaScript player scripts (solves n-sig and bot challenges)
# - curl: network utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python dependencies and ensure latest yt-dlp release
COPY requirements.txt .
RUN pip install --no-cache-dir -U -r requirements.txt \
    && pip install --no-cache-dir -U yt-dlp

# Copy application files (including server.py, and cookies if placed in project)
COPY . .

# Standard web ports (8000 for local/Koyeb, 10000 for Render)
EXPOSE 8000
EXPOSE 10000

# Dynamically bind to the platform port ($PORT) or fallback to 8000
CMD ["sh", "-c", "python3 -m uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
