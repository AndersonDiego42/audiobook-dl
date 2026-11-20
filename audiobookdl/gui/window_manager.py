"""Desktop window geometry, theme watcher, tray lifecycle, menu bar, and OS shell helpers."""

from __future__ import annotations

from collections.abc import Callable
import importlib.resources
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from audiobookdl import __version__
from audiobookdl.config import is_portable_mode
from audiobookdl.gui.controller import GuiController
from audiobookdl.gui.formatting import (
    clamp_geometry_to_monitors,
    clamp_rect_to_monitor,
    format_geometry,
    get_monitor_work_areas,
    is_valid_geometry,
)
from audiobookdl.gui.shell import NAV_ITEMS
from audiobookdl.gui.theme import (
    THEME_LABELS,
    THEME_MODES,
    ThemePalette,
    _configure_clam_palette,
    apply_theme,
    configure_hidpi_scaling,
    detect_system_theme,
    get_dpi_scale,
    make_bold_font,
    normalize_theme_mode,
    scale_px,
)
from audiobookdl.gui.tray import TrayIconController
from audiobookdl.gui.widgets import ModalDialog


class AboutDialog(ModalDialog):
    """Themed modal About dialog displaying version, mode, and project link."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        services_count: int,
        palette: ThemePalette | None = None,
    ) -> None:
        super().__init__(
            parent,
            "About audiobook-dl",
            palette=palette,
            resizable=(False, False),
        )
        body = ttk.Frame(self, padding=scale_px(self, 16))
        body.pack(fill=tk.BOTH, expand=True)

        mode_suffix = " [Portable]" if is_portable_mode() else ""
        ttk.Label(
            body,
            text=f"audiobook-dl  v{__version__}{mode_suffix}",
            font=make_bold_font(self, 3),
        ).pack(anchor="w", pady=(0, scale_px(self, 6)))

        self._info_text = (
            f"audiobook-dl v{__version__}{mode_suffix}\n"
            f"Supported services: {services_count}\n"
            "https://github.com/jo1gi/audiobook-dl"
        )
        ttk.Label(
            body,
            text=f"Supported services: {services_count}\nhttps://github.com/jo1gi/audiobook-dl",
            justify="left",
        ).pack(anchor="w", pady=(0, scale_px(self, 12)))

        btns = ttk.Frame(body)
        btns.pack(fill=tk.X)
        self._copy_after_id: str | None = None
        self._copy_btn = ttk.Button(btns, text="Copy Info", command=self._copy_info)
        self._copy_btn.pack(side=tk.LEFT)
        close_btn = ttk.Button(btns, text="Close", command=self.destroy, style="Accent.TButton")
        close_btn.pack(side=tk.RIGHT)
        self.finalize_modal(initial_focus=close_btn)

    def _copy_info(self) -> None:
        target: tk.Misc = self._owner if getattr(self._owner, "winfo_exists", lambda: False)() else self
        target.clipboard_clear()
        target.clipboard_append(self._info_text)
        target.update_idletasks()
        if self._copy_after_id is not None:
            try:
                self.after_cancel(self._copy_after_id)
            except tk.TclError:
                pass
            self._copy_after_id = None
        self._copy_btn.configure(text="Copied!")

        def _restore() -> None:
            self._copy_after_id = None
            try:
                if self.winfo_exists():
                    self._copy_btn.configure(text="Copy Info")
            except tk.TclError:
                pass

        self._copy_after_id = self.after(1500, _restore)


def _spawn_detached_posix(argv: list[str]) -> None:
    """Launch a detached POSIX child process and reap it on exit to prevent zombies."""
    proc = subprocess.Popen(argv)
    if proc is not None and hasattr(proc, "wait"):
        threading.Thread(
            target=proc.wait,
            daemon=True,
            name="audiobook-dl-os-reaper",
        ).start()


def open_path_in_os(parent: tk.Misc, target_path: str, reveal_in_folder: bool = False) -> None:
    """Open `target_path` or reveal it in the system file manager."""
    cleaned = (target_path or "").strip()
    if not cleaned or not os.path.exists(cleaned):
        messagebox.showinfo(
            "Path Missing",
            f"Path does not exist:\n{cleaned}",
            parent=parent,
        )
        return
    try:
        if reveal_in_folder and os.path.isfile(cleaned):
            if sys.platform.startswith("win"):
                norm_path = os.path.normpath(os.path.abspath(cleaned))
                subprocess.Popen(["explorer", f"/select,{norm_path}"])
            elif sys.platform == "darwin":
                _spawn_detached_posix(["open", "-R", cleaned])
            else:
                _spawn_detached_posix(["xdg-open", os.path.dirname(os.path.abspath(cleaned)) or "."])
            return
        open_target = (
            os.path.dirname(os.path.abspath(cleaned)) or "."
            if reveal_in_folder and not os.path.isdir(cleaned)
            else cleaned
        )
        startfile = getattr(os, "startfile", None)
        if sys.platform.startswith("win") and startfile is not None:
            startfile(open_target)
        elif sys.platform == "darwin":
            _spawn_detached_posix(["open", open_target])
        else:
            _spawn_detached_posix(["xdg-open", open_target])
    except OSError as exc:
        messagebox.showerror(
            "Open Path Failed",
            f"Could not open:\n{cleaned}\n\n{exc}",
            parent=parent,
        )


def build_menu_bar(
    root: tk.Tk,
    *,
    theme_var: tk.StringVar,
    on_browse_url_file: Callable[[], None],
    on_open_output_folder: Callable[[], None],
    on_reload_config: Callable[[], None],
    on_exit: Callable[[], None],
    on_go_back: Callable[[], object],
    on_go_forward: Callable[[], object],
    on_set_theme: Callable[[str], object],
    on_navigate: Callable[[str], object],
    on_open_library_tab: Callable[[str], object],
    on_check_updates: Callable[[], None],
    on_show_about: Callable[[], object],
    on_preview_metadata: Callable[[], None],
    on_start_download: Callable[[], None],
) -> None:
    """Attach the application menu bar and root keyboard shortcuts."""
    is_macos = sys.platform == "darwin"
    accel_mod = "Cmd" if is_macos else "Ctrl"
    bind_mod = "Command" if is_macos else "Control"

    menubar = tk.Menu(root, tearoff=0)
    file_menu = tk.Menu(menubar, tearoff=0)
    file_menu.add_command(
        label="Load URLs from .txt...", command=on_browse_url_file, accelerator=f"{accel_mod}+O"
    )
    file_menu.add_command(label="Open Output Folder", command=on_open_output_folder)
    file_menu.add_command(label="Reload Config", command=on_reload_config)
    file_menu.add_separator()
    file_menu.add_command(label="Exit", command=on_exit, accelerator=f"{accel_mod}+Q")
    menubar.add_cascade(label="File", menu=file_menu)

    view_menu = tk.Menu(menubar, tearoff=0)
    view_menu.add_command(label="Back", command=on_go_back, accelerator="Alt+Left")
    view_menu.add_command(label="Forward", command=on_go_forward, accelerator="Alt+Right")
    view_menu.add_separator()
    theme_menu = tk.Menu(view_menu, tearoff=0)
    for mode in THEME_MODES:

        def _theme_cb(m: str = mode) -> None:
            on_set_theme(m)

        theme_menu.add_radiobutton(
            label=THEME_LABELS[mode],
            variable=theme_var,
            value=mode,
            command=_theme_cb,
        )
    view_menu.add_cascade(label="Theme", menu=theme_menu)
    view_menu.add_separator()
    for spec in NAV_ITEMS:

        def _make_nav_cb(page_id: str = spec.page_id) -> object:
            return on_navigate(page_id)

        nav_accel = (
            spec.shortcut_hint.replace("Ctrl+", "Cmd+") if is_macos else spec.shortcut_hint
        )
        view_menu.add_command(
            label=spec.label,
            command=_make_nav_cb,
            accelerator=nav_accel,
        )
    lib_tabs_menu = tk.Menu(view_menu, tearoff=0)
    for tab_key, tab_label in (
        ("archive", "Archive Ledger"),
        ("catalog", "Catalog & Search"),
        ("sync", "Library Sync"),
    ):

        def _make_lib_cb(tk_key: str = tab_key) -> object:
            return on_open_library_tab(tk_key)

        lib_tabs_menu.add_command(label=tab_label, command=_make_lib_cb)
    view_menu.add_cascade(label="Library Tabs", menu=lib_tabs_menu)
    menubar.add_cascade(label="View", menu=view_menu)

    help_menu = tk.Menu(menubar, tearoff=0)
    help_menu.add_command(label="Check for Updates...", command=on_check_updates)
    help_menu.add_separator()
    help_menu.add_command(label="About audiobook-dl", command=on_show_about)
    menubar.add_cascade(label="Help", menu=help_menu)

    root.configure(menu=menubar)

    def _is_text_focused(ev: tk.Event) -> bool:
        try:
            focus = root.focus_get()
        except (tk.TclError, AttributeError):
            focus = getattr(ev, "widget", None)
        return isinstance(focus, tk.Text)

    def _bind_global(key: str, callback: Callable[[], object], *, skip_in_text: bool = False) -> None:
        def _handler(ev: tk.Event) -> str | None:
            if skip_in_text and not is_macos and _is_text_focused(ev):
                return None
            callback()
            return "break"

        for seq in (f"<{bind_mod}-{key.lower()}>", f"<{bind_mod}-{key.upper()}>"):
            try:
                root.bind(seq, _handler)
            except tk.TclError:
                pass

    _bind_global("o", on_browse_url_file, skip_in_text=True)
    _bind_global("q", on_exit)
    _bind_global("p", on_preview_metadata, skip_in_text=True)

    def _start_download_shortcut(_e: tk.Event) -> str:
        on_start_download()
        return "break"

    root.bind("<F5>", _start_download_shortcut)

    def _nav_back(_e: tk.Event) -> str:
        on_go_back()
        return "break"

    def _nav_forward(_e: tk.Event) -> str:
        on_go_forward()
        return "break"

    root.bind("<Alt-Left>", _nav_back)
    root.bind("<Alt-Right>", _nav_forward)
    for idx, spec in enumerate(NAV_ITEMS, start=1):

        def _make_shortcut_cb(_e: tk.Event, page_id: str = spec.page_id) -> str:
            on_navigate(page_id)
            return "break"

        try:
            root.bind(f"<{bind_mod}-Key-{idx}>", _make_shortcut_cb)
        except tk.TclError:
            pass


class WindowManager:
    """Manages window geometry/maximization, theme switching, system tray, and About dialog."""

    DEFAULT_WIDTH_DP = 960
    DEFAULT_HEIGHT_DP = 680
    MIN_WIDTH_DP = 760
    MIN_HEIGHT_DP = 520

    def __init__(
        self,
        root: tk.Tk,
        controller: GuiController,
        style: ttk.Style,
        *,
        services_count: int,
        status_var: tk.StringVar,
        minimize_to_tray_var: tk.BooleanVar,
        on_sync_now: Callable[[], None],
        on_exit: Callable[[], None],
        on_theme_applied: Callable[[ThemePalette], None],
        on_tray_started: Callable[[], None] | None = None,
        on_dpi_changed: Callable[[], None] | None = None,
    ) -> None:
        self._root = root
        self._controller = controller
        self._style = style
        self._services_count = services_count
        self._status_var = status_var
        self._minimize_to_tray_var = minimize_to_tray_var
        self._on_theme_applied = on_theme_applied
        self._on_tray_started = on_tray_started
        self._on_dpi_changed = on_dpi_changed
        self._last_dpi_scale = get_dpi_scale(self._root)

        self.normal_geometry: str | None = None
        self.is_maximized = False
        self.restore_geometry()
        self.apply_window_icon()

        self._theme_mode = normalize_theme_mode(controller.config.gui.theme)
        self.theme_var = tk.StringVar(value=self._theme_mode)
        self._last_system_theme = detect_system_theme()
        self.theme_watch_after_id: str | None = None
        self.palette: ThemePalette = apply_theme(
            self._root,
            self._style,
            self._theme_mode,
            walk_tree=False,
        )
        self.about_dialog: AboutDialog | None = None

        self.tray_controller = TrayIconController(
            on_restore=self.restore_from_tray,
            on_sync_now=on_sync_now,
            on_exit=on_exit,
        )

    @property
    def theme_mode(self) -> str:
        return self._theme_mode

    def restore_geometry(self) -> None:
        min_w = scale_px(self._root, self.MIN_WIDTH_DP)
        min_h = scale_px(self._root, self.MIN_HEIGHT_DP)
        self._root.minsize(min_w, min_h)

        monitors = get_monitor_work_areas(self._root)
        saved_geometry = (self._controller.config.gui.window_geometry or "").strip()
        clamped = (
            clamp_geometry_to_monitors(saved_geometry, monitors, min_width=min_w, min_height=min_h)
            if saved_geometry
            else None
        )
        if clamped is not None:
            self.normal_geometry = clamped
            self._root.geometry(clamped)
        else:
            default_w = scale_px(self._root, self.DEFAULT_WIDTH_DP)
            default_h = scale_px(self._root, self.DEFAULT_HEIGHT_DP)
            cw, ch, cx, cy = clamp_rect_to_monitor(
                default_w,
                default_h,
                None,
                None,
                monitors,
                min_width=min_w,
                min_height=min_h,
            )
            geom = format_geometry(cw, ch, cx, cy)
            self.normal_geometry = geom
            self._root.geometry(geom)

        self.is_maximized = bool(self._controller.config.gui.window_maximized)
        if self.is_maximized:
            self.apply_maximized_state()

    def apply_maximized_state(self) -> None:
        try:
            if self._root.state() == "withdrawn":
                return
        except tk.TclError:
            return
        try:
            self._root.state("zoomed")
            return
        except tk.TclError:
            pass
        try:
            self._root.attributes("-zoomed", True)
        except tk.TclError:
            pass

    def is_window_maximized(self) -> bool:
        try:
            if self._root.state() == "zoomed":
                return True
        except tk.TclError:
            pass
        try:
            if bool(self._root.attributes("-zoomed")):
                return True
        except tk.TclError:
            pass
        try:
            if self._root.state() == "withdrawn":
                return self.is_maximized
        except tk.TclError:
            pass
        return False

    def on_root_configure(self, event: tk.Event) -> None:
        if getattr(event, "widget", None) is not self._root:
            return
        try:
            current_scale = get_dpi_scale(self._root)
            if abs(current_scale - self._last_dpi_scale) >= 0.05:
                self._last_dpi_scale = current_scale
                configure_hidpi_scaling(self._root, self._style)
                _configure_clam_palette(self._style, self.palette, root=self._root)
                self._root.minsize(
                    scale_px(self._root, self.MIN_WIDTH_DP),
                    scale_px(self._root, self.MIN_HEIGHT_DP),
                )
                if self._on_dpi_changed is not None:
                    self._on_dpi_changed()
            if self.is_window_maximized():
                self.is_maximized = True
                return
            state = self._root.state()
            if state == "normal":
                self.is_maximized = False
                geom = self._root.geometry()
                if is_valid_geometry(geom):
                    self.normal_geometry = geom
        except tk.TclError:
            pass

    def apply_window_icon(self) -> None:
        try:
            icon_ref = importlib.resources.files("audiobookdl").joinpath("assets/icon.ico")
            with importlib.resources.as_file(icon_ref) as icon_path:
                if icon_path.is_file():
                    if sys.platform.startswith("win"):
                        try:
                            self._root.iconbitmap(default=str(icon_path))  # type: ignore[no-untyped-call]
                        except tk.TclError:
                            pass
                    try:
                        from PIL import Image, ImageTk

                        with Image.open(icon_path) as img:
                            photo: Any = ImageTk.PhotoImage(
                                img.convert("RGBA"), master=self._root
                            )
                        self._icon_photo: Any = photo
                        self._root.iconphoto(True, photo)
                    except (OSError, ValueError, tk.TclError):
                        pass
        except Exception:
            pass

    def set_theme(self, mode: str) -> ThemePalette:
        """Switch between `'system'`, `'light'`, and `'dark'` themes live."""
        self._theme_mode = normalize_theme_mode(mode)
        self.theme_var.set(self._theme_mode)
        if self._theme_mode == "system":
            self._last_system_theme = detect_system_theme()
        self.palette = apply_theme(self._root, self._style, self._theme_mode)
        self._on_theme_applied(self.palette)
        self.sync_system_theme_watch()
        return self.palette

    def cancel_theme_watch(self) -> None:
        after_id = getattr(self, "theme_watch_after_id", None)
        self.theme_watch_after_id = None
        if after_id is not None:
            try:
                self._root.after_cancel(after_id)
            except tk.TclError:
                pass

    def sync_system_theme_watch(self) -> None:
        self.cancel_theme_watch()
        if self._theme_mode != "system":
            return
        try:
            if self._root.winfo_exists():
                self.theme_watch_after_id = self._root.after(2500, self.check_system_theme)
        except tk.TclError:
            self.theme_watch_after_id = None

    def check_system_theme(self) -> None:
        self.theme_watch_after_id = None
        if self._theme_mode != "system":
            return
        try:
            if not self._root.winfo_exists():
                return
        except tk.TclError:
            return
        current = detect_system_theme()
        if current != self._last_system_theme:
            self._last_system_theme = current
            self.set_theme("system")
            return
        self.sync_system_theme_watch()

    def show_about_dialog(self) -> AboutDialog:
        if self.about_dialog is not None:
            try:
                if self.about_dialog.winfo_exists():
                    self.about_dialog.lift()
                    self.about_dialog.focus_set()
                    return self.about_dialog
            except tk.TclError:
                pass
        self.about_dialog = AboutDialog(
            self._root,
            services_count=self._services_count,
            palette=self.palette,
        )
        return self.about_dialog

    def toggle_minimize_to_tray(self) -> None:
        enabled = bool(self._minimize_to_tray_var.get())
        try:
            self._controller.save_gui_preferences(minimize_to_tray=enabled)
        except OSError:
            pass
        if not enabled:
            self.tray_controller.stop()
            self._status_var.set("Minimize to tray disabled")
        else:
            self._status_var.set("Minimize to tray enabled")

    def restore_from_tray(self) -> None:
        self.tray_controller.stop()
        try:
            if self._root.winfo_exists():
                was_maximized = self.is_maximized
                self._root.deiconify()
                if was_maximized:
                    self.apply_maximized_state()
                self._root.lift()
                self._root.focus_force()
        except tk.TclError:
            pass

    def minimize_to_tray(self) -> bool:
        if not self.tray_controller.start():
            return False
        try:
            self._root.withdraw()
        except tk.TclError:
            self.tray_controller.stop()
            return False
        self._status_var.set("Minimized to system tray (scheduled sync active)")
        if self._on_tray_started is not None:
            self._on_tray_started()
        return True

    def should_minimize_on_close(self, *, force: bool) -> bool:
        return (
            not force
            and bool(self._minimize_to_tray_var.get())
            and self.tray_controller.is_available()
            and self.minimize_to_tray()
        )

    def collect_geometry_snapshot(self) -> tuple[str | None, bool]:
        maximized = self.is_window_maximized()
        if not maximized:
            current_geom = self._root.geometry()
            if is_valid_geometry(current_geom):
                self.normal_geometry = current_geom
        saved_geom = (
            self.normal_geometry
            if (self.normal_geometry and is_valid_geometry(self.normal_geometry))
            else None
        )
        return saved_geom, maximized
