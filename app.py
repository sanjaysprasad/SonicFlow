#!/usr/bin/env python3
"""
SonicFlow - Media Audio & Video Extractor (Desktop GUI)
A sleek, modern desktop application to convert YouTube and Instagram links
to high-fidelity audio (MP3, FLAC, WAV, AAC, OPUS, ALAC) or MP4 video (up to 4K).
Supports Windows, macOS, and Linux with native icons, robust security, and fluid UI.
"""

import os
import re
import sys
import shutil
import platform
import threading
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from typing import List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox

try:
    import customtkinter as ctk
    HAS_CTK = True
except ImportError:
    HAS_CTK = False

if HAS_CTK:
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")
    BaseApp = ctk.CTk
else:
    BaseApp = tk.Tk


ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "instagram.com",
    "www.instagram.com",
    "instagr.am",
}


def find_binary(name: str) -> str:
    """Find binary location across Windows, macOS, and Linux."""
    found = shutil.which(name)
    if found:
        return found

    if platform.system() == "Windows" and not name.lower().endswith(".exe"):
        found = shutil.which(f"{name}.exe")
        if found:
            return found

    candidates = [
        # macOS / Linux paths
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/usr/bin/{name}",
        os.path.expanduser(f"~/Library/Python/3.14/bin/{name}"),
        os.path.expanduser(f"~/Library/Python/3.13/bin/{name}"),
        os.path.expanduser(f"~/Library/Python/3.12/bin/{name}"),
        os.path.expanduser(f"~/.local/bin/{name}"),
        # Windows standard locations
        rf"C:\ffmpeg\bin\{name}.exe",
        rf"C:\Program Files\ffmpeg\bin\{name}.exe",
        os.path.expandvars(rf"%LOCALAPPDATA%\Microsoft\WinGet\Links\{name}.exe"),
        os.path.expandvars(rf"%USERPROFILE%\scoop\shims\{name}.exe"),
        str(Path(__file__).parent / f"{name}.exe"),
        str(Path(__file__).parent / name),
    ]

    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return name


def sanitize_url(raw_url: str) -> str:
    """
    Validate and sanitize YouTube & Instagram URLs.
    Defends against CLI argument injection, malformed URLs, and non-streaming schemes.
    """
    url = (raw_url or "").strip()
    if not url or url.startswith("-"):
        return ""

    try:
        parsed = urlparse(url)
    except Exception:
        return ""

    if parsed.scheme not in ("http", "https"):
        return ""

    hostname = (parsed.hostname or "").lower()
    if not any(hostname == h or hostname.endswith("." + h) for h in ALLOWED_HOSTS):
        return ""

    # Normalize YouTube Music & shortlinks
    url = url.replace("music.youtube.com", "www.youtube.com")
    if "youtu.be/" in url:
        try:
            video_id = url.split("youtu.be/")[1].split("?")[0].split("&")[0]
            if re.match(r'^[a-zA-Z0-9_-]{6,15}$', video_id):
                url = f"https://www.youtube.com/watch?v={video_id}"
        except Exception:
            pass

    # Strip tracking parameters (si, igsh, utm_*, fbclid)
    url = re.sub(r'([?&])(si|igsh|utm_[a-zA-Z0-9_]+|fbclid)=[^&#]*', r'\1', url)
    url = url.replace("?&", "?").rstrip("?&")
    return url


