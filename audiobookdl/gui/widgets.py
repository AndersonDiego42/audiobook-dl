"""Reusable Tkinter composite widgets and input helpers for the desktop GUI."""

from dataclasses import dataclass
import sys
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from audiobookdl.gui.theme import scale_px


@dataclass(frozen=True)
class TreeColumnSpec:
    """Declarative column definition for `ScrolledTreeview`."""

    key: str
    heading: str
    width: int
    stretch: bool = True
    anchor: str = "w"


import typing

_TreeItemT = typing.TypeVar("_TreeItemT")


def resolve_selected_leaf_iids(tree: ttk.Treeview) -> typing.List[str]:
    """Return selected leaf item IDs, expanding any selected group parent rows."""
    seen: set[str] = set()
    leaves: typing.List[str] = []

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
    get_values: typing.Callable[[_TreeItemT], typing.Tuple[str, ...]],
    get_group_key: typing.Callable[[_TreeItemT], str],
    format_group_values: typing.Callable[[str, int], typing.Tuple[str, ...]],
    should_group: typing.Optional[typing.Callable[[_TreeItemT, int], bool]] = None,
    group_prefix: str = "group:",
    open_groups: bool = True,
    get_tags: typing.Optional[typing.Callable[[_TreeItemT], typing.Tuple[str, ...]]] = None,
) -> int:
    """Populate `tree` flat or grouped under collapsible parent rows.

    Returns the number of parent group rows inserted.
    """
    existing = tree.get_children()
    if existing:
        tree.delete(*existing)

    if not grouped:
        for idx, item in enumerate(items):
            tags = get_tags(item) if get_tags is not None else ()
            tree.insert("", "end", iid=get_iid(item, idx), values=get_values(item), tags=tags)
        return 0

    group_counts: Dict[str, int] = {}
    for item in items:
        key = get_group_key(item).strip()
        if key:
            group_counts[key] = group_counts.get(key, 0) + 1

    group_iids: Dict[str, str] = {}
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
        tags = get_tags(item) if get_tags is not None else ()
        tree.insert(parent_iid, "end", iid=get_iid(item, idx), values=get_values(item), tags=tags)
    return group_counter


class ScrolledTreeview(ttk.Frame):
    """Frame containing a `ttk.Treeview` paired with a vertical `ttk.Scrollbar`."""

    def __init__(
        self,
        parent: tk.Misc,
        columns: Sequence[TreeColumnSpec],
        *,
        selectmode: str = "browse",
        height: Optional[int] = None,
        show_tree: bool = False,
        tree_heading: str = "",
        tree_width: int = 28,
    ) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        tree_kwargs: Dict[str, Any] = {
            "columns": tuple(spec.key for spec in columns),
            "show": "tree headings" if show_tree else "headings",
            "selectmode": selectmode,
        }
        if height is not None:
            tree_kwargs["height"] = height

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
            self.tree.heading(spec.key, text=spec.heading)
            self.tree.column(
                spec.key,
                width=scale_px(self, spec.width),
                stretch=spec.stretch,
                anchor=spec.anchor,  # type: ignore[arg-type]
            )

        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")


class ScrollableFrame(ttk.Frame):
    """Vertically scrollable container that auto-hides its scrollbar when content fits."""

    def __init__(
        self,
        parent: tk.Misc,
        *,
        style: Optional[ttk.Style] = None,
        content_padding: Tuple[int, int] = (12, 8),
        inner_scrollables: Optional[Callable[[], Sequence[tk.Widget]]] = None,
    ) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self._inner_scrollables = inner_scrollables
        self.vscroll_visible = False
        self._last_canvas_size: Optional[Tuple[int, int]] = None
        self._last_content_req_size: Optional[Tuple[int, int]] = None

        canvas_bg = style.lookup("TFrame", "background") if style is not None else ""
        canvas_kwargs: Dict[str, Any] = {"highlightthickness": 0, "borderwidth": 0}
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
        toplevel.bind("<FocusIn>", self.ensure_focus_visible)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            toplevel.bind(seq, self.on_mousewheel)

    def apply_theme(self, background: str) -> None:
        """Update the underlying canvas background to match the active theme."""
        if background:
            self.canvas.configure(background=background)

    def sync_layout(self, event: Optional[Any] = None) -> None:
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

    def _on_content_configure(self, _event: Optional[tk.Event] = None) -> None:
        req_size = (
            self.content_frame.winfo_reqwidth(),
            self.content_frame.winfo_reqheight(),
        )
        if req_size != self._last_content_req_size:
            self.sync_layout()

    def ensure_focus_visible(self, event: tk.Event) -> None:
        """Scroll the viewport when a focused child widget sits outside the visible area."""
        if not self.vscroll_visible:
            return
        widget = getattr(event, "widget", None)
        toplevel = self.winfo_toplevel()
        if widget is None or widget in (toplevel, self.canvas, self.content_frame):
            return
        try:
            if widget.winfo_toplevel() is not toplevel:
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

    def on_mousewheel(self, event: tk.Event) -> None:
        """Scroll the main canvas unless an inner scrollable widget handles the wheel."""
        widget = getattr(event, "widget", None)
        if widget is not None:
            if "popdown" in str(widget):
                return
            if self._inner_scrollables is not None:
                for scrollable in self._inner_scrollables():
                    if widget is scrollable:
                        try:
                            if scrollable.yview() != (0.0, 1.0):  # type: ignore[attr-defined]
                                return
                        except Exception:
                            return
        first, last = self.canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return
        delta = getattr(event, "delta", 0)
        if delta:
            step = -1 if delta > 0 else 1
            if sys.platform != "darwin" and abs(delta) >= 120:
                step = int(-delta / 120)
            self.canvas.yview_scroll(step, "units")
        elif getattr(event, "num", None) == 4:
            self.canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
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
