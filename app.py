#!/usr/bin/env python3
"""
SonicFlow - Media Audio & Video Extractor
A modern desktop application to convert YouTube and Instagram links to high-quality audio
(MP3, FLAC, WAV, AAC, OPUS, ALAC) or MP4 video (480p up to 4K Ultra HD).
"""

import os
import re
import sys
import shutil
import platform
import threading
import subprocess
from pathlib import Path
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


def find_binary(name):
    """Find binary location, prioritizing standard paths and Homebrew."""
    found = shutil.which(name)
    if found:
        return found
    candidates = [
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        os.path.expanduser(f"~/Library/Python/3.14/bin/{name}"),
        os.path.expanduser(f"~/Library/Python/3.13/bin/{name}"),
        os.path.expanduser(f"~/Library/Python/3.12/bin/{name}"),
        os.path.expanduser(f"~/.local/bin/{name}"),
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return name


def sanitize_url(raw_url: str) -> str:
    """Clean tracking params and normalize YouTube / Instagram URLs."""
    url = raw_url.strip()
    if not url:
        return ""

    url = url.replace("music.youtube.com", "www.youtube.com")

    if "youtu.be/" in url:
        video_id = url.split("youtu.be/")[1].split("?")[0].split("&")[0]
        url = f"https://www.youtube.com/watch?v={video_id}"

    url = re.sub(r'([?&])(si|igsh|utm_[a-zA-Z0-9_]+|fbclid)=[^&#]*', r'\1', url)
    url = url.replace("?&", "?").rstrip("?&")

    return url


class AudioExtractorApp(BaseApp):
    def __init__(self):
        super().__init__()

        self.title("SonicFlow — Media Audio & Video Downloader")
        self.geometry("780x760")
        self.minsize(700, 680)

        # Download state
        self.is_downloading = False
        self.current_process = None
        self.download_thread = None

        # Default paths
        self.ffmpeg_path = find_binary("ffmpeg")
        self.output_dir = str(Path.home() / "Downloads")

        self.setup_ui()

    def setup_ui(self):
        if HAS_CTK:
            self._setup_ctk_ui()
        else:
            self._setup_tk_fallback_ui()

    def _setup_ctk_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.container = ctk.CTkScrollableFrame(self, corner_radius=0, fg_color="transparent")
        self.container.grid(row=0, column=0, sticky="nsew", padx=20, pady=20)
        self.container.grid_columnconfigure(0, weight=1)

        # 1. Header Frame
        header_frame = ctk.CTkFrame(self.container, fg_color="#181a20", corner_radius=14)
        header_frame.grid(row=0, column=0, sticky="ew", pady=(0, 16), padx=4)
        header_frame.grid_columnconfigure(0, weight=1)

        title_lbl = ctk.CTkLabel(
            header_frame,
            text="🎵 SonicFlow",
            font=ctk.CTkFont(size=26, weight="bold"),
            text_color="#38bdf8"
        )
        title_lbl.grid(row=0, column=0, sticky="w", padx=20, pady=(16, 4))

        subtitle_lbl = ctk.CTkLabel(
            header_frame,
            text="Extract audio (MP3, FLAC, WAV, AAC) or MP4 video (480p to 4K) from YouTube & Instagram.",
            font=ctk.CTkFont(size=13),
            text_color="#94a3b8"
        )
        subtitle_lbl.grid(row=1, column=0, sticky="w", padx=20, pady=(0, 14))

        # Badges row
        badge_frame = ctk.CTkFrame(header_frame, fg_color="transparent")
        badge_frame.grid(row=2, column=0, sticky="w", padx=20, pady=(0, 16))

        yt_badge = ctk.CTkLabel(
            badge_frame,
            text="▶ YouTube",
            fg_color="#ef4444",
            text_color="#ffffff",
            corner_radius=6,
            font=ctk.CTkFont(size=11, weight="bold"),
            padx=10,
            pady=3
        )
        yt_badge.pack(side="left", padx=(0, 8))

        ig_badge = ctk.CTkLabel(
            badge_frame,
            text="📷 Instagram",
            fg_color="#d946ef",
            text_color="#ffffff",
            corner_radius=6,
            font=ctk.CTkFont(size=11, weight="bold"),
            padx=10,
            pady=3
        )
        ig_badge.pack(side="left", padx=(0, 8))

        vid_badge = ctk.CTkLabel(
            badge_frame,
            text="🎬 4K MP4 Video",
            fg_color="#6366f1",
            text_color="#ffffff",
            corner_radius=6,
            font=ctk.CTkFont(size=11, weight="bold"),
            padx=10,
            pady=3
        )
        vid_badge.pack(side="left", padx=(0, 8))

        status_pill = ctk.CTkLabel(
            badge_frame,
            text=f"FFmpeg: {'✓ Ready' if os.path.exists(self.ffmpeg_path) else '⚠ Not Found'}",
            fg_color="#1e293b",
            text_color="#10b981" if os.path.exists(self.ffmpeg_path) else "#f59e0b",
            corner_radius=6,
            font=ctk.CTkFont(size=11),
            padx=10,
            pady=3
        )
        status_pill.pack(side="left")

        # 2. Input Section Frame
        input_frame = ctk.CTkFrame(self.container, fg_color="#181a20", corner_radius=14)
        input_frame.grid(row=1, column=0, sticky="ew", pady=(0, 16), padx=4)
        input_frame.grid_columnconfigure(0, weight=1)

        input_title = ctk.CTkLabel(
            input_frame,
            text="Media URLs (Single or Multiple)",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#e2e8f0"
        )
        input_title.grid(row=0, column=0, sticky="w", padx=20, pady=(16, 8))

        self.url_box = ctk.CTkTextbox(
            input_frame,
            height=85,
            corner_radius=8,
            font=ctk.CTkFont(size=13),
            fg_color="#0f172a",
            border_color="#334155",
            border_width=1
        )
        self.url_box.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 10))

        url_btn_frame = ctk.CTkFrame(input_frame, fg_color="transparent")
        url_btn_frame.grid(row=2, column=0, sticky="w", padx=20, pady=(0, 16))

        paste_btn = ctk.CTkButton(
            url_btn_frame,
            text="📋 Paste from Clipboard",
            command=self.paste_clipboard,
            width=160,
            height=30,
            fg_color="#334155",
            hover_color="#475569",
            font=ctk.CTkFont(size=12)
        )
        paste_btn.pack(side="left", padx=(0, 10))

        clear_btn = ctk.CTkButton(
            url_btn_frame,
            text="✕ Clear",
            command=self.clear_urls,
            width=80,
            height=30,
            fg_color="#334155",
            hover_color="#475569",
            font=ctk.CTkFont(size=12)
        )
        clear_btn.pack(side="left")

        # 3. Settings Card Frame
        settings_frame = ctk.CTkFrame(self.container, fg_color="#181a20", corner_radius=14)
        settings_frame.grid(row=2, column=0, sticky="ew", pady=(0, 16), padx=4)
        settings_frame.grid_columnconfigure(0, weight=1)
        settings_frame.grid_columnconfigure(1, weight=1)

        fmt_lbl = ctk.CTkLabel(
            settings_frame,
            text="Format & Quality Settings",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#e2e8f0"
        )
        fmt_lbl.grid(row=0, column=0, columnspan=2, sticky="w", padx=20, pady=(16, 12))

        # Format Dropdown
        fmt_row = ctk.CTkFrame(settings_frame, fg_color="transparent")
        fmt_row.grid(row=1, column=0, sticky="ew", padx=(20, 10), pady=(0, 12))
        fmt_row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(fmt_row, text="Format:", font=ctk.CTkFont(size=13), text_color="#cbd5e1").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.format_var = ctk.StringVar(value="MP3")
        self.format_dropdown = ctk.CTkOptionMenu(
            fmt_row,
            variable=self.format_var,
            values=["MP3", "FLAC", "WAV", "AAC", "OPUS", "ALAC", "MP4 (Video)"],
            command=self.on_format_changed,
            fg_color="#2563eb",
            button_color="#1d4ed8",
            height=32
        )
        self.format_dropdown.grid(row=1, column=0, sticky="ew")

        # Quality / Resolution Dropdown
        quality_row = ctk.CTkFrame(settings_frame, fg_color="transparent")
        quality_row.grid(row=1, column=1, sticky="ew", padx=(10, 20), pady=(0, 12))
        quality_row.grid_columnconfigure(0, weight=1)

        self.quality_label = ctk.CTkLabel(quality_row, text="Audio Quality / Bitrate:", font=ctk.CTkFont(size=13), text_color="#cbd5e1")
        self.quality_label.grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.quality_var = ctk.StringVar(value="320 kbps (Maximum)")
        self.quality_dropdown = ctk.CTkOptionMenu(
            quality_row,
            variable=self.quality_var,
            values=["320 kbps (Maximum)", "256 kbps (High)", "192 kbps (Standard)", "128 kbps (Compact)", "Best VBR (0)"],
            fg_color="#334155",
            button_color="#475569",
            height=32
        )
        self.quality_dropdown.grid(row=1, column=0, sticky="ew")

        # Destination Folder row
        dest_lbl = ctk.CTkLabel(
            settings_frame,
            text="Save Location:",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#cbd5e1"
        )
        dest_lbl.grid(row=2, column=0, sticky="w", padx=20, pady=(4, 4))

        dest_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        dest_frame.grid(row=3, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 14))
        dest_frame.grid_columnconfigure(0, weight=1)

        self.dest_entry = ctk.CTkEntry(
            dest_frame,
            placeholder_text="Destination folder...",
            fg_color="#0f172a",
            border_color="#334155",
            border_width=1,
            height=32
        )
        self.dest_entry.insert(0, self.output_dir)
        self.dest_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        browse_btn = ctk.CTkButton(
            dest_frame,
            text="Browse...",
            command=self.browse_folder,
            width=90,
            height=32,
            fg_color="#334155",
            hover_color="#475569"
        )
        browse_btn.grid(row=0, column=1, padx=(0, 8))

        open_dir_btn = ctk.CTkButton(
            dest_frame,
            text="Open Folder",
            command=self.open_output_folder,
            width=100,
            height=32,
            fg_color="#334155",
            hover_color="#475569"
        )
        open_dir_btn.grid(row=0, column=2)

        # Checkboxes for extra options
        opts_frame = ctk.CTkFrame(settings_frame, fg_color="transparent")
        opts_frame.grid(row=4, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 16))

        self.embed_thumb_var = ctk.BooleanVar(value=True)
        self.thumb_chk = ctk.CTkCheckBox(
            opts_frame,
            text="Embed Thumbnail",
            variable=self.embed_thumb_var,
            font=ctk.CTkFont(size=12)
        )
        self.thumb_chk.pack(side="left", padx=(0, 16))

        self.embed_meta_var = ctk.BooleanVar(value=True)
        self.meta_chk = ctk.CTkCheckBox(
            opts_frame,
            text="Embed Metadata",
            variable=self.embed_meta_var,
            font=ctk.CTkFont(size=12)
        )
        self.meta_chk.pack(side="left", padx=(0, 16))

        self.playlist_var = ctk.BooleanVar(value=True)
        self.playlist_chk = ctk.CTkCheckBox(
            opts_frame,
            text="Download Full Playlist",
            variable=self.playlist_var,
            font=ctk.CTkFont(size=12)
        )
        self.playlist_chk.pack(side="left")

        # 4. Action & Progress Section
        action_frame = ctk.CTkFrame(self.container, fg_color="transparent")
        action_frame.grid(row=3, column=0, sticky="ew", pady=(0, 14), padx=4)
        action_frame.grid_columnconfigure(0, weight=1)

        btn_row = ctk.CTkFrame(action_frame, fg_color="transparent")
        btn_row.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        btn_row.grid_columnconfigure(0, weight=3)
        btn_row.grid_columnconfigure(1, weight=1)

        self.download_btn = ctk.CTkButton(
            btn_row,
            text="⚡ Start Conversion & Download",
            command=self.start_download,
            height=44,
            font=ctk.CTkFont(size=15, weight="bold"),
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
            font=ctk.CTkFont(size=14),
            fg_color="#ef4444",
            hover_color="#dc2626"
        )
        self.cancel_btn.grid(row=0, column=1, sticky="ew")

        self.progress_bar = ctk.CTkProgressBar(action_frame, height=10, corner_radius=5)
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.progress_bar.set(0)

        self.status_lbl = ctk.CTkLabel(
            action_frame,
            text="Status: Ready",
            font=ctk.CTkFont(size=13),
            text_color="#94a3b8",
            anchor="w"
        )
        self.status_lbl.grid(row=2, column=0, sticky="w")

        # 5. Log Console Section
        log_frame = ctk.CTkFrame(self.container, fg_color="#181a20", corner_radius=14)
        log_frame.grid(row=4, column=0, sticky="nsew", padx=4, pady=(0, 10))
        log_frame.grid_columnconfigure(0, weight=1)

        log_header = ctk.CTkFrame(log_frame, fg_color="transparent")
        log_header.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 6))
        log_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            log_header,
            text="Activity Log",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#e2e8f0"
        ).grid(row=0, column=0, sticky="w")

        clear_log_btn = ctk.CTkButton(
            log_header,
            text="Clear Log",
            command=self.clear_log,
            width=70,
            height=24,
            fg_color="#334155",
            hover_color="#475569",
            font=ctk.CTkFont(size=11)
        )
        clear_log_btn.grid(row=0, column=1, sticky="e")

        self.log_box = ctk.CTkTextbox(
            log_frame,
            height=140,
            corner_radius=8,
            font=ctk.CTkFont(family="Courier", size=11),
            fg_color="#090d16",
            text_color="#cbd5e1"
        )
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))

    def _setup_tk_fallback_ui(self):
        self.configure(bg="#181a20")
        lbl = tk.Label(self, text="SonicFlow", font=("Helvetica", 18, "bold"), fg="#38bdf8", bg="#181a20")
        lbl.pack(pady=10)
        self.url_box = tk.Text(self, height=4, width=60, bg="#0f172a", fg="#ffffff")
        self.url_box.pack(pady=10)

    def on_format_changed(self, choice):
        fmt = choice.upper()
        if "MP4" in fmt:
            self.quality_label.configure(text="Video Resolution:")
            self.quality_dropdown.configure(
                values=["4K (2160p)", "2K (1440p)", "Full HD (1080p)", "HD (720p)", "SD (480p)", "Best Available"],
                state="normal"
            )
            self.quality_var.set("Full HD (1080p)")
        elif fmt in ["FLAC", "WAV", "ALAC"]:
            self.quality_label.configure(text="Audio Quality:")
            self.quality_dropdown.configure(
                values=["Lossless (Original Fidelity)"],
                state="disabled"
            )
            self.quality_var.set("Lossless (Original Fidelity)")
        elif fmt == "MP3":
            self.quality_label.configure(text="Audio Quality / Bitrate:")
            self.quality_dropdown.configure(
                values=["320 kbps (Maximum)", "256 kbps (High)", "192 kbps (Standard)", "128 kbps (Compact)", "Best VBR (0)"],
                state="normal"
            )
            self.quality_var.set("320 kbps (Maximum)")
        elif fmt == "AAC":
            self.quality_label.configure(text="Audio Quality / Bitrate:")
            self.quality_dropdown.configure(
                values=["320 kbps (High)", "256 kbps", "192 kbps", "128 kbps"],
                state="normal"
            )
            self.quality_var.set("256 kbps")
        elif fmt == "OPUS":
            self.quality_label.configure(text="Audio Quality / Bitrate:")
            self.quality_dropdown.configure(
                values=["160 kbps (Transparent)", "128 kbps (Standard)", "96 kbps (Compact)"],
                state="normal"
            )
            self.quality_var.set("160 kbps (Transparent)")

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
        if not os.path.exists(target):
            try:
                os.makedirs(target, exist_ok=True)
            except Exception:
                pass
        if platform.system() == "Darwin":
            subprocess.run(["open", target])
        elif platform.system() == "Windows":
            os.startfile(target)
        else:
            subprocess.run(["xdg-open", target])

    def clear_log(self):
        self.log_box.delete("1.0", "end")

    def append_log(self, text):
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")

    def update_status(self, text, progress=None):
        self.status_lbl.configure(text=f"Status: {text}")
        if progress is not None:
            self.progress_bar.set(progress)

    def start_download(self):
        raw_text = self.url_box.get("1.0", "end").strip()
        if not raw_text:
            messagebox.showwarning("Missing URL", "Please enter at least one YouTube or Instagram URL.")
            return

        urls = [sanitize_url(line) for line in raw_text.splitlines() if line.strip()]
        urls = [u for u in urls if u]

        if not urls:
            messagebox.showwarning("Invalid Input", "No valid URLs found in the input box.")
            return

        destination = self.dest_entry.get().strip()
        if not destination:
            destination = str(Path.home() / "Downloads")
        os.makedirs(destination, exist_ok=True)

        self.is_downloading = True
        self.download_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress_bar.set(0)
        self.update_status(f"Starting processing of {len(urls)} link(s)...", 0.05)

        self.download_thread = threading.Thread(
            target=self._run_downloader_thread,
            args=(urls, destination),
            daemon=True
        )
        self.download_thread.start()

    def cancel_download(self):
        if self.is_downloading and self.current_process:
            self.append_log("\n⚠ Cancelling download upon user request...")
            try:
                self.current_process.terminate()
            except Exception:
                pass
            self.update_status("Download cancelled.", 0)
            self.is_downloading = False
            self.download_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")

    def _parse_quality_arg(self, fmt, raw_quality):
        fmt = fmt.lower()
        if "mp4" in fmt:
            if "2160" in raw_quality or "4k" in raw_quality.lower():
                return "2160"
            elif "1440" in raw_quality or "2k" in raw_quality.lower():
                return "1440"
            elif "1080" in raw_quality:
                return "1080"
            elif "720" in raw_quality:
                return "720"
            elif "480" in raw_quality:
                return "480"
            return "best"

        if fmt in ["flac", "wav", "alac"]:
            return "0"
        if "320" in raw_quality:
            return "320k"
        elif "256" in raw_quality:
            return "256k"
        elif "192" in raw_quality:
            return "192k"
        elif "160" in raw_quality:
            return "160k"
        elif "128" in raw_quality:
            return "128k"
        elif "96" in raw_quality:
            return "96k"
        return "0"

    def _run_downloader_thread(self, urls, destination):
        fmt_raw = self.format_var.get().lower()
        is_video = "mp4" in fmt_raw
        quality_choice = self._parse_quality_arg(fmt_raw, self.quality_var.get())
        embed_thumbnail = self.embed_thumb_var.get()
        embed_metadata = self.embed_meta_var.get()
        allow_playlist = self.playlist_var.get()

        total = len(urls)

        for idx, url in enumerate(urls, 1):
            if not self.is_downloading:
                break

            self.after(0, self.append_log, f"\n{'='*50}\n[{idx}/{total}] Processing: {url}\n{'='*50}")
            self.after(0, self.update_status, f"Processing {idx}/{total}: {url[:40]}...", (idx - 1) / total)

            cmd = [
                sys.executable, "-m", "yt_dlp",
                "--no-check-certificates",
                "--geo-bypass",
                "--extractor-args", "youtube:player_client=ios,android,mweb",
            ]

            is_instagram = ("instagram.com" in url.lower() or "instagr.am" in url.lower())
            if is_instagram:
                cmd.extend([
                    "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                    "--add-header", "Accept-Language: en-US,en;q=0.9",
                ])

            for cand in [Path("cookies.txt"), Path("www.youtube.com_cookies.txt")]:
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
                fmt_choice = fmt_raw
                cmd.extend([
                    "-f", "ba/b",
                    "--extract-audio",
                    "--audio-format", fmt_choice,
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

            ffmpeg_dir = os.path.dirname(self.ffmpeg_path) if self.ffmpeg_path else "/opt/homebrew/bin"
            if os.path.exists(ffmpeg_dir):
                cmd.extend(["--ffmpeg-location", ffmpeg_dir])

            cmd.append(url)

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
                            self.after(0, self.update_status, f"[{idx}/{total}] Downloading: {clean_line[:60]}", overall_pct)
                    elif "[ExtractAudio]" in clean_line or "[ffmpeg]" in clean_line or "[Merger]" in clean_line:
                        target_name = "MP4 Video" if is_video else fmt_raw.upper()
                        self.after(0, self.update_status, f"[{idx}/{total}] Finalizing {target_name}...")

                self.current_process.wait()
                rc = self.current_process.returncode

                if rc == 0:
                    self.after(0, self.append_log, f"✓ Successfully converted item {idx}/{total}!")
                else:
                    self.after(0, self.append_log, f"⚠ Error processing URL (code {rc}). Check log above.")

            except Exception as e:
                self.after(0, self.append_log, f"❌ Error during download: {str(e)}")

        self.is_downloading = False
        self.current_process = None

        def finalize():
            self.download_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")
            self.progress_bar.set(1.0)
            self.update_status("Done! All items processed.", 1.0)
            messagebox.showinfo("Success", f"All conversions finished!\nFiles saved to:\n{destination}")

        self.after(0, finalize)


def main():
    app = AudioExtractorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