class AudioExtractorApp(BaseApp):
    def __init__(self):
        super().__init__()

        self.title("SonicFlow — Studio Media Downloader")
        self.geometry("740x700")
        self.minsize(680, 580)

        # Apply native Windows & cross-platform icon
        self._apply_app_icon()

        # State tracking
        self.is_downloading = False
        self.current_process = None
        self.download_thread = None
        self.log_expanded = False
        self.log_lines_count = 0

        # Paths
        self.ffmpeg_path = find_binary("ffmpeg")
        self.output_dir = str(Path.home() / "Downloads")

        self.setup_ui()

    def _apply_app_icon(self):
        """Load native application icons for Windows (.ico) and macOS/Linux."""
        base_dir = Path(__file__).resolve().parent
        ico_path = base_dir / "SonicFlow.ico"
        png_path = base_dir / "icon.png"

        if platform.system() == "Windows" and ico_path.exists():
            try:
                self.iconbitmap(str(ico_path))
            except Exception:
                pass

        if png_path.exists():
            try:
                img = tk.PhotoImage(file=str(png_path))
                self.iconphoto(True, img)
                self._icon_ref = img
            except Exception:
                pass

    def setup_ui(self):
        if HAS_CTK:
            self._setup_ctk_ui()
        else:
            self._setup_tk_fallback_ui()

    def _setup_ctk_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.container = ctk.CTkScrollableFrame(self, corner_radius=0, fg_color="transparent")
        self.container.grid(row=0, column=0, sticky="nsew", padx=20, pady=16)
        self.container.grid_columnconfigure(0, weight=1)

        # -------------------------------------------------------------
        # 1. Header Card
        # -------------------------------------------------------------
        header_frame = ctk.CTkFrame(self.container, fg_color="#121622", corner_radius=14, border_width=1, border_color="#1e293b")
        header_frame.grid(row=0, column=0, sticky="ew", pady=(0, 14), padx=2)
        header_frame.grid_columnconfigure(0, weight=1)

        top_row = ctk.CTkFrame(header_frame, fg_color="transparent")
        top_row.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 4))
        top_row.grid_columnconfigure(0, weight=1)

        title_lbl = ctk.CTkLabel(
            top_row,
            text="SonicFlow",
            font=ctk.CTkFont(size=24, weight="bold"),
            text_color="#f8fafc"
        )
        title_lbl.pack(side="left")

        ver_lbl = ctk.CTkLabel(
            top_row,
            text="v1.3 Studio",
            font=ctk.CTkFont(size=11, family="Courier"),
            text_color="#64748b"
        )
        ver_lbl.pack(side="left", padx=10, pady=(4, 0))

        ffmpeg_exists = bool(self.ffmpeg_path and (os.path.exists(self.ffmpeg_path) or shutil.which(self.ffmpeg_path)))
        status_pill = ctk.CTkLabel(
            top_row,
            text=f"FFmpeg {'Ready' if ffmpeg_exists else 'Not Detected'}",
            fg_color="#064e3b" if ffmpeg_exists else "#78350f",
            text_color="#6ee7b7" if ffmpeg_exists else "#fde047",
            corner_radius=6,
            font=ctk.CTkFont(size=11, weight="bold"),
            padx=10,
            pady=3
        )
        status_pill.pack(side="right")

        subtitle_lbl = ctk.CTkLabel(
            header_frame,
            text="Studio-grade media downloader for YouTube & Instagram with high-fidelity encoding.",
            font=ctk.CTkFont(size=12),
            text_color="#94a3b8"
        )
        subtitle_lbl.grid(row=1, column=0, sticky="w", padx=20, pady=(0, 16))

        # -------------------------------------------------------------
        # 2. Mode Switcher (Audio vs Video)
        # -------------------------------------------------------------
        self.mode_var = ctk.StringVar(value="Audio Extractor")
        self.mode_selector = ctk.CTkSegmentedButton(
            self.container,
            values=["Audio Extractor", "Video Downloader (Up to 4K)"],
            variable=self.mode_var,
            command=self.on_mode_changed,
            height=36,
            selected_color="#0284c7",
            selected_hover_color="#0369a1",
            font=ctk.CTkFont(size=13, weight="bold")
        )
        self.mode_selector.grid(row=1, column=0, sticky="ew", pady=(0, 14), padx=2)

        # -------------------------------------------------------------
        # 3. Input Section Card
        # -------------------------------------------------------------
        input_frame = ctk.CTkFrame(self.container, fg_color="#121622", corner_radius=14, border_width=1, border_color="#1e293b")
        input_frame.grid(row=2, column=0, sticky="ew", pady=(0, 14), padx=2)
        input_frame.grid_columnconfigure(0, weight=1)

        input_title_row = ctk.CTkFrame(input_frame, fg_color="transparent")
        input_title_row.grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 6))
        input_title_row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            input_title_row,
            text="Media URLs (One or Multiple)",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#e2e8f0"
        ).pack(side="left")

        # Quick action buttons in title row
        paste_btn = ctk.CTkButton(
            input_title_row,
            text="Paste",
            command=self.paste_clipboard,
            width=70,
            height=26,
            fg_color="#1e293b",
            hover_color="#334155",
            font=ctk.CTkFont(size=11)
        )
        paste_btn.pack(side="right")

        clear_btn = ctk.CTkButton(
            input_title_row,
            text="Clear",
            command=self.clear_urls,
            width=60,
            height=26,
            fg_color="#1e293b",
            hover_color="#334155",
            font=ctk.CTkFont(size=11)
        )
        clear_btn.pack(side="right", padx=(0, 8))

        self.url_box = ctk.CTkTextbox(
            input_frame,
            height=70,
            corner_radius=8,
            font=ctk.CTkFont(size=12),
            fg_color="#090d16",
            border_color="#334155",
            border_width=1
        )
        self.url_box.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 14))

        # -------------------------------------------------------------
        # 4. Settings Card
        # -------------------------------------------------------------
        settings_frame = ctk.CTkFrame(self.container, fg_color="#121622", corner_radius=14, border_width=1, border_color="#1e293b")
        settings_frame.grid(row=3, column=0, sticky="ew", pady=(0, 14), padx=2)
        settings_frame.grid_columnconfigure(0, weight=1)
        settings_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            settings_frame,
            text="Export Options",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#e2e8f0"
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(14, 10))

        # Format Dropdown
        fmt_row = ctk.CTkFrame(settings_frame, fg_color="transparent")
        fmt_row.grid(row=1, column=0, sticky="ew", padx=(20, 10), pady=(0, 12))
        fmt_row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(fmt_row, text="Format:", font=ctk.CTkFont(size=12), text_color="#cbd5e1").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.format_var = ctk.StringVar(value="MP3")
        self.format_dropdown = ctk.CTkOptionMenu(
            fmt_row,
            variable=self.format_var,
            values=["MP3", "FLAC", "WAV", "AAC", "OPUS", "ALAC"],
            command=self.on_format_changed,
            fg_color="#0284c7",
            button_color="#0369a1",
            height=32
        )
        self.format_dropdown.grid(row=1, column=0, sticky="ew")

        # Quality / Resolution Dropdown
        quality_row = ctk.CTkFrame(settings_frame, fg_color="transparent")
        quality_row.grid(row=1, column=1, sticky="ew", padx=(10, 20), pady=(0, 12))
        quality_row.grid_columnconfigure(0, weight=1)

        self.quality_label = ctk.CTkLabel(quality_row, text="Bitrate Quality:", font=ctk.CTkFont(size=12), text_color="#cbd5e1")
        self.quality_label.grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.quality_var = ctk.StringVar(value="320 kbps (High Fidelity)")
        self.quality_dropdown = ctk.CTkOptionMenu(
            quality_row,
            variable=self.quality_var,
            values=["320 kbps (High Fidelity)", "256 kbps (High)", "192 kbps (Standard)", "128 kbps (Compact)"],
            fg_color="#1e293b",
            button_color="#334155",
            height=32
        )
        self.quality_dropdown.grid(row=1, column=0, sticky="ew")

        # Destination Folder row
        dest_lbl = ctk.CTkLabel(
            settings_frame,
            text="Save Location:",
            font=ctk.CTkFont(size=12),
            text_color="#cbd5e1"
        )
        dest_lbl.grid(row=2, column=0, sticky="w", padx=20, pady=(2, 4))

        dest_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        dest_frame.grid(row=3, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 14))
        dest_frame.grid_columnconfigure(0, weight=1)

        self.dest_entry = ctk.CTkEntry(
            dest_frame,
            placeholder_text="Destination folder...",
            fg_color="#090d16",
            border_color="#334155",
            border_width=1,
            height=32
        )
        self.dest_entry.insert(0, self.output_dir)
        self.dest_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        browse_btn = ctk.CTkButton(
            dest_frame,
            text="Browse",
            command=self.browse_folder,
            width=75,
            height=32,
            fg_color="#1e293b",
            hover_color="#334155"
        )
        browse_btn.grid(row=0, column=1, padx=(0, 6))

        open_dir_btn = ctk.CTkButton(
            dest_frame,
            text="Open",
            command=self.open_output_folder,
            width=65,
            height=32,
            fg_color="#1e293b",
            hover_color="#334155"
        )
        open_dir_btn.grid(row=0, column=2)

        # Extra Options (Checkboxes)
        opts_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        opts_frame.grid(row=4, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 14))

        self.embed_thumb_var = ctk.BooleanVar(value=True)
        self.thumb_chk = ctk.CTkCheckBox(
            opts_frame,
            text="Embed Artwork",
            variable=self.embed_thumb_var,
            font=ctk.CTkFont(size=12)
        )
        self.thumb_chk.pack(side="left", padx=(0, 16))

        self.embed_meta_var = ctk.BooleanVar(value=True)
        self.meta_chk = ctk.CTkCheckBox(
            opts_frame,
            text="Embed ID3 Tags",
            variable=self.embed_meta_var,
            font=ctk.CTkFont(size=12)
        )
        self.meta_chk.pack(side="left", padx=(0, 16))

        self.playlist_var = ctk.BooleanVar(value=True)
        self.playlist_chk = ctk.CTkCheckBox(
            opts_frame,
            text="Full Playlists",
            variable=self.playlist_var,
            font=ctk.CTkFont(size=12)
        )
        self.playlist_chk.pack(side="left")

        # -------------------------------------------------------------
        # 5. Action & Progress Area
        # -------------------------------------------------------------
        action_frame = ctk.CTkFrame(self.container, fg_color="transparent")
        action_frame.grid(row=4, column=0, sticky="ew", pady=(0, 12), padx=2)
        action_frame.grid_columnconfigure(0, weight=1)

        btn_row = ctk.CTkFrame(action_frame, fg_color="transparent")
        btn_row.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        btn_row.grid_columnconfigure(0, weight=3)
        btn_row.grid_columnconfigure(1, weight=1)

        self.download_btn = ctk.CTkButton(
            btn_row,
            text="Start Download & Conversion",
            command=self.start_download,
            height=44,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#0284c7",
            hover_color="#0369a1"
        )
        self.download_btn.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        self.cancel_btn = ctk.CTkButton(
            btn_row,
            text="Cancel",
            command=self.cancel_download,
            height=44,
            state="disabled",
            font=ctk.CTkFont(size=13),
            fg_color="#ef4444",
            hover_color="#dc2626"
        )
        self.cancel_btn.grid(row=0, column=1, sticky="ew")

        self.progress_bar = ctk.CTkProgressBar(action_frame, height=8, corner_radius=4)
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self.progress_bar.set(0)

        status_row = ctk.CTkFrame(action_frame, fg_color="transparent")
        status_row.grid(row=2, column=0, sticky="ew")
        status_row.grid_columnconfigure(0, weight=1)

        self.status_lbl = ctk.CTkLabel(
            status_row,
            text="Ready",
            font=ctk.CTkFont(size=12),
            text_color="#94a3b8",
            anchor="w"
        )
        self.status_lbl.pack(side="left")

        # Collapsible Log Toggle
        self.toggle_log_btn = ctk.CTkButton(
            status_row,
            text="View Log",
            command=self.toggle_log,
            width=80,
            height=24,
            fg_color="transparent",
            hover_color="#1e293b",
            text_color="#64748b",
            font=ctk.CTkFont(size=11)
        )
        self.toggle_log_btn.pack(side="right")

        # -------------------------------------------------------------
        # 6. Collapsible Log Drawer (Hidden by default for clean UX)
        # -------------------------------------------------------------
        self.log_frame = ctk.CTkFrame(self.container, fg_color="#121622", corner_radius=12, border_width=1, border_color="#1e293b")
        self.log_frame.grid_columnconfigure(0, weight=1)

        self.log_box = ctk.CTkTextbox(
            self.log_frame,
            height=130,
            corner_radius=8,
            font=ctk.CTkFont(family="Courier", size=10),
            fg_color="#090d16",
            text_color="#cbd5e1"
        )
        self.log_box.grid(row=0, column=0, sticky="nsew", padx=14, pady=14)

    def _setup_tk_fallback_ui(self):
        self.configure(bg="#121622")
        lbl = tk.Label(self, text="SonicFlow", font=("Helvetica", 18, "bold"), fg="#38bdf8", bg="#121622")
        lbl.pack(pady=10)
        self.url_box = tk.Text(self, height=4, width=60, bg="#090d16", fg="#ffffff")
        self.url_box.pack(pady=10)

    def on_mode_changed(self, mode):
        if "Video" in mode:
            self.format_dropdown.configure(values=["MP4"], state="disabled")
            self.format_var.set("MP4")
            self.quality_label.configure(text="Video Resolution:")
            self.quality_dropdown.configure(
                values=["4K (2160p)", "1440p (2K)", "1080p (Full HD)", "720p (HD)", "480p (SD)", "Best Available"],
                state="normal"
            )
            self.quality_var.set("1080p (Full HD)")
        else:
            self.format_dropdown.configure(values=["MP3", "FLAC", "WAV", "AAC", "OPUS", "ALAC"], state="normal")
            self.format_var.set("MP3")
            self.on_format_changed("MP3")

    def on_format_changed(self, choice):
        fmt = choice.upper()
        if fmt in ["FLAC", "WAV", "ALAC"]:
            self.quality_label.configure(text="Bitrate Quality:")
            self.quality_dropdown.configure(values=["Lossless (Original Fidelity)"], state="disabled")
            self.quality_var.set("Lossless (Original Fidelity)")
        elif fmt == "MP3":
            self.quality_label.configure(text="Bitrate Quality:")
            self.quality_dropdown.configure(
                values=["320 kbps (High Fidelity)", "256 kbps (High)", "192 kbps (Standard)", "128 kbps (Compact)"],
                state="normal"
            )
            self.quality_var.set("320 kbps (High Fidelity)")
        elif fmt == "AAC":
            self.quality_label.configure(text="Bitrate Quality:")
            self.quality_dropdown.configure(values=["320 kbps", "256 kbps", "192 kbps"], state="normal")
            self.quality_var.set("256 kbps")
        elif fmt == "OPUS":
            self.quality_label.configure(text="Bitrate Quality:")
            self.quality_dropdown.configure(values=["160 kbps (Transparent)", "128 kbps", "96 kbps"], state="normal")
            self.quality_var.set("160 kbps (Transparent)")

    def toggle_log(self):
        if self.log_expanded:
            self.log_frame.grid_forget()
            self.toggle_log_btn.configure(text=f"View Log ({self.log_lines_count})")
            self.log_expanded = False
        else:
            self.log_frame.grid(row=5, column=0, sticky="nsew", padx=2, pady=(0, 10))
            self.toggle_log_btn.configure(text="Hide Log")
            self.log_expanded = True

    def paste_clipboard(self):
        try:
            content = self.clipboard_get().strip()
            if content:
                self.url_box.insert("end", content + "\n")
        except Exception:
            pass

    def clear_urls(self):
        self.url_box.delete("1.0", "end")

    def browse_folder(self):
        chosen = filedialog.askdirectory(initialdir=self.output_dir, title="Select Output Folder")
        if chosen:
            self.output_dir = chosen
            self.dest_entry.delete(0, "end")
            self.dest_entry.insert(0, chosen)

    def open_output_folder(self):
        target = self.dest_entry.get().strip() or self.output_dir
        target = os.path.expanduser(target)
        if not os.path.exists(target):
            try:
                os.makedirs(target, exist_ok=True)
            except Exception:
                pass

        try:
            if platform.system() == "Darwin":
                subprocess.run(["open", target], check=False)
            elif platform.system() == "Windows":
                os.startfile(os.path.normpath(target))
            else:
                subprocess.run(["xdg-open", target], check=False)
        except Exception as e:
            messagebox.showerror("Error", f"Could not open directory: {e}")

    def append_log(self, text: str):
        self.log_lines_count += 1
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        if not self.log_expanded:
            self.toggle_log_btn.configure(text=f"View Log ({self.log_lines_count})")

    def update_status(self, text: str, progress: Optional[float] = None):
        self.status_lbl.configure(text=text)
        if progress is not None:
            self.progress_bar.set(progress)

    def start_download(self):
        raw_text = self.url_box.get("1.0", "end").strip()
        if not raw_text:
            messagebox.showwarning("Missing URL", "Please enter at least one YouTube or Instagram link.")
            return

        urls = [sanitize_url(line) for line in raw_text.splitlines() if line.strip()]
        urls = [u for u in urls if u]

        if not urls:
            messagebox.showwarning(
                "Invalid Links",
                "No valid YouTube or Instagram links were recognized.\nMake sure the link starts with http:// or https://."
            )
            return

        destination = self.dest_entry.get().strip() or str(Path.home() / "Downloads")
        destination = os.path.expanduser(destination)
        try:
            os.makedirs(destination, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Invalid Destination", f"Cannot create output directory:\n{e}")
            return

        self.is_downloading = True
        self.download_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress_bar.set(0)
        self.update_status(f"Starting {len(urls)} download(s)...", 0.05)

        self.download_thread = threading.Thread(
            target=self._run_downloader_thread,
            args=(urls, destination),
            daemon=True
        )
        self.download_thread.start()

    def cancel_download(self):
        if self.is_downloading and self.current_process:
            self.append_log("\n⚠ Cancelling operation upon user request...")
            try:
                self.current_process.terminate()
            except Exception:
                pass
            self.update_status("Download cancelled.", 0)
            self.is_downloading = False
            self.download_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")

    def _parse_quality_arg(self, fmt: str, raw_quality: str) -> str:
        fmt = fmt.lower()
        if "mp4" in fmt:
            for res in ["2160", "1440", "1080", "720", "480"]:
                if res in raw_quality:
                    return res
            return "best"

        if fmt in ["flac", "wav", "alac"]:
            return "0"
        for bit in ["320", "256", "192", "160", "128", "96"]:
            if bit in raw_quality:
                return f"{bit}k"
        return "320k"

    def _run_downloader_thread(self, urls: List[str], destination: str):
        fmt_raw = self.format_var.get().lower()
        is_video = (fmt_raw == "mp4")
        quality_choice = self._parse_quality_arg(fmt_raw, self.quality_var.get())
        embed_thumbnail = self.embed_thumb_var.get()
        embed_metadata = self.embed_meta_var.get()
        allow_playlist = self.playlist_var.get()

        total = len(urls)

        for idx, url in enumerate(urls, 1):
            if not self.is_downloading:
                break

            self.after(0, self.append_log, f"\n{'─'*45}\n[{idx}/{total}] Processing: {url}\n{'─'*45}")
            self.after(0, self.update_status, f"[{idx}/{total}] Connecting...", (idx - 1) / total)

            cmd = [
                sys.executable, "-m", "yt_dlp",
                "-4",
                "--no-check-certificates",
                "--geo-bypass",
                "--remote-components", "ejs:github",
                "--extractor-args", "youtube:player_client=ios,android,mweb",
            ]

            is_instagram = ("instagram.com" in url.lower() or "instagr.am" in url.lower())
            if is_instagram:
                cmd.extend([
                    "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                    "--add-header", "Accept-Language: en-US,en;q=0.9",
                ])

            cookie_candidates = [
                Path("finalcookies.txt"),
                Path("cookies.txt"),
                Path("www.youtube.com_cookies.txt"),
            ]
            for f in Path(".").glob("*cookie*.txt"):
                if f not in cookie_candidates:
                    cookie_candidates.append(f)

            for cand in cookie_candidates:
                if cand.exists() and cand.is_file() and cand.stat().st_size > 10:
                    cmd.extend(["--cookies", str(cand.resolve())])
                    break

            if is_video:
                if quality_choice in ["2160", "1440", "1080", "720", "480"]:
                    fmt_str = f"bv*[height<={quality_choice}]+ba/b[height<={quality_choice}]/bv*+ba/b"
                else:
                    fmt_str = "bv*+ba/b"

                cmd.extend([
                    "-f", fmt_str,
                    "--merge-output-format", "mp4",
                    "-o", os.path.join(destination, "%(title)s.%(ext)s")
                ])
            else:
                cmd.extend([
                    "-f", "ba/b",
                    "--extract-audio",
                    "--audio-format", fmt_raw,
                    "--audio-quality", quality_choice,
                    "-o", os.path.join(destination, "%(title)s.%(ext)s")
                ])

            if embed_metadata:
                cmd.append("--add-metadata")
            if embed_thumbnail:
                cmd.append("--embed-thumbnail")

            if allow_playlist:
                cmd.append("--yes-playlist")
            else:
                cmd.append("--no-playlist")

            ffmpeg_dir = os.path.dirname(self.ffmpeg_path) if self.ffmpeg_path else ""
            if ffmpeg_dir and os.path.exists(ffmpeg_dir):
                cmd.extend(["--ffmpeg-location", ffmpeg_dir])

            # Security: Separate options with '--' to prevent CLI flag injection
            cmd.extend(["--", url])

            try:
                self.current_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True
                )

                for line in self.current_process.stdout:
                    clean_line = line.strip()
                    if not clean_line:
                        continue

                    self.after(0, self.append_log, clean_line)

                    if "[download]" in clean_line and "%" in clean_line:
                        match = re.search(r'(\d+(?:\.\d+)?)%', clean_line)
                        if match:
                            pct = float(match.group(1)) / 100.0
                            overall_pct = ((idx - 1) + pct) / total
                            self.after(0, self.update_status, f"[{idx}/{total}] Downloading: {clean_line[:50]}", overall_pct)
                    elif "[ExtractAudio]" in clean_line or "[ffmpeg]" in clean_line or "[Merger]" in clean_line:
                        target_name = "MP4 Video" if is_video else fmt_raw.upper()
                        self.after(0, self.update_status, f"[{idx}/{total}] Encoding {target_name}...")

                self.current_process.wait()
                rc = self.current_process.returncode

                if rc == 0:
                    self.after(0, self.append_log, f"✓ Finished item {idx}/{total}")
                else:
                    self.after(0, self.append_log, f"⚠ Error processing URL (Exit code {rc}).")
                    if not self.log_expanded:
                        self.after(0, self.toggle_log)

            except Exception as e:
                self.after(0, self.append_log, f"❌ Download error: {str(e)}")
                if not self.log_expanded:
                    self.after(0, self.toggle_log)

        self.is_downloading = False
        self.current_process = None

        def finalize():
            self.download_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self.progress_bar.set(1.0)
            self.update_status("Completed! All items processed.", 1.0)
            messagebox.showinfo("Download Complete", f"All conversions finished!\nFiles saved in:\n{destination}")

        self.after(0, finalize)


def main():
    app = AudioExtractorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
