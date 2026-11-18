"""FFmpeg / FFprobe probing, folder selection, winget installation, and guidance UI."""

from dataclasses import replace
import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from collections.abc import Callable

from audiobookdl import logging
from audiobookdl.options_schema import DownloadOptions
from audiobookdl.gui.controller import GuiController
from audiobookdl.utils import (
    find_executable,
    normalize_audio_bitrate,
    normalize_sample_rate,
    validate_executable,
    validate_ffmpeg_dir,
)
from audiobookdl.utils.updater import winget_available


class FFmpegAssistant:
    """Manage header FFmpeg status widgets, binary validation, and setup guidance."""

    def __init__(
        self,
        root: tk.Tk,
        controller: GuiController,
        *,
        status_var: tk.StringVar,
        ffmpeg_status_var: tk.StringVar,
        on_log: Callable[[str], None],
        on_status_changed: Callable[[bool, str], None] | None = None,
    ) -> None:
        self._root = root
        self._controller = controller
        self._status_var = status_var
        self.status_var = ffmpeg_status_var
        self._on_log = on_log
        self._on_status_changed = on_status_changed
        self._status_label: ttk.Label | None = None
        self.locate_btn: ttk.Button | None = None
        self.is_available = True

    def build_controls(self, parent: tk.Misc) -> ttk.Frame:
        """Build the header status label and Locate FFmpeg button inside `parent`."""
        box = ttk.Frame(parent)
        self._status_label = ttk.Label(
            box,
            textvariable=self.status_var,
            takefocus=True,
        )
        self._status_label.pack(side=tk.LEFT, padx=(0, 8))
        self._status_label.bind("<Button-1>", lambda _e: self.show_guidance())
        self._status_label.bind("<Return>", lambda _e: self.show_guidance())
        self._status_label.bind("<space>", lambda _e: self.show_guidance())
        self._status_label.bind(
            "<FocusIn>",
            lambda _e: self._status_label.configure(relief="solid", borderwidth=1) if self._status_label else None,
        )
        self._status_label.bind(
            "<FocusOut>",
            lambda _e: self._status_label.configure(relief="flat", borderwidth=0) if self._status_label else None,
        )

        self.locate_btn = ttk.Button(
            box,
            text="Locate FFmpeg...",
            command=self.browse_dir,
        )
        self.locate_btn.pack(side=tk.LEFT)
        return box

    def check_tool(self, program: str) -> tuple[str | None, str | None]:
        """Return `(resolved_path, validation_error)` for `ffmpeg` or `ffprobe`."""
        configured_dir = self._controller.config_snapshot().ffmpeg_dir
        current_ctx = logging.get_job_context()
        ctx = (
            replace(current_ctx, ffmpeg_dir=configured_dir)
            if current_ctx is not None
            else logging.JobContext(ffmpeg_dir=configured_dir)
        )
        with logging.job_context(ctx):
            path = find_executable(program)
        if path is None:
            return None, None
        if os.path.isfile(path):
            return path, validate_executable(path)
        return path, None

    def update_status(self) -> None:
        """Probe FFmpeg/FFprobe availability and refresh status label, button, and banner callback."""
        ffmpeg_path, ffmpeg_err = self.check_tool("ffmpeg")
        ffprobe_path, ffprobe_err = self.check_tool("ffprobe")
        if ffmpeg_err or ffprobe_err:
            self.status_var.set("FFmpeg: Unusable (click for help)")
            style_name = "Error.TLabel"
            cursor = "hand2"
            btn_text = "Locate FFmpeg..."
            available = False
        elif ffmpeg_path and ffprobe_path:
            self.status_var.set("FFmpeg: Ready")
            style_name = "Success.TLabel"
            cursor = ""
            btn_text = "FFmpeg Folder..."
            available = True
        elif ffmpeg_path:
            self.status_var.set("FFmpeg: Ready (ffprobe missing - click for help)")
            style_name = "Warning.TLabel"
            cursor = "hand2"
            btn_text = "Locate FFmpeg..."
            available = False
        else:
            self.status_var.set("FFmpeg: Not found (click for setup help)")
            style_name = "Error.TLabel"
            cursor = "hand2"
            btn_text = "Locate FFmpeg..."
            available = False

        self.is_available = available
        if self._status_label is not None:
            try:
                if self._status_label.winfo_exists():
                    self._status_label.configure(style=style_name, cursor=cursor)
            except tk.TclError:
                pass
        if self.locate_btn is not None:
            try:
                if self.locate_btn.winfo_exists():
                    self.locate_btn.configure(text=btn_text)
            except tk.TclError:
                pass
        if self._on_status_changed is not None:
            self._on_status_changed(available, self.status_var.get())

    def browse_dir(self) -> bool:
        """Let the user choose a folder containing `ffmpeg` and persist it in config."""
        chosen = filedialog.askdirectory(
            parent=self._root,
            title="Select folder containing ffmpeg / ffprobe",
            initialdir=self._controller.config.ffmpeg_dir or os.getcwd(),
        )
        if not chosen:
            return False
        resolved, error = validate_ffmpeg_dir(chosen)
        if error is not None:
            messagebox.showwarning(
                "Invalid FFmpeg Binary",
                f"FFmpeg in:\n{chosen}\n\ncannot run on this system:\n{error}\n\n"
                "Please choose a build matching your system architecture (e.g. win64 on 64-bit x64 Windows).",
                parent=self._root,
            )
            return False
        if resolved is None:
            exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
            messagebox.showwarning(
                "FFmpeg Not Found",
                f"Could not find {exe_name} inside:\n{chosen}\n\n"
                "Please choose the folder (or its bin subfolder) containing ffmpeg.",
                parent=self._root,
            )
            return False
        self._controller.save_ffmpeg_dir(resolved)
        self.update_status()
        self._status_var.set("FFmpeg folder updated")
        self._on_log(f"Using FFmpeg from: {resolved}")
        return True

    def reset_dir(self) -> None:
        """Clear any custom `ffmpeg_dir` setting and fall back to system PATH."""
        self._controller.save_ffmpeg_dir(None)
        self.update_status()
        self._status_var.set("FFmpeg folder reset to system PATH")
        self._on_log("Reset FFmpeg folder to system PATH")

    def ensure_available(self, options: DownloadOptions) -> bool:
        """Validate FFmpeg/FFprobe up front when combine, split, or format conversion is enabled."""
        if getattr(options, "cover", False):
            return True
        if getattr(options, "media_type", "auto") == "ebook":
            return True
        try:
            has_explicit_bitrate = normalize_audio_bitrate(getattr(options, "audio_bitrate", None)) is not None
        except ValueError:
            has_explicit_bitrate = False
        try:
            has_sample_rate = normalize_sample_rate(getattr(options, "sample_rate", None)) is not None
        except ValueError:
            has_sample_rate = False
        needs_ffmpeg = bool(
            options.combine
            or options.output_format
            or options.split_chapters
            or getattr(options, "loudnorm", False)
            or getattr(options, "mono", False)
            or has_explicit_bitrate
            or has_sample_rate
            or getattr(options, "media_type", "auto") == "video"
        )
        ffmpeg_path, ffmpeg_err = self.check_tool("ffmpeg")
        ffprobe_path, ffprobe_err = self.check_tool("ffprobe")
        if not needs_ffmpeg:
            if not ffprobe_err:
                return True
            choice = messagebox.askyesnocancel(
                "FFprobe Binary Error",
                f"ffprobe was found at:\n{ffprobe_path}\n\nbut failed to run:\n{ffprobe_err}\n\n"
                "Media integrity verification will fail until this is fixed.\n\n"
                "Yes: Locate a working FFmpeg folder now\n"
                "No: Continue anyway\n"
                "Cancel: Stop and view help",
                parent=self._root,
            )
            if choice is True:
                if self.browse_dir():
                    _, probe_err = self.check_tool("ffprobe")
                    return probe_err is None
                return False
            if choice is False:
                return True
            self.show_guidance()
            return False

        needs_ffprobe = bool(options.combine or options.split_chapters)
        has_ffmpeg = ffmpeg_path is not None and ffmpeg_err is None
        has_ffprobe = ffprobe_path is not None and ffprobe_err is None
        if has_ffmpeg and (not needs_ffprobe or has_ffprobe):
            return True

        issues = []
        if ffmpeg_err:
            issues.append(f"ffmpeg ({ffmpeg_path}): {ffmpeg_err}")
        elif not ffmpeg_path:
            issues.append("ffmpeg was not found")
        if ffprobe_err and (needs_ffprobe or not ffmpeg_err):
            issues.append(f"ffprobe ({ffprobe_path}): {ffprobe_err}")
        elif needs_ffprobe and not ffprobe_path:
            issues.append("ffprobe was not found")

        reasons = []
        if options.combine:
            reasons.append("combine parts")
        if options.split_chapters:
            reasons.append("split chapters")
        if options.output_format:
            reasons.append(f"convert to .{options.output_format}")
        if (
            getattr(options, "loudnorm", False)
            or getattr(options, "mono", False)
            or has_explicit_bitrate
            or has_sample_rate
        ):
            reasons.append("process audio")
        reason_text = " and ".join(reasons) if reasons else "process media"
        issue_text = "\n".join(issues)

        choice = messagebox.askyesnocancel(
            "FFmpeg Required",
            f"FFmpeg/FFprobe is unusable or missing, which is needed to {reason_text}:\n{issue_text}\n\n"
            "Yes: Locate FFmpeg folder now\n"
            "No: Continue anyway (may fail after downloading parts)\n"
            "Cancel: Stop and view setup instructions",
            parent=self._root,
        )
        if choice is True:
            if self.browse_dir():
                ff_p, ff_e = self.check_tool("ffmpeg")
                fp_p, fp_e = self.check_tool("ffprobe")
                return bool(ff_p and not ff_e and (not needs_ffprobe or (fp_p and not fp_e)))
            return False
        if choice is False:
            return True
        self.show_guidance()
        return False

    def install_via_winget(self) -> None:
        """Install FFmpeg via Windows winget (`Gyan.FFmpeg`) on a background thread."""
        if not sys.platform.startswith("win") or not winget_available():
            messagebox.showinfo(
                "winget Unavailable",
                "Windows Package Manager (winget) was not found on PATH.",
                parent=self._root,
            )
            return
        if not messagebox.askyesno(
            "Install FFmpeg via winget",
            "Run `winget install -e --id Gyan.FFmpeg` to install FFmpeg?",
            parent=self._root,
        ):
            return
        self._status_var.set("Installing FFmpeg via winget...")
        self._on_log("Installing FFmpeg via winget (Gyan.FFmpeg)...")
        self._controller.start_winget_ffmpeg_install()

    def handle_install_done(self, ok: bool, detail: str) -> None:
        """Update UI and notify the user when background winget installation finishes."""
        self.update_status()
        if ok:
            self._status_var.set("FFmpeg installed")
            self._on_log(detail)
            messagebox.showinfo("FFmpeg Installed", detail, parent=self._root)
        else:
            self._status_var.set("FFmpeg install failed")
            self._on_log(f"[ERROR] {detail}")
            messagebox.showerror("FFmpeg Install Failed", detail, parent=self._root)

    def show_guidance(self) -> None:
        """Display resolved FFmpeg paths, binary error details, or installation steps."""
        ffmpeg_path, ffmpeg_err = self.check_tool("ffmpeg")
        ffprobe_path, ffprobe_err = self.check_tool("ffprobe")
        if ffmpeg_path and not ffmpeg_err and ffprobe_path and not ffprobe_err:
            messagebox.showinfo(
                "FFmpeg Ready",
                f"ffmpeg: {ffmpeg_path}\nffprobe: {ffprobe_path}",
                parent=self._root,
            )
            return
        if ffmpeg_err or ffprobe_err:
            details = []
            if ffmpeg_err:
                details.append(f"ffmpeg ({ffmpeg_path}):\n  {ffmpeg_err}")
            if ffprobe_err:
                details.append(f"ffprobe ({ffprobe_path}):\n  {ffprobe_err}")
            detail_text = "\n\n".join(details)
            custom_dir = self._controller.config_snapshot().ffmpeg_dir
            if custom_dir:
                if messagebox.askyesno(
                    "FFmpeg Binary Error",
                    f"The configured FFmpeg binaries cannot run on this system:\n\n{detail_text}\n\n"
                    f"Configured folder: {custom_dir}\n\n"
                    "Reset custom FFmpeg folder and use system PATH instead?",
                    parent=self._root,
                ):
                    self.reset_dir()
                return
            messagebox.showerror(
                "FFmpeg Binary Error",
                f"FFmpeg binaries failed to run on this system:\n\n{detail_text}",
                parent=self._root,
            )
            return
        exe_dir = (
            os.path.dirname(os.path.abspath(sys.executable))
            if getattr(sys, "frozen", False)
            else os.getcwd()
        )
        messagebox.showinfo(
            "FFmpeg Setup Guide",
            "FFmpeg and FFprobe are needed to combine multi-part books or convert to .m4b/.mp3.\n\n"
            "Quickest setup:\n"
            "1. Click 'Locate FFmpeg...' in the top-right to point to an existing ffmpeg folder.\n"
            f"2. Or place ffmpeg.exe and ffprobe.exe in:\n   {exe_dir}\n\n"
            "Install via package manager:\n"
            "   Windows: winget install Gyan.FFmpeg\n"
            "   macOS:   brew install ffmpeg\n"
            "   Linux:   sudo apt install ffmpeg\n\n"
            "Download builds: https://ffmpeg.org/download.html",
            parent=self._root,
        )
