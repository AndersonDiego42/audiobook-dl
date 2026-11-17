"""Reusable Tkinter composite widgets and input helpers for the desktop GUI."""

from dataclasses import dataclass
import queue
import re
import sys
import threading
import tkinter as tk
from tkinter import ttk
from typing import Any
from collections.abc import Callable, Sequence

from audiobookdl.gui.formatting import (
    clamp_rect_to_monitor,
    format_geometry,
    get_monitor_work_areas,
)
from audiobookdl.gui.theme import (
    LIGHT_PALETTE,
    ThemePalette,
    apply_theme_to_window,
    resolve_palette,
    scale_px,
)


_SORT_CHUNK_RE = re.compile(r"(\d+)")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class TreeColumnSpec:
    """Declarative column definition for `ScrolledTreeview`."""

    key: str
    heading: str
    width: int
    stretch: bool = True
    anchor: str = "w"
    sortable: bool = True
    sort_kind: str = "text"  # "text" | "number" | "duration" | "date"


def _natural_text_sort_key(raw: str) -> tuple[Any, ...]:
    parts = _SORT_CHUNK_RE.split(raw.strip().casefold())
    key: list[Any] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            key.append((0, int(part), part))
        else:
            key.append((1, 0, part))
    return tuple(key)


def _column_sort_key(raw: Any, sort_kind: str) -> tuple[int, Any]:
    text = str(raw or "").strip()
    if not text or text in ("-", "--"):
        return (1, "")
    if sort_kind == "number":
        m = _NUMBER_RE.search(text.replace(",", ""))
        if m:
            return (0, float(m.group(0)))
        return (1, text.casefold())
    if sort_kind == "duration":
        parts = text.split(":")
        if len(parts) in (2, 3) and all(p.replace(".", "", 1).isdigit() for p in parts):
            total = 0.0
            for p in parts:
                total = total * 60.0 + float(p)
            return (0, total)
        return (1, text.casefold())
    if sort_kind == "date":
        return (0, text.casefold())
    return (0, _natural_text_sort_key(text))


import typing

_TreeItemT = typing.TypeVar("_TreeItemT")


def resolve_selected_leaf_iids(tree: ttk.Treeview) -> list[str]:
    """Return selected leaf item IDs, expanding any selected group parent rows."""
    seen: set[str] = set()
    leaves: list[str] = []

    def _collect(iid: str) -> None:
        children = tree.get_children(iid)
        if children:
            for child in children:
                _collect(child)
        elif iid not in seen:
            seen.add(iid)
            leaves.append(iid)

    for iid in tree.selection():
        _collect(iid)
    return leaves


def populate_grouped_tree(
    tree: ttk.Treeview,
    items: Sequence[_TreeItemT],
    *,
    grouped: bool,
    get_iid: typing.Callable[[_TreeItemT, int], str],
    get_values: typing.Callable[[_TreeItemT], tuple[str, ...]],
    get_group_key: typing.Callable[[_TreeItemT], str],
    format_group_values: typing.Callable[[str, int], tuple[str, ...]],
    should_group: typing.Callable[[_TreeItemT, int], bool] | None = None,
    group_prefix: str = "group:",
    open_groups: bool = True,
    get_tags: typing.Callable[[_TreeItemT], tuple[str, ...]] | None = None,
    get_image: typing.Callable[[_TreeItemT], Any] | None = None,
) -> int:
    """Populate `tree` flat or grouped under collapsible parent rows.

    Returns the number of parent group rows inserted.
    """
    existing = tree.get_children()
    if existing:
        tree.delete(*existing)

    def _insert_leaf(parent_iid: str, item: _TreeItemT, idx: int) -> None:
        tags = get_tags(item) if get_tags is not None else ()
        kwargs: dict[str, Any] = {
            "iid": get_iid(item, idx),
            "values": get_values(item),
            "tags": tags,
        }
        if get_image is not None:
            img = get_image(item)
            if img is not None:
                kwargs["image"] = img
        tree.insert(parent_iid, "end", **kwargs)

    if not grouped:
        for idx, item in enumerate(items):
            _insert_leaf("", item, idx)
        return 0

    group_counts: dict[str, int] = {}
    for item in items:
        key = get_group_key(item).strip()
        if key:
            group_counts[key] = group_counts.get(key, 0) + 1

    group_iids: dict[str, str] = {}
    group_counter = 0
    for idx, item in enumerate(items):
        key = get_group_key(item).strip()
        count = group_counts.get(key, 0) if key else 0
        use_group = bool(
            key
            and (
                should_group(item, count)
                if should_group is not None
                else count >= 2
            )
        )
        parent_iid = ""
        if use_group:
            parent_iid = group_iids.get(key, "")
            if not parent_iid:
                group_counter += 1
                parent_iid = f"{group_prefix}{group_counter}"
                group_iids[key] = parent_iid
                tree.insert(
                    "",
                    "end",
                    iid=parent_iid,
                    text="",
                    values=format_group_values(key, count),
                    open=open_groups,
                    tags=("group",),
                )
        _insert_leaf(parent_iid, item, idx)
    return group_counter


