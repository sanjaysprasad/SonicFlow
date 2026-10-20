# 🎵 SonicFlow — YouTube & Instagram Media Downloader

> A fast, studio-grade media extractor for **YouTube** and **Instagram**, featuring both a **macOS Desktop GUI** and a **Mobile/PC Responsive Web Application**. Supports high-fidelity audio (MP3, FLAC, WAV, AAC, OPUS) and MP4 video (480p up to 4K Ultra HD).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?style=flat&logo=fastapi&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=flat&logo=docker&logoColor=white)
![TailwindCSS](https://img.shields.io/badge/TailwindCSS-v3-38B2AC?style=flat&logo=tailwind-css&logoColor=white)

---

## ✨ Features

- **Multi-Platform Support**:
  - **YouTube**: Standard videos, Shorts, Playlists, and YouTube Music (`music.youtube.com`).
  - **Instagram**: Reels, Posts, and IGTV clips.
- **Video & Audio Formats**:
  - 🎬 **MP4 Video**: 480p (SD), 720p (HD), 1080p (Full HD), 1440p (2K), and 2160p (4K Ultra HD).
  - 🎵 **High-Fidelity Audio**:
    - `MP3`: 320 kbps (Maximum), 256 kbps, 192 kbps, 128 kbps, or VBR.
    - `FLAC`: Studio-grade lossless original master fidelity.
    - `WAV`: Raw uncompressed PCM audio.
    - `AAC`: Modern high-efficiency audio.
    - `OPUS`: Ultra-efficient low-latency audio codec.
- **Smart Tracking Sanitizer**: Automatically strips tracking tokens (`?si=...`, `?igsh=...`, `utm_*`).
- **Cover Artwork & Metadata**: Automatically embeds high-res thumbnail covers and ID3 metadata (Artist, Title, Album) into the exported files.
- **Dual Interface**:
  - 🖥️ **Desktop App**: Dark-mode GUI with progress bars and folder picker.
  - 📱 **Web App**: Responsive glassmorphism interface that works seamlessly on iPhone, Android, and Desktop browsers.
- **Docker & Cloud Ready**: Includes a production `Dockerfile` ready for 1-click free deployment on Render, Koyeb, or Docker hosts.

---

## 🚀 Quick Start

### 1. Web Application (Mobile & PC)
Run the local web server:
- **Windows**: Double-click `launch_web.bat`
- **macOS**: Double-click `launch_public_website.command` (or run `python3 server.py`)

Open **[http://localhost:8000](http://localhost:8000)** in your browser.

To share an instant **public HTTPS link** for your phone or friends:
```bash
python3 run_public.py
```

### 2. Desktop Application
Launch the standalone GUI:
- **Windows**: Double-click `launch_desktop.bat`
- **macOS**: Double-click `launch_desktop.command` (or run `python3 app.py`)

---

## 🐳 Docker Deployment

Build and run with Docker:
```bash
docker build -t sonicflow .
docker run -p 7860:7860 sonicflow
```

---

## 🛠 Prerequisites

- `python3` (3.11+)
- `ffmpeg` (for media transcoding)
- Dependencies: `pip install -r requirements.txt`

---

## 📄 License

This project is licensed under the **[MIT License](LICENSE)** - see the [LICENSE](LICENSE) file for details.

---

## ⚖️ Disclaimer

This tool is intended strictly for personal backup, offline listening, and fair-use archiving of public media.
