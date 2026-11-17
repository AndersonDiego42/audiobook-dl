"""Left-sidebar navigation shell, lazy page router, and persistent status footer."""

from __future__ import annotations

from dataclasses import dataclass
import tkinter as tk
from tkinter import ttk
from collections.abc import Callable
from typing import Any

from audiobookdl import __version__
from audiobookdl.config import is_portable_mode
from audiobookdl.gui.theme import (
    ThemePalette,
    apply_theme_to_widget_tree,
    make_bold_font,
    scale_px,
)


@dataclass(frozen=True)
class NavItemSpec:
    """Metadata describing one sidebar navigation destination."""

    page_id: str
    glyph: str
    label: str
    shortcut_hint: str


NAV_ITEMS: tuple[NavItemSpec, ...] = (
    NavItemSpec("download", "\u21e9", "Download", "Ctrl+1"),
    NavItemSpec("library", "\u25a4", "Library", "Ctrl+2"),
    NavItemSpec("pack", "\u25a6", "Pack", "Ctrl+3"),
    NavItemSpec("accounts", "\u25c8", "Accounts", "Ctrl+4"),
    NavItemSpec("settings", "\u2699", "Settings", "Ctrl+5"),
)


class SidebarShell(ttk.Frame):
    """Fixed-width sidebar shell with lazy page mounting, nav badges, and live footer."""

    SIDEBAR_WIDTH_DP = 210

    def __init__(
        self,
        parent: tk.Misc,
        *,
        palette_provider: Callable[[], ThemePalette],
        status_var: tk.StringVar,
        active_job_var: tk.StringVar,
        progress_var: tk.DoubleVar,
        on_toggle_pause: Callable[[], None],
        on_cancel: Callable[[], None],
        on_page_changed: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._palette_provider = palette_provider
        self._on_page_changed = on_page_changed
        self._factories: dict[str, Callable[[ttk.Frame], ttk.Frame]] = {}
        self._pages: dict[str, ttk.Frame] = {}
        self._nav_buttons: dict[str, ttk.Button] = {}
        self._nav_order: list[str] = [spec.page_id for spec in NAV_ITEMS]
        self._active_page_id: str | None = None
        self._badge_labels: dict[str, ttk.Label] = {}

        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=0)
        self.columnconfigure(2, weight=1)
        self.rowconfigure(0, weight=1)

        sidebar_w = scale_px(self, self.SIDEBAR_WIDTH_DP)
        self.sidebar = ttk.Frame(self, style="Sidebar.TFrame", width=sidebar_w)
        self.sidebar.grid(row=0, column=0, sticky="ns")
        self.sidebar.grid_propagate(False)
        self.sidebar.columnconfigure(0, weight=1)
        self.sidebar.rowconfigure(2, weight=1)

        # Branding header
        brand = ttk.Frame(
            self.sidebar,
            style="Sidebar.TFrame",
            padding=(scale_px(self, 12), scale_px(self, 12), scale_px(self, 12), scale_px(self, 8)),
        )
        brand.grid(row=0, column=0, sticky="ew")
        ttk.Label(
            brand,
            text="audiobook-dl",
            style="Sidebar.TLabel",
            font=make_bold_font(self, 12),
        ).pack(anchor="w")
        sub = f"v{__version__}" + ("  [Portable]" if is_portable_mode() else "")
        ttk.Label(brand, text=sub, style="SidebarMuted.TLabel").pack(anchor="w")

        # Navigation items
        nav_box = ttk.Frame(
            self.sidebar,
            style="Sidebar.TFrame",
            padding=(scale_px(self, 6), scale_px(self, 4)),
        )
        nav_box.grid(row=1, column=0, sticky="ew")
        nav_box.columnconfigure(0, weight=1)

        for idx, spec in enumerate(NAV_ITEMS):
            row_frame = ttk.Frame(nav_box, style="Sidebar.TFrame")
            row_frame.grid(row=idx, column=0, sticky="ew", pady=1)
            row_frame.columnconfigure(0, weight=1)

            def _bind_nav(button: ttk.Button, row_idx: int, page_id: str) -> None:
                button.configure(command=lambda: self.navigate(page_id))
                button.bind("<Up>", lambda _e: self._move_nav_focus(row_idx, -1))
                button.bind("<Down>", lambda _e: self._move_nav_focus(row_idx, 1))
                button.bind("<Return>", lambda _e: self._activate_from_key(page_id))
                button.bind("<space>", lambda _e: self._activate_from_key(page_id))

            btn = ttk.Button(
                row_frame,
                text=f"{spec.glyph}  {spec.label}",
                style="SidebarNav.TButton",
                takefocus=True,
            )
            btn.grid(row=0, column=0, sticky="ew")
            _bind_nav(btn, idx, spec.page_id)
            self._nav_buttons[spec.page_id] = btn

            if spec.page_id == "download":
                badge = ttk.Label(row_frame, text="", style="SidebarBadge.TLabel")
                badge.grid(row=0, column=1, padx=(2, 2))
                badge.grid_remove()
                self._badge_labels[spec.page_id] = badge

        # Spacer
        ttk.Frame(self.sidebar, style="Sidebar.TFrame").grid(row=2, column=0, sticky="nsew")

        # Persistent status footer
        ttk.Separator(self.sidebar, orient=tk.HORIZONTAL).grid(row=3, column=0, sticky="ew")
        footer = ttk.Frame(
            self.sidebar,
            style="Sidebar.TFrame",
            padding=(scale_px(self, 10), scale_px(self, 8)),
        )
        footer.grid(row=4, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)

        ttk.Label(
            footer,
            textvariable=active_job_var,
            style="Sidebar.TLabel",
            wraplength=sidebar_w - scale_px(self, 22),
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.footer_progressbar = ttk.Progressbar(
            footer,
            mode="determinate",
            maximum=100,
            variable=progress_var,
        )
        self.footer_progressbar.grid(row=1, column=0, sticky="ew", pady=(0, 4))

        ttk.Label(
            footer,
            textvariable=status_var,
            style="SidebarMuted.TLabel",
            wraplength=sidebar_w - scale_px(self, 22),
        ).grid(row=2, column=0, sticky="w", pady=(0, 6))

        footer_btns = ttk.Frame(footer, style="Sidebar.TFrame")
        footer_btns.grid(row=3, column=0, sticky="ew")
        footer_btns.columnconfigure(0, weight=1)
        footer_btns.columnconfigure(1, weight=1)
        self.footer_pause_btn = ttk.Button(
            footer_btns, text="Pause", command=on_toggle_pause, state="disabled"
        )
        self.footer_pause_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.footer_cancel_btn = ttk.Button(
            footer_btns, text="Cancel", command=on_cancel, state="disabled"
        )
        self.footer_cancel_btn.grid(row=0, column=1, sticky="ew")

        # Vertical divider + Page content stack
        ttk.Separator(self, orient=tk.VERTICAL).grid(row=0, column=1, sticky="ns")
        self.content = ttk.Frame(self)
        self.content.grid(row=0, column=2, sticky="nsew")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(0, weight=1)

    @property
    def active_page_id(self) -> str | None:
        return self._active_page_id

    def _move_nav_focus(self, current_idx: int, delta: int) -> str:
        next_idx = (current_idx + delta) % len(self._nav_order)
        target_id = self._nav_order[next_idx]
        self._nav_buttons[target_id].focus_set()
        return "break"

    def _activate_from_key(self, page_id: str) -> str:
        self.navigate(page_id)
        return "break"

    def register_page(
        self,
        page_id: str,
        factory: Callable[[ttk.Frame], ttk.Frame],
    ) -> None:
        """Register a lazy page factory for `page_id`."""
        self._factories[page_id] = factory

    def get_page(self, page_id: str) -> Any | None:
        """Return the already-built page widget for `page_id`, or `None` if unvisited."""
        return self._pages.get(page_id)

    def built_page_ids(self) -> tuple[str, ...]:
        """Return page IDs that have already been instantiated."""
        return tuple(self._pages.keys())

    def ensure_page(self, page_id: str) -> Any:
        """Return the page for `page_id`, building it without changing the active view if needed."""
        if page_id not in self._factories:
            raise KeyError(f"Unknown page id: {page_id!r}")
        page = self._pages.get(page_id)
        if page is None:
            page = self._factories[page_id](self.content)
            self._pages[page_id] = page
            apply_theme_to_widget_tree(page, self._palette_provider())
        return page

    def navigate(self, page_id: str) -> Any:
        """Switch to `page_id`, lazily instantiating it on first visit and calling `on_show()`."""
        if page_id not in self._factories:
            raise KeyError(f"Unknown page id: {page_id!r}")

        if self._active_page_id and self._active_page_id in self._pages:
            self._pages[self._active_page_id].grid_remove()

        page = self.ensure_page(page_id)
        page.grid(row=0, column=0, sticky="nsew")

        self._active_page_id = page_id
        for pid, btn in self._nav_buttons.items():
            btn.configure(style="SidebarNavActive.TButton" if pid == page_id else "SidebarNav.TButton")

        on_show = getattr(page, "on_show", None)
        if callable(on_show):
            on_show()
        if self._on_page_changed is not None:
            self._on_page_changed(page_id)
        return page

    def set_download_badge(self, active_count: int, failed_count: int) -> None:
        """Update the queue/failure badge next to the Download nav button."""
        badge = self._badge_labels.get("download")
        if badge is None:
            return
        parts: list[str] = []
        if active_count > 0:
            parts.append(f"{active_count} queued")
        if failed_count > 0:
            parts.append(f"{failed_count} failed")
        if parts:
            badge.configure(text=", ".join(parts))
            badge.grid()
        else:
            badge.configure(text="")
            badge.grid_remove()

    def set_footer_running_state(
        self,
        *,
        running: bool,
        paused: bool,
        previewing: bool,
        syncing: bool,
    ) -> None:
        if running:
            self.footer_pause_btn.configure(
                text="Resume" if paused else "Pause",
                state="normal",
            )
            self.footer_cancel_btn.configure(state="normal")
        elif previewing or syncing:
            self.footer_pause_btn.configure(text="Pause", state="disabled")
            self.footer_cancel_btn.configure(state="normal")
        else:
            self.footer_pause_btn.configure(text="Pause", state="disabled")
            self.footer_cancel_btn.configure(state="disabled")

    def set_footer_progress_mode(self, mode: str) -> None:
        if mode == "indeterminate":
            self.footer_progressbar.configure(mode="indeterminate")
            self.footer_progressbar.start(12)
        else:
            self.footer_progressbar.stop()
            self.footer_progressbar.configure(mode="determinate")

    def apply_theme(self, palette: ThemePalette) -> None:
        for page in self._pages.values():
            hook = getattr(page, "apply_theme", None)
            if callable(hook):
                hook(palette)