class ScrolledTreeview(ttk.Frame):
    """Frame containing a `ttk.Treeview`, vertical `ttk.Scrollbar`, column sorting, and empty overlay."""

    def __init__(
        self,
        parent: tk.Misc,
        columns: Sequence[TreeColumnSpec],
        *,
        selectmode: str = "browse",
        height: int | None = None,
        show_tree: bool = False,
        tree_heading: str = "",
        tree_width: int = 28,
        tree_style: str | None = None,
        enable_sorting: bool = True,
    ) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._columns = tuple(columns)
        self._col_specs = {spec.key: spec for spec in self._columns}
        self._col_index = {spec.key: idx for idx, spec in enumerate(self._columns)}
        self._sort_col: str | None = None
        self._sort_desc = False
        self._enable_sorting = enable_sorting

        tree_kwargs: dict[str, Any] = {
            "columns": tuple(spec.key for spec in columns),
            "show": "tree headings" if show_tree else "headings",
            "selectmode": selectmode,
        }
        if height is not None:
            tree_kwargs["height"] = height
        if tree_style:
            tree_kwargs["style"] = tree_style

        self.tree = ttk.Treeview(self, **tree_kwargs)
        if show_tree:
            self.tree.heading("#0", text=tree_heading)
            self.tree.column(
                "#0",
                width=scale_px(self, tree_width),
                minwidth=scale_px(self, 22),
                stretch=False,
            )
        for spec in columns:
            if enable_sorting and spec.sortable:

                def _sort_cb(k: str = spec.key) -> None:
                    self.sort_by_column(k)

                self.tree.heading(
                    spec.key,
                    text=spec.heading,
                    command=_sort_cb,
                )
            else:
                self.tree.heading(spec.key, text=spec.heading)
            self.tree.column(  # type: ignore[call-overload]
                spec.key,
                width=scale_px(self, spec.width),
                stretch=spec.stretch,
                anchor=spec.anchor,
            )

        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")

        self._empty_overlay: ttk.Frame | None = None
        self._empty_label: ttk.Label | None = None
        self._empty_btn: ttk.Button | None = None
        self._empty_cmd: Callable[[], None] | None = None

    def set_empty_state(
        self,
        visible: bool,
        message: str = "",
        *,
        action_text: str = "",
        action_command: Callable[[], None] | None = None,
    ) -> None:
        """Show or hide a centered empty-state message and optional CTA button over the table."""
        if not visible:
            if self._empty_overlay is not None:
                self._empty_overlay.place_forget()
            return
        if self._empty_overlay is None:
            self._empty_overlay = ttk.Frame(self.tree, padding=scale_px(self, 12))
            self._empty_label = ttk.Label(
                self._empty_overlay,
                text="",
                style="Muted.TLabel",
                justify="center",
            )
            self._empty_label.pack(pady=(0, 6))
            self._empty_btn = ttk.Button(
                self._empty_overlay,
                text="",
                command=self._invoke_empty_action,
            )
        assert self._empty_label is not None
        self._empty_label.configure(text=message)
        self._empty_cmd = action_command
        if self._empty_btn is not None:
            if action_text and action_command is not None:
                self._empty_btn.configure(text=action_text)
                self._empty_btn.pack()
            else:
                self._empty_btn.pack_forget()
        self._empty_overlay.place(relx=0.5, rely=0.52, anchor="center")

    def _invoke_empty_action(self) -> None:
        if self._empty_cmd is not None:
            self._empty_cmd()

    def clear_sort_indicator(self) -> None:
        """Reset column sort indicators without altering current row order."""
        self._sort_col = None
        self._sort_desc = False
        for spec in self._columns:
            self.tree.heading(spec.key, text=spec.heading)

    def sort_by_column(self, col_key: str, *, descending: bool | None = None) -> None:
        """Sort rows by `col_key`, preserving parent group hierarchy when present."""
        spec = self._col_specs.get(col_key)
        col_idx = self._col_index.get(col_key)
        if spec is None or col_idx is None or not spec.sortable:
            return
        if descending is None:
            descending = not self._sort_desc if self._sort_col == col_key else False
        self._sort_col = col_key
        self._sort_desc = descending

        for s in self._columns:
            suffix = ""
            if s.key == col_key:
                suffix = " \u25bc" if descending else " \u25b2"
            self.tree.heading(s.key, text=f"{s.heading}{suffix}")

        def _sort_children(parent_iid: str) -> None:
            children = list(self.tree.get_children(parent_iid))
            if not children:
                return
            for child in children:
                if self.tree.get_children(child):
                    _sort_children(child)
            scored: list[tuple[tuple[int, Any], str]] = []
            for iid in children:
                vals = self.tree.item(iid, "values")
                raw_val = vals[col_idx] if isinstance(vals, (tuple, list)) and col_idx < len(vals) else ""
                scored.append((_column_sort_key(raw_val, spec.sort_kind), iid))
            scored.sort(key=lambda pair: pair[0], reverse=descending)
            for pos, (_key, iid) in enumerate(scored):
                self.tree.move(iid, parent_iid, pos)

        _sort_children("")


