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
from audiobookdl.gui.widgets import attach_tooltip


@dataclass(frozen=True)
class NavItemSpec:
    """Metadata describing one sidebar navigation destination."""

    page_id: str
    glyph: str
    label: str
    shortcut_hint: str


@dataclass(frozen=True)
class NavRoute:
    """Canonical navigation route combining top-level page, sub-tab, and contextual state."""

    page_id: str
    sub_tab: str | None = None
    service_key: str | None = None
    profile: str | None = None

    def to_key(self) -> str:
        if self.sub_tab:
            return f"{self.page_id}:{self.sub_tab}"
        return self.page_id

    @classmethod
    def parse(cls, raw: str | NavRoute) -> NavRoute:
        if isinstance(raw, NavRoute):
            return raw
        cleaned = (raw or "").strip().lower()
        if ":" in cleaned:
            pid, sub = cleaned.split(":", 1)
            return cls(page_id=pid.strip(), sub_tab=sub.strip() or None)
        return cls(page_id=cleaned)


NAV_ITEMS: tuple[NavItemSpec, ...] = (
    NavItemSpec("download", "\u21e9", "Download", "Ctrl+1"),
    NavItemSpec("library", "\u25a4", "Library", "Ctrl+2"),
    NavItemSpec("pack", "\u25a6", "Pack", "Ctrl+3"),
    NavItemSpec("accounts", "\u25c8", "Accounts", "Ctrl+4"),
    NavItemSpec("settings", "\u2699", "Settings", "Ctrl+5"),
)

_PAGE_LABELS: dict[str, str] = {spec.page_id: spec.label for spec in NAV_ITEMS}
_SUB_TAB_LABELS: dict[tuple[str, str], str] = {
    ("library", "archive"): "Archive Ledger",
    ("library", "catalog"): "Catalog & Search",
    ("library", "sync"): "Library Sync",
}


