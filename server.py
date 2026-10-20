#!/usr/bin/env python3
"""
SonicFlow Web Server — Studio-Grade YouTube & Instagram Media Extractor
FastAPI backend supporting both High-Fidelity Audio (MP3, FLAC, WAV, AAC, OPUS)
and Video (MP4 from 480p up to 4K Ultra HD) with hardened security,
intelligent challenge bypass, and an ultra-clean, modern UI.
"""

import os
import re
import sys
import time
import uuid
import shutil
import asyncio
import threading
import subprocess
import logging
from collections import deque
from pathlib import Path
from urllib.parse import urlparse
from typing import Dict, Any, Optional, Tuple, Set

from fastapi import FastAPI, BackgroundTasks, HTTPException, Query, Header
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="SonicFlow Web", version="1.3.1")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("sonicflow")

# Security: Wildcard origins without credentials prevents cross-origin session hijacking
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)

TEMP_DIR = Path("/tmp/sonicflow_web")
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# In-memory job state store and concurrency controls
jobs: Dict[str, Dict[str, Any]] = {}
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "1"))
_active_jobs_count = 0
_jobs_lock = threading.Lock()

ALLOWED_HOSTS: Set[str] = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "instagram.com",
    "www.instagram.com",
    "instagr.am",
}

ALLOWED_FORMATS: Set[str] = {"mp3", "flac", "wav", "aac", "opus", "alac", "mp4"}
ALLOWED_QUALITIES: Set[str] = {
    "320", "256", "192", "160", "128", "96", "0",
    "480", "720", "1080", "1440", "2160", "best"
}


