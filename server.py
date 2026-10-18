#!/usr/bin/env python3
"""
SonicFlow Web Server — Responsive YouTube & Instagram Media Extractor
FastAPI backend supporting both High-Quality Audio (MP3, FLAC, WAV, AAC, OPUS)
and Video (MP4 from 480p up to 4K Ultra HD) with background workers and auto-cleanup.
"""

import os
import re
import sys
import time
import uuid
import shutil
import asyncio
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="SonicFlow Web", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMP_DIR = Path("/tmp/sonicflow_web")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# In-memory job state store
jobs: Dict[str, Dict[str, Any]] = {}


def find_ffmpeg() -> str:
    """Detect ffmpeg binary location."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = [
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg"
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return "ffmpeg"


def sanitize_url(raw_url: str) -> str:
    """Normalize YouTube & Instagram URLs and strip tracking query parameters."""
    url = raw_url.strip()
    if not url:
        return ""

    url = url.replace("music.youtube.com", "www.youtube.com")

    if "youtu.be/" in url:
        video_id = url.split("youtu.be/")[1].split("?")[0].split("&")[0]
        url = f"https://www.youtube.com/watch?v={video_id}"

    # Strip tracking parameters (si, igsh, utm_*, fbclid)
    url = re.sub(r'([?&])(si|igsh|utm_[a-zA-Z0-9_]+|fbclid)=[^&#]*', r'\1', url)
    url = url.replace("?&", "?").rstrip("?&")
    return url


class ConvertRequest(BaseModel):
    url: str
    format: str = "mp3"
    quality: str = "320"


def cleanup_old_files():
    """Remove converted jobs older than 15 minutes to save server storage."""
    now = time.time()
    expired_jobs = []
    for jid, job in list(jobs.items()):
        if now - job.get("created_at", now) > 900:  # 15 minutes
            expired_jobs.append(jid)
            job_folder = TEMP_DIR / jid
            if job_folder.exists():
                shutil.rmtree(job_folder, ignore_errors=True)

    for jid in expired_jobs:
        jobs.pop(jid, None)


def find_cookie_file() -> Tuple[Optional[Path], str]:
    """
    Search for valid cookies across all possible locations:
    1. COOKIES_PATH env var
    2. Render Secret Files mount (/etc/secrets/): searches all files in directory
    3. Project /app and working directory (*cookie*.txt, cookies.txt, www.youtube.com_cookies.txt)
    4. /tmp/ directory
    5. YTDLP_COOKIES or YOUTUBE_COOKIES environment variables (plain Netscape text or base64)
    """
def check_cookie_auth(path: Path) -> Tuple[bool, str]:
    """Check if cookie file contains actual authenticated YouTube session tokens."""
    try:
        content = path.read_text(errors="ignore")
        has_login = "LOGIN_INFO" in content
        has_sid = any(k in content for k in ["SID\t", "SID ", "__Secure-1PSID"])
        if has_login and has_sid:
            return True, "Authenticated session (LOGIN_INFO & SID present)"
        elif has_sid or "__Secure-3PSID" in content:
            return False, "Unauthenticated session (missing LOGIN_INFO; guest/visitor only)"
        else:
            return False, "Empty or unrecognized cookie format"
    except Exception as e:
        return False, f"Read error: {e}"


def find_cookie_file() -> Tuple[Optional[Path], str]:
    """
    Search for valid cookies across all possible locations, prioritizing
    files that contain true authenticated Google login tokens (LOGIN_INFO).
    """
    # 1. Explicit env var path
    if os.environ.get("COOKIES_PATH"):
        p = Path(os.environ["COOKIES_PATH"])
        if p.is_file() and p.stat().st_size > 10:
            is_auth, note = check_cookie_auth(p)
            return p, f"Loaded from COOKIES_PATH: {p} ({p.stat().st_size} bytes, {note})"

    candidates: list[Path] = [
        Path("www.youtube.com_cookies.txt"),
        Path("cookies.txt"),
        Path("/app/www.youtube.com_cookies.txt"),
        Path("/app/cookies.txt"),
        Path("/etc/secrets/www.youtube.com_cookies.txt"),
        Path("/etc/secrets/cookies.txt"),
        Path("/tmp/sonicflow_cookies.txt"),
        Path("/tmp/cookies.txt"),
    ]

    # Check Render Secret Files directory (/etc/secrets) for ANY cookie/secret text file
    secrets_dir = Path("/etc/secrets")
    if secrets_dir.exists() and secrets_dir.is_dir():
        for f in secrets_dir.glob("*"):
            if f.is_file() and f.stat().st_size > 10 and f not in candidates:
                candidates.append(f)

    # Check current directory and /app for any cookie-like files
    for base in [Path("."), Path("/app")]:
        if base.exists() and base.is_dir():
            for f in base.glob("*cookie*.txt"):
                if f not in candidates:
                    candidates.append(f)

    existing = [c for c in candidates if c.exists() and c.is_file() and c.stat().st_size > 10]

    # Prioritize authenticated cookies (with LOGIN_INFO) over anonymous visitor cookies
    authenticated_candidates = [c for c in existing if check_cookie_auth(c)[0]]
    chosen = authenticated_candidates[0] if authenticated_candidates else (existing[0] if existing else None)

    if chosen:
        is_auth, note = check_cookie_auth(chosen)
        return chosen, f"Found cookie file: {chosen} ({chosen.stat().st_size} bytes, {note})"

    # Check environment variable
    raw_env_cookie = os.environ.get("YTDLP_COOKIES") or os.environ.get("YOUTUBE_COOKIES")
    if raw_env_cookie:
        try:
            target = Path("/tmp/sonicflow_cookies.txt")
            raw_cookie = raw_env_cookie.replace("\\n", "\n").strip()
            if not raw_cookie.startswith("# Netscape") and "\t" not in raw_cookie:
                try:
                    import base64
                    decoded = base64.b64decode(raw_cookie).decode("utf-8", errors="ignore")
                    if "# Netscape" in decoded or "\t" in decoded:
                        raw_cookie = decoded
                except Exception:
                    pass
            target.write_text(raw_cookie)
            is_auth, note = check_cookie_auth(target)
            return target, f"Loaded cookies from environment variable ({len(raw_cookie)} bytes, {note})"
        except Exception as e:
            print(f"Error parsing cookies env var: {e}")

    return None, "No cookies detected"


def run_yt_dlp_worker(job_id: str, url: str, fmt: str, quality: str):
    """Background worker to download and process audio or video with multi-strategy fallback."""
    job_folder = TEMP_DIR / job_id
    job_folder.mkdir(parents=True, exist_ok=True)

    fmt = fmt.lower()
    is_video = (fmt == "mp4")

    jobs[job_id]["status"] = "processing"
    jobs[job_id]["progress"] = 10
    jobs[job_id]["message"] = f"Fetching {'video' if is_video else 'audio'} metadata..."

    output_template = str(job_folder / "%(title)s.%(ext)s")
    ffmpeg_bin = find_ffmpeg()
    ffmpeg_dir = os.path.dirname(ffmpeg_bin) if ffmpeg_bin else ""

    found_cookie, cookie_info = find_cookie_file()
    print(f"[{job_id}] {cookie_info}")

    # Determine platform
    url_lower = url.lower()
    is_youtube = ("youtube.com" in url_lower or "youtu.be" in url_lower)
    is_instagram = ("instagram.com" in url_lower or "instagr.am" in url_lower)

    # Multi-strategy client definitions for YouTube (especially on Cloud / Render datacenter IPs)
    if is_youtube:
        strategies = [
            # Strategy 1: Android API with player_skip (bypasses web HTML scraper & BotGuard PO-token challenge)
            ["--extractor-args", "youtube:player_client=android;player_skip=webpage,configs"],
            # Strategy 2: Android standard client
            ["--extractor-args", "youtube:player_client=android"],
            # Strategy 3: iOS app client
            ["--extractor-args", "youtube:player_client=ios"],
            # Strategy 4: TV Embedded client
            ["--extractor-args", "youtube:player_client=tv_embedded"],
            # Strategy 5: Mobile Web client
            ["--extractor-args", "youtube:player_client=mweb"],
            # Strategy 6: Web Embedded client
            ["--extractor-args", "youtube:player_client=web_embedded"],
            # Strategy 7: Web client with cookies
            ["--extractor-args", "youtube:player_client=web"],
            # Strategy 8: Default fallback
            []
        ]
    else:
        strategies = [[]]

    # Video / Audio format flags
    if is_video:
        res = quality.strip().lower().replace("p", "")
        if res in ["480", "720", "1080", "1440", "2160"]:
            fmt_str = f"bv*[height<={res}]+ba/b[height<={res}]/bv*+ba/b"
        else:
            fmt_str = "bv*+ba/b"
        format_args = [
            "-f", fmt_str,
            "--merge-output-format", "mp4",
            "--add-metadata",
            "--embed-thumbnail",
            "--no-playlist",
            "-o", output_template
        ]
    else:
        if fmt in ["flac", "wav", "alac"]:
            q_arg = "0"
        elif quality in ["320", "256", "192", "128", "96", "160"]:
            q_arg = f"{quality}k"
        else:
            q_arg = "320k"
        format_args = [
            "-f", "ba/b",
            "--extract-audio",
            "--audio-format", fmt,
            "--audio-quality", q_arg,
            "--add-metadata",
            "--embed-thumbnail",
            "--no-playlist",
            "-o", output_template
        ]

    success = False
    last_error_lines = []

    for strat_idx, strat_args in enumerate(strategies):
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "-4",
            "--no-check-certificates",
            "--geo-bypass",
            "--socket-timeout", "30",
        ]

        if found_cookie:
            cmd.extend(["--cookies", str(found_cookie.resolve())])

        if strat_args:
            cmd.extend(strat_args)

        # Support custom PO-Token env var if provided (YT_PO_TOKEN / PO_TOKEN)
        po_token = os.environ.get("YT_PO_TOKEN") or os.environ.get("PO_TOKEN")
        if po_token and is_youtube:
            cmd.extend(["--extractor-args", f"youtube:po_token=web+{po_token}"])

        # Only pass browser user-agent for Instagram; never override YouTube's native client user-agents!
        if is_instagram:
            cmd.extend([
                "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "--add-header", "Accept-Language: en-US,en;q=0.9"
            ])

        cmd.extend(format_args)

        if ffmpeg_dir and os.path.exists(ffmpeg_dir):
            cmd.extend(["--ffmpeg-location", ffmpeg_dir])

        cmd.append(url)

        error_lines = []

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )

            for line in proc.stdout:
                clean = line.strip()
                if not clean:
                    continue

                if "ERROR:" in clean or "Sign in to confirm" in clean or "429" in clean or "blocked" in clean.lower():
                    error_lines.append(clean)

                if "[download]" in clean and "%" in clean:
                    match = re.search(r'(\d+(?:\.\d+)?)%', clean)
                    if match:
                        pct = float(match.group(1))
                        mapped = 10 + int(pct * 0.75)
                        jobs[job_id]["progress"] = mapped
                        jobs[job_id]["message"] = f"Downloading: {clean}"
                elif "[ExtractAudio]" in clean or "[ffmpeg]" in clean or "[Merger]" in clean:
                    jobs[job_id]["progress"] = 90
                    jobs[job_id]["message"] = f"Muxing and processing {fmt.upper()}..."

            proc.wait()

            if proc.returncode == 0:
                target_ext = "mp4" if is_video else fmt
                produced_files = list(job_folder.glob(f"*.{target_ext}"))
                if not produced_files:
                    produced_files = [f for f in job_folder.iterdir() if f.is_file() and not f.name.endswith(".part")]

                if produced_files:
                    output_file = produced_files[0]
                    jobs[job_id]["status"] = "completed"
                    jobs[job_id]["progress"] = 100
                    jobs[job_id]["message"] = "Ready to download!"
                    jobs[job_id]["file_path"] = str(output_file)
                    jobs[job_id]["file_name"] = output_file.name
                    jobs[job_id]["file_size"] = os.path.getsize(output_file)
                    success = True
                    break

            last_error_lines = error_lines
            # If failed and more strategies exist, retry with next resolver
            if strat_idx < len(strategies) - 1:
                jobs[job_id]["message"] = f"Switching client stream resolver (attempt {strat_idx + 2} of {len(strategies)})..."
                time.sleep(0.5)
                continue

        except Exception as e:
            last_error_lines = [str(e)]

    if not success:
        jobs[job_id]["status"] = "error"
        cookie_status = f" [Cookies: {found_cookie.name if found_cookie else 'none detected'}]"
        if last_error_lines:
            clean_err = last_error_lines[-1].replace("ERROR: ", "").strip()
            norm_err = clean_err.replace("’", "'").replace("“", '"').replace("”", '"').lower()

            if "sign in to confirm" in norm_err or "confirm you're not a bot" in norm_err or "bot" in norm_err:
                is_auth, auth_status = check_cookie_auth(found_cookie) if found_cookie else (False, "none")
                if not is_auth:
                    jobs[job_id]["message"] = (
                        f"YouTube bot check triggered [{found_cookie.name if found_cookie else 'no cookies'}]. "
                        "The uploaded cookie file does NOT contain 'LOGIN_INFO' (it was exported while logged out). "
                        "Please export cookies from your browser while logged into YouTube and update your Render Secret File."
                    )
                else:
                    jobs[job_id]["message"] = (
                        f"YouTube bot check triggered [{found_cookie.name}]. "
                        "Your session cookies may have expired or need a refresh from your browser."
                    )
            elif "403" in norm_err or "forbidden" in norm_err:
                jobs[job_id]["message"] = f"YouTube returned 403 Forbidden{cookie_status}. The media stream was blocked or expired."
            elif "requested format is not available" in norm_err:
                jobs[job_id]["message"] = "Requested stream resolution is not available for this video. Try another quality."
            elif "video unavailable" in norm_err or "private video" in norm_err:
                jobs[job_id]["message"] = f"This video is private, age-restricted, or unavailable{cookie_status}."
            else:
                jobs[job_id]["message"] = f"yt-dlp error: {clean_err[:120]}{cookie_status}"
        else:
            jobs[job_id]["message"] = f"Conversion failed{cookie_status}. Link may be blocked or unavailable."


@app.post("/api/convert")
async def start_conversion(payload: ConvertRequest, background_tasks: BackgroundTasks):
    cleanup_old_files()

    clean_url = sanitize_url(payload.url)
    if not clean_url:
        raise HTTPException(status_code=400, detail="Please enter a valid URL.")

    if not any(domain in clean_url.lower() for domain in ["youtube.com", "youtu.be", "instagram.com"]):
        raise HTTPException(status_code=400, detail="Only YouTube and Instagram URLs are currently supported.")

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "message": "Initializing...",
        "created_at": time.time(),
        "file_path": None,
        "file_name": None
    }

    background_tasks.add_task(run_yt_dlp_worker, job_id, clean_url, payload.format, payload.quality)

    return {"job_id": job_id, "status": "queued"}


@app.get("/api/status/{job_id}")
async def get_job_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    return jobs[job_id]


@app.get("/api/download/{job_id}")
async def download_file(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Download expired or not found.")

    job = jobs[job_id]
    if job.get("status") != "completed" or not job.get("file_path"):
        raise HTTPException(status_code=400, detail="File is not ready yet.")

    file_path = Path(job["file_path"])
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File on disk has been cleaned up.")

    media_types = {
        "mp3": "audio/mpeg",
        "flac": "audio/flac",
        "wav": "audio/wav",
        "aac": "audio/aac",
        "m4a": "audio/mp4",
        "opus": "audio/opus",
        "mp4": "video/mp4"
    }
    ext = file_path.suffix.lstrip(".").lower()
    media_type = media_types.get(ext, "application/octet-stream")

    return FileResponse(
        path=str(file_path),
        filename=job["file_name"],
        media_type=media_type
    )


@app.get("/api/diagnostics")
def get_diagnostics():
    """Diagnostic endpoint to inspect environment, cookie status, ffmpeg, nodejs, and yt-dlp version."""
    found_cookie, cookie_info = find_cookie_file()
    ffmpeg_bin = find_ffmpeg()
    node_bin = shutil.which("node") or shutil.which("nodejs")

    try:
        import yt_dlp
        yt_version = getattr(yt_dlp, "__version__", "unknown")
    except Exception as e:
        yt_version = f"error: {e}"

    secrets_path = Path("/etc/secrets")
    secrets_list = []
    if secrets_path.exists() and secrets_path.is_dir():
        try:
            secrets_list = [f.name for f in secrets_path.iterdir() if f.is_file()]
        except Exception as e:
            secrets_list = [f"error reading secrets: {e}"]

    is_auth, auth_detail = check_cookie_auth(found_cookie) if found_cookie else (False, "No cookie file found")

    return {
        "status": "online",
        "cookie_found": found_cookie is not None,
        "cookie_is_authenticated": is_auth,
        "cookie_auth_detail": auth_detail,
        "cookie_source": cookie_info,
        "cookie_path": str(found_cookie) if found_cookie else None,
        "cookie_file_size_bytes": found_cookie.stat().st_size if found_cookie and found_cookie.exists() else 0,
        "ffmpeg_detected": bool(ffmpeg_bin and os.path.exists(ffmpeg_bin)),
        "ffmpeg_path": ffmpeg_bin,
        "nodejs_detected": bool(node_bin),
        "nodejs_path": node_bin,
        "yt_dlp_version": yt_version,
        "env_ytdlp_cookies_set": bool(os.environ.get("YTDLP_COOKIES") or os.environ.get("YOUTUBE_COOKIES")),
        "secrets_dir_exists": secrets_path.exists(),
        "secrets_files": secrets_list,
    }


@app.get("/icon.png")
async def get_icon():
    for p in [Path("icon.png"), Path("/app/icon.png")]:
        if p.exists() and p.is_file():
            return FileResponse(str(p), media_type="image/png")
    raise HTTPException(status_code=404, detail="Icon not found")


@app.get("/favicon.ico")
async def get_favicon():
    for p in [Path("icon.png"), Path("/app/icon.png")]:
        if p.exists() and p.is_file():
            return FileResponse(str(p), media_type="image/png")
    raise HTTPException(status_code=404, detail="Favicon not found")


# -------------------------------------------------------------------------
# Embedded Responsive HTML/Tailwind Frontend
# -------------------------------------------------------------------------
HTML_CONTENT = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>SonicFlow — YouTube & Instagram Audio/Video Downloader</title>
    <!-- Favicon & Icons -->
    <link rel="icon" type="image/png" href="/icon.png">
    <link rel="apple-touch-icon" href="/icon.png">
    <!-- Tailwind CSS -->
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    colors: {
                        brand: {
                            500: '#0284c7',
                            600: '#0369a1'
                        }
                    }
                }
            }
        }
    </script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');
        body {
            font-family: 'Plus Jakarta Sans', sans-serif;
            background-color: #0b0f19;
            color: #f8fafc;
        }
        .glass-card {
            background: rgba(17, 24, 39, 0.75);
            backdrop-filter: blur(16px);
            border: 1px solid rgba(255, 255, 255, 0.08);
        }
    </style>
</head>
<body class="min-h-screen flex flex-col justify-between selection:bg-sky-500 selection:text-white">

    <!-- Navbar -->
    <header class="w-full border-b border-slate-800/80 bg-slate-900/60 backdrop-blur-md sticky top-0 z-50">
        <div class="max-w-4xl mx-auto px-4 h-16 flex items-center justify-between">
            <div class="flex items-center space-x-3">
                <img src="/icon.png" alt="SonicFlow Logo" class="w-10 h-10 rounded-xl shadow-lg shadow-indigo-500/25 border border-white/10 hover:scale-105 transition-transform object-cover">
                <span class="text-xl font-bold tracking-tight bg-gradient-to-r from-white via-indigo-200 to-sky-300 bg-clip-text text-transparent">SonicFlow</span>
            </div>
            <div class="flex items-center space-x-2">
                <span class="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                    <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 mr-1.5 animate-pulse"></span>
                    Online
                </span>
            </div>
        </div>
    </header>

    <!-- Main Container -->
    <main class="flex-1 flex items-center justify-center p-4 sm:p-6 my-6">
        <div class="w-full max-w-xl mx-auto">

            <!-- Hero Text with Logo -->
            <div class="text-center mb-8">
                <div class="inline-flex items-center justify-center mb-4 relative group">
                    <div class="absolute -inset-1.5 bg-gradient-to-r from-cyan-500 via-indigo-500 to-fuchsia-500 rounded-3xl blur-md opacity-50 group-hover:opacity-85 transition duration-500"></div>
                    <img src="/icon.png" alt="SonicFlow" class="relative w-20 h-20 rounded-2xl shadow-2xl border border-white/20 transform group-hover:scale-105 transition duration-300 object-cover">
                </div>
                <h1 class="text-3xl sm:text-4xl font-extrabold tracking-tight text-white mb-3">
                    Sonic<span class="bg-gradient-to-r from-cyan-400 via-indigo-400 to-fuchsia-400 bg-clip-text text-transparent">Flow</span>
                </h1>
                <p class="text-slate-400 text-sm sm:text-base max-w-md mx-auto">
                    Convert YouTube & Instagram links to high-quality audio (MP3, FLAC) or HD/4K MP4 video.
                </p>

                <!-- Supported Platform Badges -->
                <div class="flex items-center justify-center gap-2 mt-4">
                    <span class="px-3 py-1 rounded-lg text-xs font-semibold bg-red-500/10 text-red-400 border border-red-500/20 flex items-center gap-1.5">
                        <i class="fa-brands fa-youtube text-red-500"></i> YouTube
                    </span>
                    <span class="px-3 py-1 rounded-lg text-xs font-semibold bg-pink-500/10 text-pink-400 border border-pink-500/20 flex items-center gap-1.5">
                        <i class="fa-brands fa-instagram text-pink-400"></i> Instagram Reels
                    </span>
                    <span class="px-3 py-1 rounded-lg text-xs font-semibold bg-indigo-500/10 text-indigo-400 border border-indigo-500/20 flex items-center gap-1.5">
                        <i class="fa-solid fa-video text-indigo-400"></i> Up to 4K
                    </span>
                </div>
            </div>

            <!-- Conversion Card -->
            <div class="glass-card rounded-2xl p-5 sm:p-7 shadow-2xl shadow-black/50">
                
                <!-- URL Input Group -->
                <div class="mb-5">
                    <label class="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">Media URL</label>
                    <div class="relative">
                        <input 
                            type="text" 
                            id="urlInput"
                            placeholder="Paste YouTube or Instagram link here..."
                            class="w-full bg-slate-900/90 text-white placeholder-slate-500 rounded-xl px-4 py-3.5 pr-20 text-sm sm:text-base border border-slate-700/80 focus:outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500 transition shadow-inner"
                        />
                        <button 
                            id="pasteBtn"
                            onclick="handlePaste()"
                            class="absolute right-2 top-2 bottom-2 px-3 text-xs font-medium text-slate-300 bg-slate-800 hover:bg-slate-700 active:scale-95 rounded-lg border border-slate-700 transition flex items-center gap-1"
                        >
                            <i class="fa-regular fa-clipboard"></i>
                            <span class="hidden sm:inline">Paste</span>
                        </button>
                    </div>
                </div>

                <!-- Format & Quality Options Grid -->
                <div class="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-6">
                    <div>
                        <label class="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">Format</label>
                        <select 
                            id="formatSelect"
                            onchange="updateQualityOptions()"
                            class="w-full bg-slate-900/90 text-white rounded-xl px-4 py-3 text-sm border border-slate-700/80 focus:outline-none focus:border-sky-500 transition"
                        >
                            <optgroup label="Audio Formats">
                                <option value="mp3" selected>🎵 MP3 (Universal Audio)</option>
                                <option value="flac">💎 FLAC (Lossless Master)</option>
                                <option value="wav">🎼 WAV (Uncompressed)</option>
                                <option value="aac">🎧 AAC / M4A (Efficient)</option>
                                <option value="opus">⚡ OPUS (Next-Gen)</option>
                            </optgroup>
                            <optgroup label="Video Formats">
                                <option value="mp4">🎬 MP4 Video (480p to 4K)</option>
                            </optgroup>
                        </select>
                    </div>

                    <div>
                        <label id="qualityLabel" class="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">Audio Quality</label>
                        <select 
                            id="qualitySelect"
                            class="w-full bg-slate-900/90 text-white rounded-xl px-4 py-3 text-sm border border-slate-700/80 focus:outline-none focus:border-sky-500 transition"
                        >
                            <option value="320" selected>320 kbps (Maximum Quality)</option>
                            <option value="256">256 kbps (High Quality)</option>
                            <option value="192">192 kbps (Standard Quality)</option>
                            <option value="128">128 kbps (Compact)</option>
                        </select>
                    </div>
                </div>

                <!-- Convert Button -->
                <button 
                    id="submitBtn"
                    onclick="startConversion()"
                    class="w-full bg-gradient-to-r from-sky-500 to-indigo-600 hover:from-sky-400 hover:to-indigo-500 text-white font-semibold py-3.5 rounded-xl shadow-lg shadow-sky-500/25 active:scale-[0.99] transition flex items-center justify-center gap-2 text-base cursor-pointer"
                >
                    <i class="fa-solid fa-bolt"></i>
                    <span>Convert & Download Media</span>
                </button>

                <!-- Processing Box -->
                <div id="progressContainer" class="hidden mt-6 p-4 rounded-xl bg-slate-900/90 border border-slate-800">
                    <div class="flex items-center justify-between mb-2">
                        <span id="statusText" class="text-xs font-medium text-slate-300 truncate max-w-[280px] sm:max-w-sm">Initializing...</span>
                        <span id="pctText" class="text-xs font-bold text-sky-400">0%</span>
                    </div>
                    <!-- Bar -->
                    <div class="w-full bg-slate-800 rounded-full h-2 overflow-hidden mb-3">
                        <div id="progressBar" class="bg-gradient-to-r from-sky-500 to-indigo-500 h-2 rounded-full transition-all duration-300" style="width: 0%"></div>
                    </div>
                    
                    <!-- Download Ready Trigger -->
                    <div id="downloadReady" class="hidden text-center mt-4">
                        <a 
                            id="downloadLink" 
                            href="#" 
                            class="inline-flex items-center gap-2 bg-emerald-500 hover:bg-emerald-400 text-white font-bold px-6 py-3 rounded-xl shadow-lg shadow-emerald-500/30 transition text-sm active:scale-95"
                            download
                        >
                            <i class="fa-solid fa-cloud-arrow-down text-lg"></i>
                            <span id="downloadBtnText">Download File</span>
                        </a>
                    </div>
                </div>

                <!-- Error Box -->
                <div id="errorBox" class="hidden mt-4 p-3 rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-400 text-xs text-center"></div>

            </div>

            <!-- Features Highlights -->
            <div class="grid grid-cols-3 gap-3 mt-8 text-center text-slate-400 text-xs">
                <div class="p-3 rounded-xl bg-slate-900/40 border border-slate-800/60">
                    <i class="fa-solid fa-film text-sky-400 text-base mb-1 block"></i>
                    Up to 4K MP4
                </div>
                <div class="p-3 rounded-xl bg-slate-900/40 border border-slate-800/60">
                    <i class="fa-solid fa-compact-disc text-indigo-400 text-base mb-1 block"></i>
                    Studio Audio
                </div>
                <div class="p-3 rounded-xl bg-slate-900/40 border border-slate-800/60">
                    <i class="fa-solid fa-shield-halved text-emerald-400 text-base mb-1 block"></i>
                    100% Free
                </div>
            </div>

        </div>
    </main>

    <!-- Footer -->
    <footer class="w-full text-center py-6 text-slate-600 text-xs border-t border-slate-800/60">
        SonicFlow &copy; 2026 &bull; High-Quality Media Downloader
    </footer>

    <!-- App Script -->
    <script>
        function updateQualityOptions() {
            const format = document.getElementById('formatSelect').value;
            const qualitySelect = document.getElementById('qualitySelect');
            const qualityLabel = document.getElementById('qualityLabel');

            if (format === 'mp4') {
                qualityLabel.textContent = "Video Resolution";
                qualitySelect.innerHTML = `
                    <option value="2160">2160p (4K Ultra HD)</option>
                    <option value="1440">1440p (2K Quad HD)</option>
                    <option value="1080" selected>1080p (Full HD)</option>
                    <option value="720">720p (HD)</option>
                    <option value="480">480p (Standard Definition)</option>
                    <option value="best">Best Available Resolution</option>
                `;
                qualitySelect.disabled = false;
            } else if (['flac', 'wav'].includes(format)) {
                qualityLabel.textContent = "Audio Quality";
                qualitySelect.innerHTML = '<option value="0" selected>Lossless (Best Original Fidelity)</option>';
                qualitySelect.disabled = true;
            } else if (format === 'mp3') {
                qualityLabel.textContent = "Audio Quality";
                qualitySelect.innerHTML = `
                    <option value="320" selected>320 kbps (Maximum Quality)</option>
                    <option value="256">256 kbps (High Quality)</option>
                    <option value="192">192 kbps (Standard Quality)</option>
                    <option value="128">128 kbps (Compact)</option>
                `;
                qualitySelect.disabled = false;
            } else if (format === 'aac') {
                qualityLabel.textContent = "Audio Quality";
                qualitySelect.innerHTML = `
                    <option value="320" selected>320 kbps (High Quality)</option>
                    <option value="256">256 kbps</option>
                    <option value="192">192 kbps</option>
                `;
                qualitySelect.disabled = false;
            } else if (format === 'opus') {
                qualityLabel.textContent = "Audio Quality";
                qualitySelect.innerHTML = `
                    <option value="160" selected>160 kbps (Transparent)</option>
                    <option value="128">128 kbps</option>
                    <option value="96">96 kbps</option>
                `;
                qualitySelect.disabled = false;
            }
        }

        async function handlePaste() {
            try {
                const text = await navigator.clipboard.readText();
                if (text) {
                    document.getElementById('urlInput').value = text.trim();
                }
            } catch (err) {
                document.getElementById('urlInput').focus();
            }
        }

        let pollInterval = null;

        async function startConversion() {
            const urlInput = document.getElementById('urlInput');
            const url = urlInput.value.trim();
            const format = document.getElementById('formatSelect').value;
            const quality = document.getElementById('qualitySelect').value;

            const errorBox = document.getElementById('errorBox');
            const progressContainer = document.getElementById('progressContainer');
            const progressBar = document.getElementById('progressBar');
            const statusText = document.getElementById('statusText');
            const pctText = document.getElementById('pctText');
            const downloadReady = document.getElementById('downloadReady');
            const submitBtn = document.getElementById('submitBtn');

            errorBox.classList.add('hidden');
            downloadReady.classList.add('hidden');

            if (!url) {
                errorBox.textContent = "Please enter a YouTube or Instagram link.";
                errorBox.classList.remove('hidden');
                return;
            }

            submitBtn.disabled = true;
            submitBtn.classList.add('opacity-50', 'cursor-not-allowed');
            progressContainer.classList.remove('hidden');
            progressBar.style.width = '5%';
            pctText.textContent = '5%';
            statusText.textContent = 'Submitting request...';

            try {
                const res = await fetch('/api/convert', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url, format, quality })
                });

                const data = await res.json();
                if (!res.ok) {
                    throw new Error(data.detail || 'Failed to initialize conversion.');
                }

                const jobId = data.job_id;
                pollJob(jobId);

            } catch (err) {
                errorBox.textContent = err.message;
                errorBox.classList.remove('hidden');
                submitBtn.disabled = false;
                submitBtn.classList.remove('opacity-50', 'cursor-not-allowed');
                progressContainer.classList.add('hidden');
            }
        }

        function pollJob(jobId) {
            if (pollInterval) clearInterval(pollInterval);

            pollInterval = setInterval(async () => {
                try {
                    const res = await fetch(`/api/status/${jobId}`);
                    if (!res.ok) return;

                    const job = await res.json();
                    const progressBar = document.getElementById('progressBar');
                    const statusText = document.getElementById('statusText');
                    const pctText = document.getElementById('pctText');
                    const downloadReady = document.getElementById('downloadReady');
                    const downloadLink = document.getElementById('downloadLink');
                    const downloadBtnText = document.getElementById('downloadBtnText');
                    const submitBtn = document.getElementById('submitBtn');
                    const errorBox = document.getElementById('errorBox');

                    progressBar.style.width = `${job.progress}%`;
                    pctText.textContent = `${job.progress}%`;
                    statusText.textContent = job.message || 'Processing...';

                    if (job.status === 'completed') {
                        clearInterval(pollInterval);
                        submitBtn.disabled = false;
                        submitBtn.classList.remove('opacity-50', 'cursor-not-allowed');

                        downloadLink.href = `/api/download/${jobId}`;
                        downloadBtnText.textContent = `Download: ${job.file_name}`;
                        downloadReady.classList.remove('hidden');
                    } else if (job.status === 'error') {
                        clearInterval(pollInterval);
                        submitBtn.disabled = false;
                        submitBtn.classList.remove('opacity-50', 'cursor-not-allowed');
                        errorBox.textContent = job.message || 'Processing error occurred.';
                        errorBox.classList.remove('hidden');
                    }
                } catch (err) {
                    console.error("Polling error:", err);
                }
            }, 1000);
        }
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return HTMLResponse(content=HTML_CONTENT)


@app.head("/")
async def head_index():
    return HTMLResponse(content="")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
