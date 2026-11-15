"""Modal dialog displaying audiobook metadata/cover or a selectable series checklist."""

import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable, List, Optional, Tuple, Union

from audiobookdl.gui.formatting import (
    describe_series_book,
    format_audiobook_preview,
    render_cover_thumbnail,
)
from audiobookdl.gui.widgets import ScrolledTreeview, TreeColumnSpec
from audiobookdl.sources.source import Source
from audiobookdl.utils.audiobook import Audiobook, BookId, Result, Series


class PreviewDialog(tk.Toplevel):
    """Modal `Toplevel` window hosting book metadata or a series selection checklist."""

    _SERIES_COLUMNS = (
        TreeColumnSpec("selected", "Download", width=72, stretch=False, anchor="center"),
        TreeColumnSpec("book", "Book", width=420, stretch=True, anchor="w"),
    )

    def __init__(
        self,
        parent: tk.Misc,
        url: str,
        source: Source,
        result: Result,
        *,
        is_running: bool,
        skip_downloaded: bool,
        series_book_exists: Callable[[Union[BookId[Any], Audiobook]], bool],
        on_confirm: Callable[[str, Tuple[Source, Result], Optional[List[int]]], None],
    ) -> None:
        super().__init__(parent)
        self._url = url
        self._source = source
        self._result = result
        self._is_running = is_running
        self._skip_downloaded = skip_downloaded
        self._series_book_exists = series_book_exists
        self._on_confirm = on_confirm

        self._cover_photo: Optional[Any] = None
        self._check_vars: List[tk.BooleanVar] = []
        self._filter_var = tk.StringVar()
        self._item_labels: List[str] = []
        self._last_clicked_index: Optional[int] = None
        self._tree: Optional[ttk.Treeview] = None

        self.title(f"Preview \u2014 {result.title}")
        self.transient(parent)  # type: ignore[arg-type]
        self.minsize(520, 360)
        self.bind("<Escape>", lambda _e: self.destroy())

        body = ttk.Frame(self, padding=14)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)

        if isinstance(result, Audiobook):
            self._build_audiobook_view(body, result)
        else:
            self._build_series_view(body, result)

    def _build_audiobook_view(self, body: ttk.Frame, audiobook: Audiobook) -> None:
        top = ttk.Frame(body)
        top.grid(row=0, column=0, sticky="nsew")
        top.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        cover_frame = ttk.Frame(top, width=190, height=190)
        cover_frame.grid(row=0, column=0, sticky="nw", padx=(0, 14))
        photo = render_cover_thumbnail(audiobook.cover, self, max_size=180)
        if photo is not None:
            self._cover_photo = photo
            ttk.Label(cover_frame, image=photo).pack()
        else:
            ttk.Label(
                cover_frame,
                text="No Cover",
                width=20,
                anchor="center",
                relief="groove",
                padding=24,
            ).pack()

        meta_frame = ttk.Frame(top)
        meta_frame.grid(row=0, column=1, sticky="nsew")
        meta_frame.columnconfigure(1, weight=1)
        rows = format_audiobook_preview(audiobook)
        for row_idx, (label, value) in enumerate(rows):
            ttk.Label(
                meta_frame,
                text=f"{label}:",
                font=("Segoe UI", 9, "bold"),
            ).grid(row=row_idx, column=0, sticky="nw", padx=(0, 8), pady=2)
            ttk.Label(
                meta_frame,
                text=value,
                wraplength=320,
                justify="left",
            ).grid(row=row_idx, column=1, sticky="nw", pady=2)

        if audiobook.metadata.description:
            desc_row = len(rows)
            ttk.Label(
                meta_frame,
                text="Description:",
                font=("Segoe UI", 9, "bold"),
            ).grid(row=desc_row, column=0, sticky="nw", padx=(0, 8), pady=(6, 2))
            desc_box = ScrolledText(meta_frame, height=4, width=40, wrap="word")
            desc_box.grid(row=desc_row, column=1, sticky="nsew", pady=(6, 2))
            desc_box.insert("1.0", audiobook.metadata.description.strip())
            desc_box.configure(state="disabled")
            meta_frame.rowconfigure(desc_row, weight=1)

        btn_bar = ttk.Frame(body)
        btn_bar.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        action_text = "Add to Queue" if self._is_running else "Download"

        def _confirm_book() -> None:
            self.destroy()
            self._on_confirm(self._url, (self._source, audiobook), None)

        ttk.Button(btn_bar, text=action_text, command=_confirm_book).pack(side=tk.RIGHT)
        ttk.Button(btn_bar, text="Close", command=self.destroy).pack(side=tk.RIGHT, padx=(0, 6))

    def _build_series_view(self, body: ttk.Frame, series: Series[Any]) -> None:
        body.rowconfigure(2, weight=1)
        header = ttk.Frame(body)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=series.title, font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        book_count = len(series.books)
        ttk.Label(
            header,
            text=f"{self._source.name}  \u2022  {book_count} book{'s' if book_count != 1 else ''}",
            foreground="#555555",
        ).grid(row=1, column=0, sticky="w")

        self._selected_count_var = tk.StringVar()
        self._check_vars = [tk.BooleanVar(value=True) for _ in series.books]

        toolbar = ttk.Frame(body)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(toolbar, text="Select All", command=lambda: self._set_all(True)).pack(
            side=tk.LEFT, padx=(0, 4)
        )
        ttk.Button(toolbar, text="Deselect All", command=lambda: self._set_all(False)).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Label(toolbar, text="Filter:").pack(side=tk.LEFT, padx=(0, 4))
        ttk.Entry(toolbar, textvariable=self._filter_var, width=22).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(toolbar, textvariable=self._selected_count_var, foreground="#333333").pack(
            side=tk.RIGHT
        )
        self._update_selected_count(book_count)

        list_container = ttk.Frame(body, relief="sunken", borderwidth=1)
        list_container.grid(row=2, column=0, sticky="nsew")
        list_container.columnconfigure(0, weight=1)
        list_container.rowconfigure(0, weight=1)

        if book_count == 0:
            ttk.Label(
                list_container,
                text="No downloadable books found in this series.",
                padding=20,
            ).grid(row=0, column=0)
        else:
            scrolled_tree = ScrolledTreeview(
                list_container,
                self._SERIES_COLUMNS,
                selectmode="browse",
                height=12,
            )
            scrolled_tree.grid(row=0, column=0, sticky="nsew")
            tree = scrolled_tree.tree
            self._tree = tree

            for idx, book in enumerate(series.books):
                label = f"{idx + 1}. {describe_series_book(idx, book)}"
                if self._skip_downloaded and self._series_book_exists(book):
                    label = f"{label} [Exists]"
                self._item_labels.append(label)
                tree.insert(
                    "",
                    tk.END,
                    iid=str(idx),
                    values=("[x]" if self._check_vars[idx].get() else "[ ]", label),
                )

            tree.bind("<Button-1>", self._on_tree_click)
            tree.bind("<space>", self._on_tree_key)
            tree.bind("<Return>", self._on_tree_key)
            self._filter_var.trace_add("write", self._apply_filter)

        btn_bar = ttk.Frame(body)
        btn_bar.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        action_text = "Queue Selected" if self._is_running else "Download Selected"

        def _confirm_series() -> None:
            indices = [i for i, v in enumerate(self._check_vars) if v.get()]
            if not indices:
                messagebox.showwarning(
                    "No Selection",
                    "Please select at least one book to download.",
                    parent=self,
                )
                return
            self.destroy()
            self._on_confirm(self._url, (self._source, series), indices)

        ttk.Button(
            btn_bar,
            text=action_text,
            command=_confirm_series,
            state="disabled" if book_count == 0 else "normal",
        ).pack(side=tk.RIGHT)
        ttk.Button(btn_bar, text="Close", command=self.destroy).pack(side=tk.RIGHT, padx=(0, 6))

    def _update_selected_count(self, book_count: Optional[int] = None) -> None:
        total = len(self._check_vars) if book_count is None else book_count
        chosen = sum(1 for v in self._check_vars if v.get())
        self._selected_count_var.set(f"Selected: {chosen} / {total}")

    def _sync_tree_marks(self) -> None:
        if self._tree is None:
            return
        for idx_item in range(len(self._item_labels)):
            self._tree.set(str(idx_item), "selected", "[x]" if self._check_vars[idx_item].get() else "[ ]")

    def _set_all(self, val: bool) -> None:
        query = self._filter_var.get().strip().lower()
        if query:
            for idx_item, label_item in enumerate(self._item_labels):
                if query in label_item.lower():
                    self._check_vars[idx_item].set(val)
        else:
            for v in self._check_vars:
                v.set(val)
        self._sync_tree_marks()
        self._update_selected_count()

    def _toggle_series_item(self, idx: int, shift_held: bool = False) -> None:
        if shift_held and self._last_clicked_index is not None:
            start, end = sorted((self._last_clicked_index, idx))
            target_val = self._check_vars[idx].get()
            query = self._filter_var.get().strip().lower()
            for pos in range(start, end + 1):
                if not query or query in self._item_labels[pos].lower():
                    self._check_vars[pos].set(target_val)
        self._last_clicked_index = idx
        self._sync_tree_marks()
        self._update_selected_count()

    def _on_tree_click(self, event: tk.Event) -> Optional[str]:
        if self._tree is None:
            return None
        row_iid = self._tree.identify_row(event.y)
        if not row_iid:
            return None
        idx_row = int(row_iid)
        self._tree.focus(row_iid)
        self._tree.selection_set(row_iid)
        self._check_vars[idx_row].set(not self._check_vars[idx_row].get())
        self._toggle_series_item(idx_row, bool(getattr(event, "state", 0) & 0x0001))
        return "break"

    def _on_tree_key(self, event: tk.Event) -> str:
        if self._tree is None:
            return "break"
        focused = self._tree.focus()
        if not focused:
            visible = self._tree.get_children()
            if not visible:
                return "break"
            focused = visible[0]
            self._tree.focus(focused)
        idx_row = int(focused)
        self._check_vars[idx_row].set(not self._check_vars[idx_row].get())
        self._toggle_series_item(idx_row, bool(getattr(event, "state", 0) & 0x0001))
        return "break"

    def _apply_filter(self, *_args: object) -> None:
        if self._tree is None:
            return
        query = self._filter_var.get().strip().lower()
        visible_pos = 0
        for idx_item, item_label in enumerate(self._item_labels):
            iid = str(idx_item)
            if not query or query in item_label.lower():
                self._tree.reattach(iid, "", visible_pos)
                visible_pos += 1
            else:
                self._tree.detach(iid)