def find_ffmpeg() -> str:
    """Detect ffmpeg binary location across macOS, Linux, and Windows."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = [
        # macOS / Linux
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
        # Windows standard locations
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
        os.path.expandvars(r"%USERPROFILE%\scoop\shims\ffmpeg.exe"),
        str(Path(__file__).parent / "ffmpeg.exe"),
        str(Path(__file__).parent / "ffmpeg"),
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return "ffmpeg"


def validate_and_sanitize_url(raw_url: str) -> str:
    """
    Strict URL validation to defend against SSRF, internal network probing,
    and command-line argument injection.
    """
    url = (raw_url or "").strip()
    if not url or len(url) > 2048:
        raise ValueError("Invalid URL: must be non-empty and under 2048 characters.")

    # Guard against command line flag injection
    if url.startswith("-"):
        raise ValueError("Security error: URL cannot start with a dash.")

    try:
        parsed = urlparse(url)
    except Exception:
        raise ValueError("Malformed URL.")

    if parsed.scheme != "https":
        raise ValueError("Invalid protocol: only HTTP and HTTPS are permitted.")

    if parsed.username or parsed.password or parsed.port:
        raise ValueError("Invalid URL authority.")
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError("URL is missing a valid host.")

    # Block local loopback and cloud metadata IPs to defend against SSRF
    blocked_hosts = {
        "localhost", "127.0.0.1", "0.0.0.0", "::1",
        "169.254.169.254", "metadata.google.internal"
    }
    if (
        hostname in blocked_hosts
        or hostname.startswith("127.")
        or hostname.startswith("10.")
        or hostname.startswith("192.168.")
        or hostname.startswith("172.16.")
    ):
        raise ValueError("Security violation: requests to private or loopback networks are strictly forbidden.")

    # Validate against allowed streaming platforms
    is_allowed = any(hostname == h or hostname.endswith("." + h) for h in ALLOWED_HOSTS)
    if not is_allowed:
        raise ValueError("Unsupported platform: only YouTube and Instagram links are supported.")

    # Normalize YouTube Music & youtu.be shortlinks
    url = url.replace("music.youtube.com", "www.youtube.com")
    if "youtu.be/" in url:
        try:
            video_id = url.split("youtu.be/")[1].split("?")[0].split("&")[0]
            if re.match(r'^[a-zA-Z0-9_-]{6,15}$', video_id):
                url = f"https://www.youtube.com/watch?v={video_id}"
        except Exception:
            pass

    # Strip surveillance/tracking parameters (si, igsh, utm_*, fbclid)
    url = re.sub(r'([?&])(si|igsh|utm_[a-zA-Z0-9_]+|fbclid)=[^&#]*', r'\1', url)
    url = url.replace("?&", "?").rstrip("?&")
    return url


class ConvertRequest(BaseModel):
    url: str
    format: str = "mp3"
    quality: str = "320"


def cleanup_old_files():
    """Remove converted jobs older than 15 minutes to preserve storage and privacy."""
    now = time.time()
    expired_jobs = []
    with _jobs_lock:
        for jid, job in list(jobs.items()):
            if job.get("status") in ("completed", "error") and now - job.get("created_at", now) > 900:
                expired_jobs.append(jid)
                job_folder = TEMP_DIR / jid
                if job_folder.exists():
                    shutil.rmtree(job_folder, ignore_errors=True)

        for jid in expired_jobs:
            jobs.pop(jid, None)


def check_cookie_auth(path: Path) -> Tuple[bool, str]:
    """Check if cookie file contains authenticated YouTube session tokens."""
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
    # 0. Check environment variables first
    raw_env_cookie = (
        os.environ.get("YTDLP_COOKIES")
        or os.environ.get("YOUTUBE_COOKIES")
        or os.environ.get("YT_COOKIES")
        or os.environ.get("COOKIES")
    )
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
        except Exception as e:
            print(f"Error parsing cookies env var: {e}")

    # 1. Explicit env var path
    if os.environ.get("COOKIES_PATH"):
        p = Path(os.environ["COOKIES_PATH"])
        if p.is_file() and p.stat().st_size > 10:
            is_auth, note = check_cookie_auth(p)
            return p, f"Loaded from COOKIES_PATH: {p} ({p.stat().st_size} bytes, {note})"

    base_dir = Path(__file__).resolve().parent
    candidates: list[Path] = [
        Path("/etc/secrets/finalcookies.txt"),
        Path("/etc/secrets/cookies.txt"),
        Path("/etc/secrets/www.youtube.com_cookies.txt"),
        Path("/tmp/sonicflow_cookies.txt"),
        Path("/tmp/cookies.txt"),
        base_dir / "finalcookies.txt",
        base_dir / "cookies.txt",
        base_dir / "www.youtube.com_cookies.txt",
        Path("finalcookies.txt"),
        Path("cookies.txt"),
        Path("www.youtube.com_cookies.txt"),
        Path("/app/finalcookies.txt"),
        Path("/app/cookies.txt"),
        Path("/app/www.youtube.com_cookies.txt"),
    ]

    # Check Render Secret Files directory (/etc/secrets)
    secrets_dir = Path("/etc/secrets")
    if secrets_dir.exists() and secrets_dir.is_dir():
        for f in secrets_dir.rglob("*"):
            if f.is_file() and f.stat().st_size > 10 and f not in candidates:
                candidates.insert(0, f)

    for base in [Path("."), Path("/app"), base_dir]:
        if base.exists() and base.is_dir():
            for f in base.glob("*cookie*.txt"):
                if f not in candidates:
                    candidates.append(f)

    existing = [c for c in candidates if c.exists() and c.is_file() and c.stat().st_size > 10]
    authenticated_candidates = [c for c in existing if check_cookie_auth(c)[0]]
    chosen = authenticated_candidates[0] if authenticated_candidates else (existing[0] if existing else None)

    if chosen:
        is_auth, note = check_cookie_auth(chosen)
        return chosen, f"Found cookie file: {chosen} ({chosen.stat().st_size} bytes, {note})"

    return None, "No cookies detected"


def run_yt_dlp_worker(job_id: str, url: str, fmt: str, quality: str):
    """Single bounded subprocess, with Render-visible logs and compatible job states."""
    global _active_jobs_count
    folder = TEMP_DIR / job_id
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with _jobs_lock:
            jobs[job_id].update(status="processing", progress=10, message="Connecting to media source...")

        output_template = str(folder / "%(title)s.%(ext)s")
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--no-playlist", "--newline", "--no-overwrites",
            "--retries", "2", "--fragment-retries", "2",
            "--socket-timeout", "25", "--max-filesize", "500M",
            "--no-progress" if os.getenv("SONICFLOW_NO_PROGRESS") == "1" else "--progress",
            "-o", output_template,
        ]
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            cmd.extend(["--ffmpeg-location", str(Path(ffmpeg).parent)])

        # Attach authenticated cookies if present (Secret files: finalcookies.txt, cookies.txt)
        found_cookie, cookie_info = find_cookie_file()
        if found_cookie:
            cmd.extend(["--cookies", str(found_cookie.resolve())])
            logger.info("job=%s attached cookies: %s", job_id, cookie_info)
        else:
            logger.warning("job=%s no cookies detected: %s", job_id, cookie_info)

        # Platform-specific extractor optimizations
        url_lower = url.lower()
        if "youtube.com" in url_lower or "youtu.be" in url_lower:
            cmd.extend(["--extractor-args", "youtube:player_client=android,web,mweb"])
        elif "instagram.com" in url_lower or "instagr.am" in url_lower:
            cmd.extend([
                "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "--add-header", "Accept-Language: en-US,en;q=0.9",
            ])

        if fmt == "mp4":
            resolution = quality if quality in {"480", "720", "1080", "1440", "2160"} else "1080"
            cmd.extend([
                "-f", f"bv*[height<={resolution}]+ba/b[height<={resolution}]/best",
                "--merge-output-format", "mp4",
            ])
        else:
            q = quality if quality in {"96", "128", "160", "192", "256", "320"} else "192"
            cmd.extend(["-f", "bestaudio/best", "-x", "--audio-format", fmt,
                        "--audio-quality", f"{q}K"])

        cmd.extend(["--", url])
        logger.info("job=%s starting platform=%s format=%s quality=%s deno=%s ffmpeg=%s cookies=%s",
                    job_id, urlparse(url).hostname, fmt, quality,
                    bool(shutil.which("deno")), bool(ffmpeg), bool(found_cookie))

        tail = deque(maxlen=35)
        timed_out = False
        try:
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, bufsize=1, errors="replace", start_new_session=True) as proc:
                import signal
                # Kill runaway yt-dlp/ffmpeg process groups even if the server stays alive.
                timeout_hit = threading.Event()
                def kill_overdue():
                    if proc.poll() is None:
                        timeout_hit.set()
                        os.killpg(proc.pid, signal.SIGKILL)
                timer = threading.Timer(720, kill_overdue)
                timer.daemon = True
                timer.start()
                try:
                    for line in proc.stdout:
                        clean = line.strip()
                        if not clean:
                            continue
                        tail.append(clean)
                        if "ERROR:" in clean or "WARNING:" in clean or "[youtube]" in clean:
                            logger.warning("job=%s %s", job_id, clean[:800])
                        if "[download]" in clean and "%" in clean:
                            match = re.search(r"(\d+(?:\.\d+)?)%", clean)
                            if match:
                                with _jobs_lock:
                                    jobs[job_id].update(progress=min(85, 10 + int(float(match.group(1)) * .75)),
                                                        message="Downloading media...")
                        elif "[ExtractAudio]" in clean or "[Merger]" in clean or "[ffmpeg]" in clean:
                            with _jobs_lock:
                                jobs[job_id].update(progress=92, message="Converting media...")
                    code = proc.wait()
                finally:
                    timed_out = timeout_hit.is_set()
                    timer.cancel()
        except Exception:
            logger.exception("job=%s subprocess exception", job_id)
            raise

        if code != 0:
            detail = " | ".join(tail)[-1800:]
            logger.error("job=%s yt-dlp exit=%s timed_out=%s output=%s", job_id, code, timed_out, detail)
            lower = detail.lower()
            cookie_badge = f" [Cookies: {found_cookie.name}]" if found_cookie else " [Cookies: none detected]"
            if "confirm you're not a bot" in lower or "sign in to confirm" in lower:
                message = f"YouTube requested verification from this server{cookie_badge}. Ensure cookies are mounted in Render Secret Files."
            elif "403" in lower or "forbidden" in lower:
                message = f"Media server returned HTTP 403{cookie_badge}. The stream token was revoked or expired."
            elif "requested format is not available" in lower:
                message = "Selected format or quality is unavailable. Try another resolution."
            elif timed_out:
                message = "Conversion timed out on the server. Try a shorter video."
            else:
                message = f"Download failed{cookie_badge}. Check Render logs for the yt-dlp error."
            with _jobs_lock:
                jobs[job_id].update(status="error", message=message)
            return

        files = [f for f in folder.iterdir() if f.is_file() and
                 f.suffix.lower() == f".{fmt}" and f.stat().st_size > 0]
        if not files:
            logger.error("job=%s yt-dlp completed but expected .%s output was not produced", job_id, fmt)
            with _jobs_lock:
                jobs[job_id].update(status="error", message="Conversion completed but output format was not produced. Check FFmpeg logs.")
            return
        result = max(files, key=lambda f: f.stat().st_size)
        with _jobs_lock:
            jobs[job_id].update(status="completed", progress=100, message="Ready to download!",
                                file_path=str(result), file_name=result.name,
                                file_size=result.stat().st_size)
        logger.info("job=%s success bytes=%s", job_id, result.stat().st_size)
    except Exception:
        logger.exception("job=%s unexpected failure", job_id)
        with _jobs_lock:
            if job_id in jobs:
                jobs[job_id].update(status="error", message="Unexpected backend error. See Render logs.")
    finally:
        with _jobs_lock:
            _active_jobs_count = max(0, _active_jobs_count - 1)


@app.post("/api/convert")
async def start_conversion(payload: ConvertRequest, background_tasks: BackgroundTasks):
    global _active_jobs_count
    cleanup_old_files()

    # 1. URL validation and defense
    try:
        clean_url = validate_and_sanitize_url(payload.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 2. Format & quality parameter whitelisting
    fmt = payload.format.strip().lower()
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid format '{fmt}'. Allowed: {', '.join(sorted(ALLOWED_FORMATS))}"
        )

    quality = payload.quality.strip().lower()
    if quality not in ALLOWED_QUALITIES:
        quality = "320" if fmt != "mp4" else "1080"

    # 3. Concurrency guard to prevent Denial of Service
    with _jobs_lock:
        if _active_jobs_count >= MAX_CONCURRENT_JOBS:
            raise HTTPException(
                status_code=429,
                detail="Server is busy processing media conversions. Please retry in 15 seconds."
            )
        _active_jobs_count += 1

    job_id = str(uuid.uuid4()).replace("-", "")[:12]
    with _jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "progress": 0,
            "message": "Initializing...",
            "created_at": time.time(),
            "file_path": None,
            "file_name": None,
            "format": fmt,
            "quality": quality,
        }

    background_tasks.add_task(run_yt_dlp_worker, job_id, clean_url, fmt, quality)
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/status/{job_id}")
async def get_job_status(job_id: str):
    if not re.fullmatch(r'^[a-zA-Z0-9_-]{1,64}$', job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format.")

    with _jobs_lock:
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail="Job not found or expired.")
        return jobs[job_id]


@app.get("/api/download/{job_id}")
async def download_file(job_id: str):
    if not re.fullmatch(r'^[a-zA-Z0-9_-]{1,64}$', job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID format.")

    with _jobs_lock:
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail="Download expired or not found.")
        job = jobs[job_id]

    if job.get("status") != "completed" or not job.get("file_path"):
        raise HTTPException(status_code=400, detail="File is not ready yet.")

    file_path = Path(job["file_path"]).resolve()
    base_sandbox = (TEMP_DIR / job_id).resolve()

    # Sandboxing check: ensure file is strictly inside TEMP_DIR/job_id
    try:
        file_path.relative_to(base_sandbox)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied: file path is outside allowed sandbox.")

    if not file_path.exists() or not file_path.is_file():
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

    # Sanitize header filename to prevent CRLF injection / header splitting
    raw_name = Path(job.get("file_name") or file_path.name).name
    safe_name = re.sub(r'[\r\n\x00-\x1f"/\\:*?<>|]', '_', raw_name)

    return FileResponse(
        path=str(file_path),
        filename=safe_name,
        media_type=media_type
    )


@app.get("/health")
def health():
    return {"status": "ok", "service": "sonicflow"}


@app.get("/api/diagnostics")
def get_diagnostics(token: Optional[str] = Query(None), x_admin_token: Optional[str] = Header(None)):
    """
    Diagnostic endpoint.
    Public callers only receive high-level service status.
    Detailed filesystem and cookie paths require admin authentication.
    """
    admin_secret = os.environ.get("ADMIN_TOKEN") or os.environ.get("DIAGNOSTICS_TOKEN")
    caller_token = x_admin_token or token
    is_authenticated_admin = bool(admin_secret and caller_token and caller_token == admin_secret)

    ffmpeg_bin = find_ffmpeg()
    node_bin = shutil.which("deno")
    found_cookie, cookie_info = find_cookie_file()
    is_auth, _ = check_cookie_auth(found_cookie) if found_cookie else (False, "none")

    summary = {
        "status": "online",
        "service": "SonicFlow",
        "version": "v1.3.1",
        "ffmpeg_ready": bool(ffmpeg_bin and (os.path.exists(ffmpeg_bin) or shutil.which(ffmpeg_bin))),
        "engine_ready": bool(node_bin),
        "auth_ready": is_auth,
    }

    if is_authenticated_admin:
        secrets_path = Path("/etc/secrets")
        secrets_list = []
        if secrets_path.exists() and secrets_path.is_dir():
            try:
                secrets_list = [f.name for f in secrets_path.iterdir() if f.is_file()]
            except Exception:
                pass

        summary.update({
            "cookie_found": found_cookie is not None,
            "cookie_source": cookie_info,
            "cookie_file_size_bytes": found_cookie.stat().st_size if found_cookie and found_cookie.exists() else 0,
            "ffmpeg_path": ffmpeg_bin,
            "deno_path": node_bin,
            "secrets_files": secrets_list,
        })

    return summary


@app.get("/icon.png")
async def get_icon():
    for p in [Path("icon.png"), Path("/app/icon.png")]:
        if p.exists() and p.is_file():
            return FileResponse(str(p), media_type="image/png")
    raise HTTPException(status_code=404, detail="Icon not found")


@app.get("/favicon.ico")
async def get_favicon():
    for p in [Path("SonicFlow.ico"), Path("icon.png"), Path("/app/icon.png")]:
        if p.exists() and p.is_file():
            media = "image/x-icon" if p.suffix == ".ico" else "image/png"
            return FileResponse(str(p), media_type=media)
    raise HTTPException(status_code=404, detail="Favicon not found")


# -------------------------------------------------------------------------
# Modern, Tactile, Non-AI Frontend (Tailwind + Vanilla JS)
# -------------------------------------------------------------------------
HTML_CONTENT = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>SonicFlow — Studio Media Downloader</title>
    <link rel="icon" type="image/png" href="/icon.png">
    <link rel="apple-touch-icon" href="/icon.png">
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    fontFamily: {
                        sans: ['"Plus Jakarta Sans"', 'system-ui', 'sans-serif'],
                        mono: ['"JetBrains Mono"', 'monospace'],
                    },
                    colors: {
                        obsidian: {
                            950: '#07090e',
                            900: '#0c1017',
                            850: '#111622',
                            800: '#171f2e',
                            700: '#222d42',
                        },
                        accent: {
                            400: '#38bdf8',
                            500: '#0284c7',
                            600: '#0369a1',
                        }
                    }
                }
            }
        }
    </script>
    <style>
        body {
            background-color: #07090e;
            background-image: 
                radial-gradient(circle at 50% -10%, rgba(56, 189, 248, 0.08) 0%, transparent 50%),
                radial-gradient(circle at 85% 60%, rgba(99, 102, 241, 0.04) 0%, transparent 40%);
            color: #f1f5f9;
            -webkit-font-smoothing: antialiased;
        }
        .studio-card {
            background: rgba(17, 22, 34, 0.75);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid rgba(255, 255, 255, 0.07);
        }
        .studio-card-active {
            border-color: rgba(56, 189, 248, 0.3);
            box-shadow: 0 0 25px -5px rgba(2, 132, 199, 0.15);
        }
        .pill-tab-active {
            background-color: #0284c7;
            color: #ffffff;
            box-shadow: 0 2px 10px rgba(2, 132, 199, 0.35);
        }
        .chip-active {
            background-color: rgba(56, 189, 248, 0.15);
            border-color: #38bdf8;
            color: #38bdf8;
        }
        input:focus {
            box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.3);
        }
        /* Custom smooth transition for progress bar */
        #progressBar {
            transition: width 0.4s cubic-bezier(0.4, 0, 0.2, 1);
        }
    </style>
</head>
<body class="min-h-screen flex flex-col justify-between selection:bg-sky-500 selection:text-white">

    <!-- Header Navigation -->
    <header class="w-full border-b border-white/[0.06] bg-obsidian-950/70 backdrop-blur-xl sticky top-0 z-50">
        <div class="max-w-4xl mx-auto px-4 h-16 flex items-center justify-between">
            <div class="flex items-center space-x-3 cursor-pointer" onclick="window.location.reload()">
                <img src="/icon.png" alt="SonicFlow Logo" class="w-9 h-9 rounded-xl shadow-md border border-white/10 object-cover">
                <div class="flex items-baseline space-x-2">
                    <span class="text-lg font-bold tracking-tight text-white">SonicFlow</span>
                    <span class="text-[11px] font-medium text-slate-400 font-mono">v1.3</span>
                </div>
            </div>
            <div class="flex items-center space-x-3">
                <span class="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                    <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 mr-2 animate-pulse"></span>
                    Ready
                </span>
            </div>
        </div>
    </header>

    <!-- Main Workspace -->
    <main class="flex-1 flex items-center justify-center p-4 sm:p-6 my-4 sm:my-8">
        <div class="w-full max-w-xl mx-auto">

            <!-- Hero Title -->
            <div class="text-center mb-6">
                <h1 class="text-2xl sm:text-3xl font-extrabold tracking-tight text-white mb-2">
                    Media Downloader & Converter
                </h1>
                <p class="text-slate-400 text-xs sm:text-sm max-w-md mx-auto">
                    Clean, studio-quality audio and video exports from YouTube and Instagram.
                </p>
            </div>

            <!-- Main Interactive Card -->
            <div class="studio-card rounded-2xl p-5 sm:p-7 shadow-2xl shadow-black/60 relative">

                <!-- Mode Switcher (Audio vs Video) -->
                <div class="flex items-center justify-center p-1 bg-obsidian-900/90 rounded-xl border border-white/[0.06] mb-5">
                    <button 
                        id="audioTabBtn"
                        onclick="setMode('audio')"
                        class="flex-1 py-2 text-xs sm:text-sm font-semibold rounded-lg transition-all flex items-center justify-center gap-2 pill-tab-active"
                    >
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3"/>
                        </svg>
                        <span>Audio Extractor</span>
                    </button>
                    <button 
                        id="videoTabBtn"
                        onclick="setMode('video')"
                        class="flex-1 py-2 text-xs sm:text-sm font-semibold rounded-lg transition-all flex items-center justify-center gap-2 text-slate-400 hover:text-white"
                    >
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/>
                        </svg>
                        <span>Video (Up to 4K)</span>
                    </button>
                </div>

                <!-- URL Input Container -->
                <div class="mb-5">
                    <div class="flex items-center justify-between mb-2">
                        <label class="text-xs font-semibold text-slate-300 uppercase tracking-wider">Media URL</label>
                        <!-- Dynamic Platform Indicator Badge -->
                        <span id="platformBadge" class="hidden text-[11px] font-semibold px-2 py-0.5 rounded-md border flex items-center gap-1.5 transition-all"></span>
                    </div>
                    <div class="relative flex items-center">
                        <input 
                            type="text" 
                            id="urlInput"
                            placeholder="Paste YouTube or Instagram link here..."
                            autocomplete="off"
                            spellcheck="false"
                            oninput="handleUrlInput()"
                            onkeydown="if(event.key === 'Enter') startConversion()"
                            class="w-full bg-obsidian-900/90 text-white placeholder-slate-500 rounded-xl pl-4 pr-24 py-3.5 text-sm sm:text-base border border-white/[0.08] focus:outline-none focus:border-sky-500 transition shadow-inner font-sans"
                        />
                        <div class="absolute right-2 flex items-center space-x-1">
                            <button 
                                id="clearBtn"
                                onclick="clearInput()"
                                class="hidden p-2 text-xs text-slate-400 hover:text-white rounded-lg transition"
                                title="Clear"
                            >
                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
                            </button>
                            <button 
                                id="pasteBtn"
                                onclick="handlePaste()"
                                class="px-2.5 py-1.5 text-xs font-medium text-slate-300 bg-obsidian-800 hover:bg-obsidian-700 active:scale-95 rounded-lg border border-white/10 transition flex items-center gap-1"
                            >
                                <svg class="w-3.5 h-3.5 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/></svg>
                                <span>Paste</span>
                            </button>
                        </div>
                    </div>
                </div>

                <!-- Audio Settings Subpanel -->
                <div id="audioSettingsPanel" class="space-y-4 mb-6">
                    <div>
                        <label class="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">Audio Format</label>
                        <div class="grid grid-cols-5 gap-2">
                            <button type="button" onclick="selectFormat('mp3')" id="fmt-mp3" class="format-chip py-2 px-1 text-xs font-semibold rounded-lg border border-white/[0.08] bg-obsidian-900/60 hover:bg-obsidian-800 transition text-center chip-active">MP3</button>
                            <button type="button" onclick="selectFormat('flac')" id="fmt-flac" class="format-chip py-2 px-1 text-xs font-semibold rounded-lg border border-white/[0.08] bg-obsidian-900/60 hover:bg-obsidian-800 transition text-center text-slate-300">FLAC</button>
                            <button type="button" onclick="selectFormat('wav')" id="fmt-wav" class="format-chip py-2 px-1 text-xs font-semibold rounded-lg border border-white/[0.08] bg-obsidian-900/60 hover:bg-obsidian-800 transition text-center text-slate-300">WAV</button>
                            <button type="button" onclick="selectFormat('aac')" id="fmt-aac" class="format-chip py-2 px-1 text-xs font-semibold rounded-lg border border-white/[0.08] bg-obsidian-900/60 hover:bg-obsidian-800 transition text-center text-slate-300">AAC</button>
                            <button type="button" onclick="selectFormat('opus')" id="fmt-opus" class="format-chip py-2 px-1 text-xs font-semibold rounded-lg border border-white/[0.08] bg-obsidian-900/60 hover:bg-obsidian-800 transition text-center text-slate-300">OPUS</button>
                        </div>
                    </div>

                    <div id="bitrateSection">
                        <label class="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">Audio Bitrate</label>
                        <div class="grid grid-cols-4 gap-2">
                            <button type="button" onclick="selectQuality('320')" id="q-320" class="quality-chip py-2 text-xs font-medium rounded-lg border border-white/[0.08] bg-obsidian-900/60 hover:bg-obsidian-800 transition text-center chip-active">320 kbps</button>
                            <button type="button" onclick="selectQuality('256')" id="q-256" class="quality-chip py-2 text-xs font-medium rounded-lg border border-white/[0.08] bg-obsidian-900/60 hover:bg-obsidian-800 transition text-center text-slate-300">256 kbps</button>
                            <button type="button" onclick="selectQuality('192')" id="q-192" class="quality-chip py-2 text-xs font-medium rounded-lg border border-white/[0.08] bg-obsidian-900/60 hover:bg-obsidian-800 transition text-center text-slate-300">192 kbps</button>
                            <button type="button" onclick="selectQuality('128')" id="q-128" class="quality-chip py-2 text-xs font-medium rounded-lg border border-white/[0.08] bg-obsidian-900/60 hover:bg-obsidian-800 transition text-center text-slate-300">128 kbps</button>
                        </div>
                    </div>

                    <div id="losslessNotice" class="hidden py-2 px-3 rounded-lg bg-obsidian-900/60 border border-white/[0.08] text-xs text-slate-400">
                        <span class="text-sky-400 font-semibold">Lossless Audio:</span> Retains original uncompressed master sample rate & dynamic range.
                    </div>
                </div>

                <!-- Video Settings Subpanel -->
                <div id="videoSettingsPanel" class="hidden space-y-4 mb-6">
                    <div>
                        <label class="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">Video Resolution</label>
                        <div class="grid grid-cols-5 gap-2">
                            <button type="button" onclick="selectQuality('2160')" id="q-2160" class="quality-chip py-2 text-xs font-medium rounded-lg border border-white/[0.08] bg-obsidian-900/60 hover:bg-obsidian-800 transition text-center text-slate-300">4K</button>
                            <button type="button" onclick="selectQuality('1440')" id="q-1440" class="quality-chip py-2 text-xs font-medium rounded-lg border border-white/[0.08] bg-obsidian-900/60 hover:bg-obsidian-800 transition text-center text-slate-300">1440p</button>
                            <button type="button" onclick="selectQuality('1080')" id="q-1080" class="quality-chip py-2 text-xs font-medium rounded-lg border border-white/[0.08] bg-obsidian-900/60 hover:bg-obsidian-800 transition text-center chip-active">1080p</button>
                            <button type="button" onclick="selectQuality('720')" id="q-720" class="quality-chip py-2 text-xs font-medium rounded-lg border border-white/[0.08] bg-obsidian-900/60 hover:bg-obsidian-800 transition text-center text-slate-300">720p</button>
                            <button type="button" onclick="selectQuality('480')" id="q-480" class="quality-chip py-2 text-xs font-medium rounded-lg border border-white/[0.08] bg-obsidian-900/60 hover:bg-obsidian-800 transition text-center text-slate-300">480p</button>
                        </div>
                    </div>
                    <div class="py-2 px-3 rounded-lg bg-obsidian-900/60 border border-white/[0.08] text-xs text-slate-400 flex items-center justify-between">
                        <span>Format: <strong class="text-white font-medium">MP4</strong> (H.264 Video + AAC Audio)</span>
                        <span class="text-emerald-400 font-medium">Native Playback</span>
                    </div>
                </div>

                <!-- Action Button -->
                <button 
                    id="submitBtn"
                    onclick="startConversion()"
                    class="w-full bg-accent-500 hover:bg-accent-600 active:scale-[0.99] text-white font-semibold py-3.5 rounded-xl shadow-lg shadow-sky-600/25 transition-all flex items-center justify-center gap-2 text-sm sm:text-base cursor-pointer"
                >
                    <svg id="btnIcon" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/></svg>
                    <span id="btnText">Extract & Download</span>
                </button>

                <!-- Real-time Progress Display -->
                <div id="progressContainer" class="hidden mt-5 p-4 rounded-xl bg-obsidian-900/90 border border-white/[0.08]">
                    <div class="flex items-center justify-between mb-2">
                        <span id="statusText" class="text-xs font-medium text-slate-300 truncate max-w-[260px] sm:max-w-sm">Initializing conversion...</span>
                        <span id="pctText" class="text-xs font-mono font-bold text-sky-400">0%</span>
                    </div>
                    <div class="w-full bg-obsidian-800 rounded-full h-2 overflow-hidden mb-1">
                        <div id="progressBar" class="bg-sky-500 h-2 rounded-full" style="width: 0%"></div>
                    </div>
                </div>

                <!-- Completion Card -->
                <div id="downloadReadyCard" class="hidden mt-5 p-4 rounded-xl bg-emerald-500/10 border border-emerald-500/20 text-center animate-fade-in">
                    <div class="inline-flex items-center justify-center w-10 h-10 rounded-full bg-emerald-500/20 text-emerald-400 mb-2">
                        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 13l4 4L19 7"/></svg>
                    </div>
                    <h3 id="completedFileName" class="text-sm font-semibold text-white truncate max-w-sm mx-auto mb-1">media_file.mp3</h3>
                    <p id="completedFileSize" class="text-xs text-slate-400 mb-4">Size: 0 MB</p>

                    <div class="flex flex-col sm:flex-row items-center justify-center gap-2">
                        <a 
                            id="downloadLink" 
                            href="#" 
                            class="w-full sm:w-auto inline-flex items-center justify-center gap-2 bg-emerald-500 hover:bg-emerald-400 text-white font-semibold px-6 py-2.5 rounded-xl shadow-lg shadow-emerald-500/20 transition text-sm active:scale-95"
                            download
                        >
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/></svg>
                            <span>Save to Device</span>
                        </a>
                        <button 
                            onclick="resetForm()"
                            class="w-full sm:w-auto px-4 py-2.5 text-xs font-medium text-slate-400 hover:text-white bg-obsidian-800 hover:bg-obsidian-700 rounded-xl border border-white/10 transition"
                        >
                            Convert Another
                        </button>
                    </div>
                </div>

                <!-- Error Notice Box -->
                <div id="errorBox" class="hidden mt-4 p-3 rounded-xl bg-rose-500/10 border border-rose-500/20 text-rose-400 text-xs text-center"></div>

            </div>

            <!-- Helpful Hints -->
            <div class="mt-6 text-center text-xs text-slate-500 space-y-1">
                <p>Supports YouTube Videos, Shorts, Playlists & Instagram Reels</p>
                <p class="font-mono text-[11px] text-slate-600">Private & local &bull; No ads &bull; Metadata embedded</p>
            </div>

        </div>
    </main>

    <!-- Clean Footer -->
    <footer class="w-full text-center py-5 text-slate-600 text-xs border-t border-white/[0.04]">
        SonicFlow &bull; High-Fidelity Audio & Video Engine &bull; 2026
    </footer>

    <!-- Frontend Interactive Logic -->
    <script>
        let currentMode = 'audio';
        let currentFormat = 'mp3';
        let currentQuality = '320';
        let pollInterval = null;

        function setMode(mode) {
            currentMode = mode;
            const audioBtn = document.getElementById('audioTabBtn');
            const videoBtn = document.getElementById('videoTabBtn');
            const audioSettings = document.getElementById('audioSettingsPanel');
            const videoSettings = document.getElementById('videoSettingsPanel');

            if (mode === 'audio') {
                audioBtn.className = "flex-1 py-2 text-xs sm:text-sm font-semibold rounded-lg transition-all flex items-center justify-center gap-2 pill-tab-active";
                videoBtn.className = "flex-1 py-2 text-xs sm:text-sm font-semibold rounded-lg transition-all flex items-center justify-center gap-2 text-slate-400 hover:text-white";
                audioSettings.classList.remove('hidden');
                videoSettings.classList.add('hidden');
                currentFormat = 'mp3';
                selectFormat('mp3');
            } else {
                videoBtn.className = "flex-1 py-2 text-xs sm:text-sm font-semibold rounded-lg transition-all flex items-center justify-center gap-2 pill-tab-active";
                audioBtn.className = "flex-1 py-2 text-xs sm:text-sm font-semibold rounded-lg transition-all flex items-center justify-center gap-2 text-slate-400 hover:text-white";
                videoSettings.classList.remove('hidden');
                audioSettings.classList.add('hidden');
                currentFormat = 'mp4';
                selectQuality('1080');
            }
        }

        function selectFormat(fmt) {
            currentFormat = fmt;
            document.querySelectorAll('.format-chip').forEach(el => {
                el.className = "format-chip py-2 px-1 text-xs font-semibold rounded-lg border border-white/[0.08] bg-obsidian-900/60 hover:bg-obsidian-800 transition text-center text-slate-300";
            });
            const activeEl = document.getElementById(`fmt-${fmt}`);
            if (activeEl) {
                activeEl.className = "format-chip py-2 px-1 text-xs font-semibold rounded-lg border border-sky-400 bg-sky-500/15 text-sky-400 transition text-center font-bold";
            }

            const bitrateSection = document.getElementById('bitrateSection');
            const losslessNotice = document.getElementById('losslessNotice');

            if (['flac', 'wav'].includes(fmt)) {
                bitrateSection.classList.add('hidden');
                losslessNotice.classList.remove('hidden');
                currentQuality = '0';
            } else {
                bitrateSection.classList.remove('hidden');
                losslessNotice.classList.add('hidden');
                if (currentQuality === '0') {
                    selectQuality('320');
                }
            }
        }

        function selectQuality(q) {
            currentQuality = q;
            document.querySelectorAll('.quality-chip').forEach(el => {
                el.className = "quality-chip py-2 text-xs font-medium rounded-lg border border-white/[0.08] bg-obsidian-900/60 hover:bg-obsidian-800 transition text-center text-slate-300";
            });
            const activeEl = document.getElementById(`q-${q}`);
            if (activeEl) {
                activeEl.className = "quality-chip py-2 text-xs font-medium rounded-lg border border-sky-400 bg-sky-500/15 text-sky-400 transition text-center font-bold";
            }
        }

        function handleUrlInput() {
            const input = document.getElementById('urlInput');
            const val = input.value.trim().toLowerCase();
            const badge = document.getElementById('platformBadge');
            const clearBtn = document.getElementById('clearBtn');

            if (val.length > 0) {
                clearBtn.classList.remove('hidden');
            } else {
                clearBtn.classList.add('hidden');
            }

            if (val.includes('youtube.com') || val.includes('youtu.be')) {
                badge.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-red-500"></span> YouTube`;
                badge.className = "text-[11px] font-semibold px-2 py-0.5 rounded-md border border-red-500/30 bg-red-500/10 text-red-400 flex items-center gap-1.5";
                badge.classList.remove('hidden');
            } else if (val.includes('instagram.com') || val.includes('instagr.am')) {
                badge.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-pink-500"></span> Instagram`;
                badge.className = "text-[11px] font-semibold px-2 py-0.5 rounded-md border border-pink-500/30 bg-pink-500/10 text-pink-400 flex items-center gap-1.5";
                badge.classList.remove('hidden');
            } else {
                badge.classList.add('hidden');
            }
        }

        function clearInput() {
            const input = document.getElementById('urlInput');
            input.value = '';
            handleUrlInput();
            input.focus();
        }

        async function handlePaste() {
            try {
                const text = await navigator.clipboard.readText();
                if (text) {
                    const input = document.getElementById('urlInput');
                    input.value = text.trim();
                    handleUrlInput();
                }
            } catch (err) {
                document.getElementById('urlInput').focus();
            }
        }

        function formatBytes(bytes, decimals = 1) {
            if (!+bytes) return '0 B';
            const k = 1024;
            const dm = decimals < 0 ? 0 : decimals;
            const sizes = ['B', 'KB', 'MB', 'GB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
        }

        function resetForm() {
            document.getElementById('urlInput').value = '';
            handleUrlInput();
            document.getElementById('progressContainer').classList.add('hidden');
            document.getElementById('downloadReadyCard').classList.add('hidden');
            document.getElementById('errorBox').classList.add('hidden');
            const submitBtn = document.getElementById('submitBtn');
            submitBtn.disabled = false;
            submitBtn.classList.remove('opacity-50', 'cursor-not-allowed');
            document.getElementById('btnText').textContent = 'Extract & Download';
        }

        async function startConversion() {
            const urlInput = document.getElementById('urlInput');
            const rawUrl = urlInput.value.trim();

            const errorBox = document.getElementById('errorBox');
            const progressContainer = document.getElementById('progressContainer');
            const progressBar = document.getElementById('progressBar');
            const statusText = document.getElementById('statusText');
            const pctText = document.getElementById('pctText');
            const downloadReadyCard = document.getElementById('downloadReadyCard');
            const submitBtn = document.getElementById('submitBtn');
            const btnText = document.getElementById('btnText');

            errorBox.classList.add('hidden');
            downloadReadyCard.classList.add('hidden');

            if (!rawUrl) {
                errorBox.textContent = "Please enter or paste a YouTube or Instagram link.";
                errorBox.classList.remove('hidden');
                return;
            }

            submitBtn.disabled = true;
            submitBtn.classList.add('opacity-50', 'cursor-not-allowed');
            btnText.textContent = 'Processing Media...';

            progressContainer.classList.remove('hidden');
            progressBar.style.width = '6%';
            pctText.textContent = '6%';
            statusText.textContent = 'Connecting to stream...';

            try {
                const res = await fetch('/api/convert', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ 
                        url: rawUrl, 
                        format: currentFormat, 
                        quality: currentQuality 
                    })
                });

                const data = await res.json();
                if (!res.ok) {
                    throw new Error(data.detail || 'Failed to initialize conversion.');
                }

                pollJob(data.job_id);

            } catch (err) {
                errorBox.textContent = err.message;
                errorBox.classList.remove('hidden');
                submitBtn.disabled = false;
                submitBtn.classList.remove('opacity-50', 'cursor-not-allowed');
                btnText.textContent = 'Extract & Download';
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
                    const downloadReadyCard = document.getElementById('downloadReadyCard');
                    const downloadLink = document.getElementById('downloadLink');
                    const completedFileName = document.getElementById('completedFileName');
                    const completedFileSize = document.getElementById('completedFileSize');
                    const submitBtn = document.getElementById('submitBtn');
                    const btnText = document.getElementById('btnText');
                    const errorBox = document.getElementById('errorBox');
                    const progressContainer = document.getElementById('progressContainer');

                    progressBar.style.width = `${job.progress}%`;
                    pctText.textContent = `${job.progress}%`;
                    statusText.textContent = job.message || 'Processing...';

                    if (job.status === 'completed') {
                        clearInterval(pollInterval);
                        submitBtn.disabled = false;
                        submitBtn.classList.remove('opacity-50', 'cursor-not-allowed');
                        btnText.textContent = 'Extract & Download';
                        progressContainer.classList.add('hidden');

                        downloadLink.href = `/api/download/${jobId}`;
                        completedFileName.textContent = job.file_name || 'Downloaded Media';
                        completedFileSize.textContent = `Size: ${formatBytes(job.file_size)} \u2022 Format: ${(job.format || currentFormat).toUpperCase()}`;
                        downloadReadyCard.classList.remove('hidden');

                    } else if (job.status === 'error') {
                        clearInterval(pollInterval);
                        submitBtn.disabled = false;
                        submitBtn.classList.remove('opacity-50', 'cursor-not-allowed');
                        btnText.textContent = 'Extract & Download';
                        progressContainer.classList.add('hidden');
                        errorBox.textContent = job.message || 'Media processing error occurred.';
                        errorBox.classList.remove('hidden');
                    }
                } catch (err) {
                    console.error("Polling error:", err);
                }
            }, 850);
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
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