class SidebarShell(ttk.Frame):
    """Fixed-width sidebar shell with lazy page mounting, history bar, nav badges, and live footer."""

    SIDEBAR_WIDTH_DP = 210
    MAX_HISTORY = 50

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
    ) -> None:
        super().__init__(parent)
        self._palette_provider = palette_provider
        self._factories: dict[str, Callable[[ttk.Frame], ttk.Frame]] = {}
        self._pages: dict[str, ttk.Frame] = {}
        self._nav_buttons: dict[str, ttk.Button] = {}
        self._nav_order: list[str] = [spec.page_id for spec in NAV_ITEMS]
        self._active_page_id: str | None = None
        self._active_route: NavRoute | None = None
        self._back_stack: list[NavRoute] = []
        self._forward_stack: list[NavRoute] = []
        self._breadcrumb_detail: str = ""
        self._context_action_cb: Callable[[], None] | None = None
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
        self._brand_title_label = ttk.Label(
            brand,
            text="audiobook-dl",
            style="Sidebar.TLabel",
            font=make_bold_font(self, 3),
        )
        self._brand_title_label.pack(anchor="w")
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
            attach_tooltip(
                btn,
                f"{spec.label} ({spec.shortcut_hint})",
                palette_provider=palette_provider,
            )
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

        self._active_job_label = ttk.Label(
            footer,
            textvariable=active_job_var,
            style="Sidebar.TLabel",
            wraplength=sidebar_w - scale_px(self, 22),
        )
        self._active_job_label.grid(row=0, column=0, sticky="w", pady=(0, scale_px(self, 4)))

        self.footer_progressbar = ttk.Progressbar(
            footer,
            mode="determinate",
            maximum=100,
            variable=progress_var,
        )
        self.footer_progressbar.grid(row=1, column=0, sticky="ew", pady=(0, scale_px(self, 4)))

        self._status_label = ttk.Label(
            footer,
            textvariable=status_var,
            style="SidebarMuted.TLabel",
            wraplength=sidebar_w - scale_px(self, 22),
        )
        self._status_label.grid(row=2, column=0, sticky="w", pady=(0, scale_px(self, 6)))

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

        # Vertical divider + Page content stack with compact navigation bar
        ttk.Separator(self, orient=tk.VERTICAL).grid(row=0, column=1, sticky="ns")
        self.content = ttk.Frame(self)
        self.content.grid(row=0, column=2, sticky="nsew")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(2, weight=1)

        self._nav_header = ttk.Frame(
            self.content,
            padding=(scale_px(self, 12), scale_px(self, 5), scale_px(self, 12), scale_px(self, 5)),
        )
        self._nav_header.grid(row=0, column=0, sticky="ew")
        self._nav_header.columnconfigure(2, weight=1)

        self.back_btn = ttk.Button(
            self._nav_header,
            text="\u25c0",
            width=3,
            command=self.go_back,
            state="disabled",
        )
        self.back_btn.grid(row=0, column=0, padx=(0, 3))
        attach_tooltip(self.back_btn, "Back (Alt+Left)", palette_provider=palette_provider)

        self.forward_btn = ttk.Button(
            self._nav_header,
            text="\u25b6",
            width=3,
            command=self.go_forward,
            state="disabled",
        )
        self.forward_btn.grid(row=0, column=1, padx=(0, 10))
        attach_tooltip(self.forward_btn, "Forward (Alt+Right)", palette_provider=palette_provider)

        self.breadcrumb_var = tk.StringVar(value="")
        self.breadcrumb_label = ttk.Label(
            self._nav_header,
            textvariable=self.breadcrumb_var,
            font=make_bold_font(self, 1),
        )
        self.breadcrumb_label.grid(row=0, column=2, sticky="w")

        self.context_action_btn = ttk.Button(
            self._nav_header,
            text="",
            command=self._invoke_context_action,
            style="Accent.TButton",
        )
        self.context_action_btn.grid(row=0, column=3, sticky="e", padx=(8, 0))
        self.context_action_btn.grid_remove()

        ttk.Separator(self.content, orient=tk.HORIZONTAL).grid(row=1, column=0, sticky="ew")

        self.page_host = ttk.Frame(self.content)
        self.page_host.grid(row=2, column=0, sticky="nsew")
        self.page_host.columnconfigure(0, weight=1)
        self.page_host.rowconfigure(0, weight=1)

    @property
    def active_page_id(self) -> str | None:
        return self._active_page_id

    @property
    def active_route(self) -> NavRoute | None:
        return self._active_route

    @property
    def can_go_back(self) -> bool:
        return bool(self._back_stack)

    @property
    def can_go_forward(self) -> bool:
        return bool(self._forward_stack)

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

    def ensure_page(self, page_id: str, *, apply_theme_on_create: bool = True) -> Any:
        """Return the page for `page_id`, building it without changing the active view if needed."""
        if page_id not in self._factories:
            raise KeyError(f"Unknown page id: {page_id!r}")
        page = self._pages.get(page_id)
        if page is None:
            page = self._factories[page_id](self.page_host)
            self._pages[page_id] = page
            if apply_theme_on_create:
                apply_theme_to_widget_tree(page, self._palette_provider())
        return page

    def _invoke_context_action(self) -> None:
        cb = self._context_action_cb
        if cb is not None:
            cb()

    def set_context_action(self, label: str, command: Callable[[], None]) -> None:
        """Display a contextual quick-jump button in the navigation header bar."""
        self._context_action_cb = command
        self.context_action_btn.configure(text=label)
        self.context_action_btn.grid()

    def clear_context_action(self) -> None:
        """Hide any active contextual quick-jump button in the navigation header bar."""
        self._context_action_cb = None
        self.context_action_btn.configure(text="")
        self.context_action_btn.grid_remove()

    def set_breadcrumb_detail(self, detail: str) -> None:
        """Set or clear an optional trailing context segment in the header breadcrumb."""
        self._breadcrumb_detail = (detail or "").strip()
        self._refresh_nav_header()

    def _refresh_nav_header(self) -> None:
        self.back_btn.configure(state="normal" if self._back_stack else "disabled")
        self.forward_btn.configure(state="normal" if self._forward_stack else "disabled")
        route = self._active_route
        if route is None:
            self.breadcrumb_var.set("")
            return
        parts = [_PAGE_LABELS.get(route.page_id, route.page_id.capitalize())]
        if route.sub_tab:
            sub_label = _SUB_TAB_LABELS.get(
                (route.page_id, route.sub_tab),
                route.sub_tab.capitalize(),
            )
            parts.append(sub_label)
        if self._breadcrumb_detail:
            parts.append(self._breadcrumb_detail)
        self.breadcrumb_var.set("  \u203a  ".join(parts))

    def _push_history(self, previous: NavRoute | None, new_route: NavRoute) -> None:
        if previous is None or previous == new_route:
            return
        self._back_stack.append(previous)
        if len(self._back_stack) > self.MAX_HISTORY:
            del self._back_stack[: len(self._back_stack) - self.MAX_HISTORY]
        self._forward_stack.clear()

    def sync_sub_tab(self, sub_tab: str | None) -> None:
        """Record an in-page sub-tab change (e.g. notebook tab click) into history and breadcrumb."""
        if self._active_route is None:
            return
        cleaned = sub_tab.strip().lower() if sub_tab else None
        next_route = NavRoute(
            page_id=self._active_route.page_id,
            sub_tab=cleaned,
            service_key=self._active_route.service_key,
            profile=self._active_route.profile,
        )
        if next_route == self._active_route:
            return
        self._push_history(self._active_route, next_route)
        self._active_route = next_route
        self._refresh_nav_header()

    def go_back(self) -> NavRoute | None:
        """Navigate backward to the previous route in history, if available."""
        if not self._back_stack:
            return None
        target = self._back_stack.pop()
        if self._active_route is not None:
            self._forward_stack.append(self._active_route)
        self.navigate(target, record_history=False)
        return self._active_route

    def go_forward(self) -> NavRoute | None:
        """Navigate forward to the next route in history, if available."""
        if not self._forward_stack:
            return None
        target = self._forward_stack.pop()
        if self._active_route is not None:
            self._back_stack.append(self._active_route)
        self.navigate(target, record_history=False)
        return self._active_route

    def navigate(
        self,
        target: str | NavRoute,
        *,
        record_history: bool = True,
        apply_theme_on_create: bool = True,
    ) -> Any:
        """Switch to `target` (`page_id` or `page_id:sub_tab`), lazily building it and updating history."""
        route = NavRoute.parse(target)
        if route.page_id not in self._factories:
            raise KeyError(f"Unknown page id: {route.page_id!r}")

        prev_route = self._active_route
        prev_page_id = self._active_page_id
        if prev_page_id and prev_page_id in self._pages and prev_page_id != route.page_id:
            self._pages[prev_page_id].grid_remove()

        page = self.ensure_page(route.page_id, apply_theme_on_create=apply_theme_on_create)
        if prev_page_id != route.page_id:
            page.grid(row=0, column=0, sticky="nsew")
            self._breadcrumb_detail = ""
            self.clear_context_action()
        elif route.page_id == "download":
            self.clear_context_action()

        if route.sub_tab:
            select_tab = getattr(page, "select_tab", None)
            if callable(select_tab):
                select_tab(route.sub_tab)

        resolved_sub = route.sub_tab
        if resolved_sub is None:
            cur_sub_fn = getattr(page, "current_sub_tab", None)
            if callable(cur_sub_fn):
                resolved_sub = cur_sub_fn()
        effective_route = NavRoute(
            page_id=route.page_id,
            sub_tab=resolved_sub,
            service_key=route.service_key,
            profile=route.profile,
        )

        if record_history:
            self._push_history(prev_route, effective_route)

        self._active_page_id = route.page_id
        self._active_route = effective_route
        for pid, btn in self._nav_buttons.items():
            btn.configure(
                style="SidebarNavActive.TButton" if pid == route.page_id else "SidebarNav.TButton"
            )

        apply_ctx = getattr(page, "apply_route_context", None)
        if callable(apply_ctx):
            apply_ctx(effective_route)

        on_show = getattr(page, "on_show", None)
        if callable(on_show):
            on_show()
        self._refresh_nav_header()
        return page

    def set_download_badge(self, active_count: int, failed_count: int) -> None:
        """Update the queue/failure badge next to the Download nav button."""
        badge = self._badge_labels.get("download")
        if badge is None:
            return
        if active_count > 0 and failed_count > 0:
            badge_text = f"{active_count}q, {failed_count} err"
        elif active_count > 0:
            badge_text = f"{active_count} queued"
        elif failed_count > 0:
            badge_text = f"{failed_count} failed"
        else:
            badge_text = ""
        if badge_text:
            badge.configure(text=badge_text)
            badge.grid()
        else:
            badge.configure(text="")
            badge.grid_remove()

    def update_dpi_scaling(self) -> None:
        """Recompute DPI-dependent sidebar width, wraplengths, and fonts after a monitor DPI change."""
        sidebar_w = scale_px(self, self.SIDEBAR_WIDTH_DP)
        self.sidebar.configure(width=sidebar_w)
        wrap_w = max(scale_px(self, 120), sidebar_w - scale_px(self, 22))
        self._active_job_label.configure(wraplength=wrap_w)
        self._status_label.configure(wraplength=wrap_w)
        self._brand_title_label.configure(font=make_bold_font(self, 3))
        self.breadcrumb_label.configure(font=make_bold_font(self, 1))

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