class ScrollableFrame(ttk.Frame):
    """Vertically scrollable container that auto-hides its scrollbar when content fits."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        style: ttk.Style | None = None,
        content_padding: tuple[int, int] = (12, 8),
        inner_scrollables: Callable[[], Sequence[tk.Widget]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._inner_scrollables = inner_scrollables
        self.vscroll_visible = False
        self._last_canvas_size: tuple[int, int] | None = None
        self._last_content_req_size: tuple[int, int] | None = None

        canvas_bg = style.lookup("TFrame", "background") if style is not None else ""
        canvas_kwargs: dict[str, Any] = {"highlightthickness": 0, "borderwidth": 0}
        if canvas_bg:
            canvas_kwargs["background"] = canvas_bg
        self.canvas = tk.Canvas(self, **canvas_kwargs)
        self.canvas.grid(row=0, column=0, sticky="nsew")

        self.vscroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vscroll.set)

        self.content_frame = ttk.Frame(self.canvas, padding=content_padding)
        self.window_id = self.canvas.create_window((0, 0), window=self.content_frame, anchor="nw")

        self.content_frame.bind("<Configure>", self._on_content_configure)
        self.canvas.bind("<Configure>", self.sync_layout)

        toplevel = self.winfo_toplevel()
        toplevel.bind("<FocusIn>", self.ensure_focus_visible, add="+")
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            toplevel.bind(seq, self.on_mousewheel, add="+")

    def apply_theme(self, background: str) -> None:
        """Update the underlying canvas background to match the active theme."""
        if background:
            self.canvas.configure(background=background)

    def sync_layout(self, event: Any | None = None) -> None:
        """Synchronize embedded content dimensions and scrollbar visibility with canvas size."""
        if event is not None:
            canvas_w = event.width
            canvas_h = event.height
            self._last_canvas_size = (canvas_w, canvas_h)
        elif self._last_canvas_size is not None:
            canvas_w, canvas_h = self._last_canvas_size
        else:
            canvas_w = self.canvas.winfo_width()
            canvas_h = self.canvas.winfo_height()
        if canvas_w <= 1 or canvas_h <= 1:
            return
        req_w = self.content_frame.winfo_reqwidth()
        req_h = self.content_frame.winfo_reqheight()
        self._last_content_req_size = (req_w, req_h)
        target_w = max(canvas_w, req_w)
        target_h = max(canvas_h, req_h)
        self.canvas.itemconfigure(self.window_id, width=target_w, height=target_h)
        self.canvas.configure(scrollregion=(0, 0, target_w, target_h))
        needs_vscroll = req_h > canvas_h + 1
        if needs_vscroll and not self.vscroll_visible:
            self.vscroll.grid(row=0, column=1, sticky="ns")
            self.vscroll_visible = True
        elif not needs_vscroll and self.vscroll_visible:
            self.vscroll.grid_remove()
            self.vscroll_visible = False
            self.canvas.yview_moveto(0.0)

    def _on_content_configure(self, _event: tk.Event | None = None) -> None:
        req_size = (
            self.content_frame.winfo_reqwidth(),
            self.content_frame.winfo_reqheight(),
        )
        if req_size != self._last_content_req_size:
            self.sync_layout()

    @staticmethod
    def _is_descendant(widget: tk.Misc | None, ancestor: tk.Misc) -> bool:
        cur = widget
        while cur is not None:
            if cur is ancestor:
                return True
            cur = getattr(cur, "master", None)
        return False

    def ensure_focus_visible(self, event: tk.Event) -> None:
        """Scroll the viewport when a focused child widget sits outside the visible area."""
        if not self.winfo_exists() or not self.vscroll_visible:
            return
        widget = getattr(event, "widget", None)
        toplevel = self.winfo_toplevel()
        if widget is None or widget in (toplevel, self.canvas, self.content_frame):
            return
        if not self._is_descendant(widget, self.content_frame):
            return
        try:
            if not self.winfo_ismapped() or widget.winfo_toplevel() is not toplevel:
                return
            content_y = self.content_frame.winfo_rooty()
            widget_top = widget.winfo_rooty() - content_y
            widget_height = widget.winfo_height()
            widget_bottom = widget_top + widget_height
            content_h = self.content_frame.winfo_height()
            canvas_h = self.canvas.winfo_height()
        except tk.TclError:
            return
        if content_h <= canvas_h or canvas_h <= 1 or widget_height <= 1 or widget_height > canvas_h:
            return
        first, last = self.canvas.yview()
        view_top = first * content_h
        view_bottom = last * content_h
        if widget_top < view_top:
            self.canvas.yview_moveto(max(0.0, (widget_top - 8) / content_h))
        elif widget_bottom > view_bottom:
            self.canvas.yview_moveto(min(1.0, (widget_bottom + 8 - canvas_h) / content_h))

    @staticmethod
    def _matches_inner_scrollable(widget: tk.Misc, scrollable: tk.Misc) -> bool:
        cur: tk.Misc | None = widget
        while cur is not None:
            if cur is scrollable:
                return True
            if isinstance(cur, (ttk.Scrollbar, tk.Scrollbar)) and getattr(cur, "master", None) is getattr(
                scrollable, "master", None
            ):
                return True
            cur = getattr(cur, "master", None)
        return False

    def on_mousewheel(self, event: tk.Event) -> None:
        """Scroll this canvas when pointer is inside it and no inner scrollable handles the wheel."""
        if not self.winfo_exists():
            return
        try:
            if not self.winfo_ismapped():
                return
        except tk.TclError:
            return
        widget = getattr(event, "widget", None)
        delta = getattr(event, "delta", 0)
        event_num = getattr(event, "num", None)
        scroll_up = (delta > 0) if delta else (event_num == 4)
        if widget is not None:
            try:
                if widget.winfo_toplevel() is not self.winfo_toplevel():
                    return
            except tk.TclError:
                return
            if not self._is_descendant(widget, self):
                return
            if "popdown" in str(widget):
                return
            if self._inner_scrollables is not None:
                for scrollable in self._inner_scrollables():
                    if self._matches_inner_scrollable(widget, scrollable):
                        try:
                            lo, hi = scrollable.yview()  # type: ignore[attr-defined]
                            if (lo, hi) != (0.0, 1.0):
                                if (scroll_up and lo > 0.0) or (not scroll_up and hi < 1.0):
                                    return
                        except Exception:
                            return
        first, last = self.canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return
        if delta:
            step = -1 if delta > 0 else 1
            if sys.platform != "darwin" and abs(delta) >= 120:
                step = int(-delta / 120)
            self.canvas.yview_scroll(step, "units")
        elif event_num == 4:
            self.canvas.yview_scroll(-1, "units")
        elif event_num == 5:
            self.canvas.yview_scroll(1, "units")


def bind_text_tab_navigation(widget: tk.Text) -> None:
    """Bind `<Tab>` and `<Shift-Tab>` on a `tk.Text` to cycle widget focus."""

    def _focus_next(event: tk.Event) -> str:
        next_widget = event.widget.tk_focusNext()
        if next_widget is not None:
            next_widget.focus()
        return "break"

    def _focus_prev(event: tk.Event) -> str:
        prev_widget = event.widget.tk_focusPrev()
        if prev_widget is not None:
            prev_widget.focus()
        return "break"

    widget.bind("<Tab>", _focus_next)
    widget.bind("<Shift-Tab>", _focus_prev)


def attach_url_context_menu(
    text_widget: tk.Text,
    *,
    on_paste: Callable[[], Any],
    on_paste_and_queue: Callable[[], Any],
    on_clear: Callable[[], Any],
) -> tk.Menu:
    """Attach Cut/Copy/Paste/Paste & Queue/Select All/Clear context menu to a URL text box."""
    menu = tk.Menu(text_widget, tearoff=0)
    menu.add_command(label="Cut", command=lambda: text_widget.event_generate("<<Cut>>"))
    menu.add_command(label="Copy", command=lambda: text_widget.event_generate("<<Copy>>"))
    menu.add_command(label="Paste", command=on_paste)
    menu.add_command(label="Paste && Queue", command=on_paste_and_queue)
    menu.add_separator()
    menu.add_command(label="Select All", command=lambda: text_widget.tag_add(tk.SEL, "1.0", tk.END))
    menu.add_command(label="Clear", command=on_clear)

    def _show_menu(event: tk.Event) -> str:
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    text_widget.bind("<Button-3>", _show_menu)
    if sys.platform == "darwin":
        text_widget.bind("<Button-2>", _show_menu)
    return menu


class MainThreadCallbackQueue:
    """Thread-safe queue delivering background worker callbacks onto the Tk main thread."""

    def __init__(self, widget: tk.Misc, interval_ms: int = 25) -> None:
        self._widget = widget
        self._interval_ms = interval_ms
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._after_id: str | None = None
        self._destroyed = False
        self._active_workers = 0
        self._lock = threading.Lock()
        widget.bind("<Destroy>", self._on_destroy, add="+")

    def begin_task(self) -> None:
        """Register an active background task and start main-thread polling."""
        with self._lock:
            if self._destroyed:
                return
            self._active_workers += 1
        self._ensure_polling()

    def post(self, callback: Callable[[], None]) -> None:
        """Enqueue `callback` from any thread without touching Tcl/Tk."""
        self._queue.put(callback)

    def finish_worker(self) -> None:
        """Mark one background worker complete from any thread."""
        with self._lock:
            self._active_workers = max(0, self._active_workers - 1)

    def _ensure_polling(self) -> None:
        if self._destroyed or self._after_id is not None:
            return
        try:
            if self._widget.winfo_exists():
                self._after_id = self._widget.after(self._interval_ms, self._poll)
        except tk.TclError:
            self._after_id = None

    def _poll(self) -> None:
        self._after_id = None
        if self._destroyed:
            return
        try:
            if not self._widget.winfo_exists():
                self._destroyed = True
                return
        except tk.TclError:
            self._destroyed = True
            return

        while not self._destroyed:
            try:
                cb = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                if not self._widget.winfo_exists():
                    self._destroyed = True
                    break
            except tk.TclError:
                self._destroyed = True
                break
            cb()

        if self._destroyed:
            return
        with self._lock:
            keep_polling = self._active_workers > 0 or not self._queue.empty()
        if keep_polling:
            self._ensure_polling()

    def _on_destroy(self, event: tk.Event) -> None:
        if getattr(event, "widget", None) is not self._widget:
            return
        self._destroyed = True
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break


class Tooltip:
    """Theme-aware hover and focus tooltip for Tkinter/ttk widgets."""

    def __init__(
        self,
        widget: tk.Misc,
        text: str,
        *,
        delay_ms: int = 400,
        palette_provider: Callable[[], ThemePalette] | None = None,
    ) -> None:
        self._widget = widget
        self._text = text
        self._delay_ms = delay_ms
        self._palette_provider = palette_provider
        self._after_id: str | None = None
        self._tip_window: tk.Toplevel | None = None

        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._cancel_and_hide, add="+")
        widget.bind("<ButtonPress>", self._cancel_and_hide, add="+")
        widget.bind("<FocusIn>", self._schedule_focus, add="+")
        widget.bind("<FocusOut>", self._cancel_and_hide, add="+")
        widget.bind("<Destroy>", self._on_destroy, add="+")

    def set_text(self, text: str) -> None:
        self._text = text

    def _schedule(self, _event: tk.Event | None = None) -> None:
        self._cancel_timer()
        if not self._text:
            return
        try:
            if self._widget.winfo_exists():
                self._after_id = self._widget.after(self._delay_ms, self.show)
        except tk.TclError:
            self._after_id = None

    def _schedule_focus(self, _event: tk.Event | None = None) -> None:
        # Show briefly delayed on keyboard focus navigation only if mapped
        try:
            if self._widget.winfo_ismapped():
                self._schedule()
        except tk.TclError:
            pass

    def _cancel_timer(self) -> None:
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _cancel_and_hide(self, _event: tk.Event | None = None) -> None:
        self._cancel_timer()
        self.hide()

    def _on_destroy(self, event: tk.Event) -> None:
        if getattr(event, "widget", None) is self._widget:
            self._cancel_and_hide()

    def show(self) -> None:
        self._after_id = None
        if self._tip_window is not None or not self._text:
            return
        try:
            if not self._widget.winfo_exists() or not self._widget.winfo_ismapped():
                return
            x = self._widget.winfo_rootx() + scale_px(self._widget, 12)
            y = self._widget.winfo_rooty() + self._widget.winfo_height() + scale_px(self._widget, 4)
        except tk.TclError:
            return
        palette = self._palette_provider() if self._palette_provider is not None else resolve_palette("system")
        tip = tk.Toplevel(self._widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        frame = tk.Frame(
            tip,
            background=palette.surface,
            highlightbackground=palette.border,
            highlightthickness=1,
            padx=scale_px(self._widget, 8),
            pady=scale_px(self._widget, 4),
        )
        frame.pack(fill=tk.BOTH, expand=True)
        label = tk.Label(
            frame,
            text=self._text,
            justify="left",
            wraplength=scale_px(self._widget, 320),
            background=palette.surface,
            foreground=palette.fg,
        )
        label.pack()
        self._tip_window = tip

    def hide(self) -> None:
        tip = self._tip_window
        self._tip_window = None
        if tip is not None:
            try:
                tip.destroy()
            except tk.TclError:
                pass


def attach_tooltip(
    widget: tk.Misc,
    text: str,
    *,
    palette_provider: Callable[[], ThemePalette] | None = None,
) -> Tooltip | None:
    """Attach a `Tooltip` to `widget` if `text` is non-empty."""
    cleaned = text.strip()
    if not cleaned:
        return None
    return Tooltip(widget, cleaned, palette_provider=palette_provider)


class CollapsibleSection(ttk.Frame):
    """Collapsible container with a header toggle and compact summary badge when collapsed."""

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        *,
        collapsed: bool = False,
        on_toggle: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self._title = title
        self._collapsed = collapsed
        self._on_toggle = on_toggle

        self._header = ttk.Frame(self)
        self._header.grid(row=0, column=0, sticky="ew")
        self._header.columnconfigure(1, weight=1)

        self._toggle_btn = ttk.Button(
            self._header,
            text="",
            command=self.toggle,
        )
        self._toggle_btn.grid(row=0, column=0, sticky="w")

        self.summary_var = tk.StringVar(value="")
        self._summary_label = ttk.Label(
            self._header,
            textvariable=self.summary_var,
            style="Muted.TLabel",
        )
        self._summary_label.grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.content = ttk.Frame(self)
        self.content.columnconfigure(0, weight=1)
        self._apply_state(notify=False)

    @property
    def is_collapsed(self) -> bool:
        return self._collapsed

    def set_summary(self, summary: str) -> None:
        self.summary_var.set(summary)

    def toggle(self, collapsed: bool | None = None) -> bool:
        target = (not self._collapsed) if collapsed is None else bool(collapsed)
        if target != self._collapsed:
            self._collapsed = target
            self._apply_state(notify=True)
        return self._collapsed

    def _apply_state(self, *, notify: bool) -> None:
        prefix = "\u25b8" if self._collapsed else "\u25be"
        self._toggle_btn.configure(text=f"{prefix} {self._title}")
        if self._collapsed:
            self.content.grid_remove()
            self._summary_label.grid()
        else:
            self.content.grid(row=1, column=0, sticky="ew", pady=(2, 0))
            self._summary_label.grid_remove()
        if notify and self._on_toggle is not None:
            self._on_toggle(self._collapsed)


class ModalDialog(tk.Toplevel):
    """Base modal dialog with parent centering, monitor clamping, DPI sizing, Esc close, and focus restore."""

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        *,
        palette: ThemePalette | None = None,
        default_size: tuple[int, int] | None = None,
        min_size: tuple[int, int] | None = None,
        resizable: tuple[bool, bool] = (True, True),
    ) -> None:
        super().__init__(parent)
        self._palette = palette or LIGHT_PALETTE
        self._owner = parent.winfo_toplevel()
        self._default_size = default_size
        self._min_size = min_size
        self._opener_focus: tk.Misc | None = None
        try:
            self._opener_focus = self._owner.focus_get()
        except (tk.TclError, KeyError):
            self._opener_focus = None

        self.withdraw()
        self.title(title)
        self.transient(self._owner)  # type: ignore[call-overload]
        self.resizable(*resizable)
        if min_size is not None:
            self.minsize(scale_px(self, min_size[0]), scale_px(self, min_size[1]))
        self.bind("<Escape>", self._on_escape)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Map>", self._on_modal_map, add="+")
        self._finalized = False

    def _on_escape(self, _event: tk.Event | None = None) -> str:
        self.destroy()
        return "break"

    def _on_modal_map(self, _event: tk.Event | None = None) -> None:
        if not self._finalized:
            return
        try:
            if self.winfo_exists() and self.grab_current() is None:
                self.grab_set()
        except tk.TclError:
            pass

    def finalize_modal(
        self,
        *,
        initial_focus: tk.Misc | None = None,
        monitors: Sequence[tuple[int, int, int, int]] | None = None,
    ) -> None:
        """Apply theme, size and center on owner clamped to monitor work area, show, grab, and focus."""
        self._finalized = True
        apply_theme_to_window(self, self._palette)
        self.update_idletasks()
        min_w = scale_px(self, self._min_size[0]) if self._min_size else scale_px(self, 240)
        min_h = scale_px(self, self._min_size[1]) if self._min_size else scale_px(self, 160)
        if self._default_size is not None:
            req_w = max(min_w, scale_px(self, self._default_size[0]))
            req_h = max(min_h, scale_px(self, self._default_size[1]))
        else:
            req_w = max(min_w, int(self.winfo_reqwidth()))
            req_h = max(min_h, int(self.winfo_reqheight()))

        mon_areas = list(monitors) if monitors is not None else get_monitor_work_areas(self._owner)
        target_x: int | None = None
        target_y: int | None = None
        try:
            if self._owner.winfo_exists() and self._owner.winfo_ismapped():
                ow = int(self._owner.winfo_width())
                oh = int(self._owner.winfo_height())
                if ow > 1 and oh > 1:
                    ox = int(self._owner.winfo_rootx())
                    oy = int(self._owner.winfo_rooty())
                    target_x = ox + (ow - req_w) // 2
                    target_y = oy + (oh - req_h) // 2
        except tk.TclError:
            target_x = None
            target_y = None

        cw, ch, cx, cy = clamp_rect_to_monitor(
            req_w,
            req_h,
            target_x,
            target_y,
            mon_areas,
            min_width=min_w,
            min_height=min_h,
        )
        self.geometry(format_geometry(cw, ch, cx, cy))
        self.deiconify()
        self.lift()
        try:
            self.grab_set()
        except tk.TclError:
            pass
        focus_target = initial_focus if initial_focus is not None else self
        try:
            focus_target.focus_set()
        except tk.TclError:
            pass

    def destroy(self) -> None:
        opener = self._opener_focus
        self._opener_focus = None
        try:
            if self.grab_current() is self:
                self.grab_release()
        except tk.TclError:
            pass
        super().destroy()
        if opener is not None:
            try:
                if opener.winfo_exists():
                    opener.focus_set()
            except tk.TclError:
                pass
