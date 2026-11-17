"""Light/dark theme palettes, OS theme detection, and HiDPI scaling helpers for the GUI."""

import ctypes
from dataclasses import dataclass
import os
import subprocess
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk


THEME_MODES: tuple[str, ...] = ("system", "light", "dark")
THEME_LABELS: dict[str, str] = {
    "system": "System (Auto)",
    "light": "Light",
    "dark": "Dark",
}


@dataclass(frozen=True)
class ThemePalette:
    """Semantic color palette shared by ttk styles and classic Tk widgets."""

    mode: str  # "light" | "dark"
    bg: str
    surface: str
    surface_alt: str
    surface_hover: str
    fg: str
    fg_secondary: str
    fg_muted: str
    fg_disabled: str
    border: str
    accent: str
    accent_fg: str
    success: str
    warning: str
    error: str
    text_bg: str
    text_fg: str
    select_bg: str
    select_fg: str
    menu_bg: str
    menu_fg: str


LIGHT_PALETTE = ThemePalette(
    mode="light",
    bg="#f0f0f0",
    surface="#ffffff",
    surface_alt="#e5e5e5",
    surface_hover="#d8d8d8",
    fg="#1f1f1f",
    fg_secondary="#333333",
    fg_muted="#555555",
    fg_disabled="#888888",
    border="#c6c6c6",
    accent="#0078d4",
    accent_fg="#ffffff",
    success="#1f6f3d",
    warning="#9a6700",
    error="#b42318",
    text_bg="#ffffff",
    text_fg="#1f1f1f",
    select_bg="#0078d4",
    select_fg="#ffffff",
    menu_bg="#f0f0f0",
    menu_fg="#1f1f1f",
)

DARK_PALETTE = ThemePalette(
    mode="dark",
    bg="#1e1e1e",
    surface="#252526",
    surface_alt="#2d2d30",
    surface_hover="#3a3a3d",
    fg="#e8e8e8",
    fg_secondary="#cccccc",
    fg_muted="#9d9d9d",
    fg_disabled="#6e6e6e",
    border="#3e3e42",
    accent="#3b82f6",
    accent_fg="#ffffff",
    success="#4ade80",
    warning="#facc15",
    error="#f87171",
    text_bg="#18181b",
    text_fg="#e8e8e8",
    select_bg="#264f78",
    select_fg="#ffffff",
    menu_bg="#252526",
    menu_fg="#e8e8e8",
)


def normalize_theme_mode(raw: str | None) -> str:
    """Normalize a theme preference string to `'system'`, `'light'`, or `'dark'`."""
    if not raw:
        return "system"
    cleaned = raw.strip().lower()
    return cleaned if cleaned in THEME_MODES else "system"


def detect_system_theme() -> str:
    """Detect whether the host OS prefers `'dark'` or `'light'` appearance."""
    if sys.platform.startswith("win"):
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return "light" if int(value) else "dark"
        except Exception:
            return "light"

    if sys.platform == "darwin":
        try:
            res = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
            if res.returncode == 0 and "dark" in res.stdout.strip().lower():
                return "dark"
        except Exception:
            pass
        return "light"

    gtk_theme = os.environ.get("GTK_THEME", "").lower()
    if "dark" in gtk_theme:
        return "dark"
    try:
        res = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        if res.returncode == 0 and "dark" in res.stdout.strip().lower():
            return "dark"
    except Exception:
        pass
    return "light"


def resolve_palette(theme_mode: str | None) -> ThemePalette:
    """Resolve a theme mode (`'system'`, `'light'`, `'dark'`) to a concrete `ThemePalette`."""
    normalized = normalize_theme_mode(theme_mode)
    resolved = detect_system_theme() if normalized == "system" else normalized
    return DARK_PALETTE if resolved == "dark" else LIGHT_PALETTE


def enable_high_dpi_awareness() -> None:
    """Enable per-monitor DPI awareness on Windows when supported by the OS."""
    if not sys.platform.startswith("win"):
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (-4)
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # type: ignore[attr-defined]
        return
    except Exception:
        pass
    try:
        shcore = ctypes.windll.shcore  # type: ignore[attr-defined]
        shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
    except Exception:
        pass


def get_dpi_scale(widget: tk.Misc | None) -> float:
    """Return the display DPI scale factor relative to 96 DPI (snapped to 1.0 near 100%)."""
    if widget is None:
        return 1.0
    try:
        dpi = float(widget.winfo_fpixels("1i"))
    except (tk.TclError, ValueError, TypeError):
        return 1.0
    scale = dpi / 96.0
    if abs(scale - 1.0) < 0.05:
        return 1.0
    return max(1.0, min(4.0, scale))


def scale_px(widget: tk.Misc | None, px: int) -> int:
    """Scale a logical pixel dimension by the widget's DPI scale factor."""
    return max(1, round(px * get_dpi_scale(widget)))


def configure_hidpi_scaling(root: tk.Misc, style: ttk.Style) -> float:
    """Apply DPI-aware `Treeview` row height and return the active scale factor."""
    scale = get_dpi_scale(root)
    try:
        default_font = tkfont.nametofont("TkDefaultFont", root=root)
        line_h = int(default_font.metrics("linespace"))
    except (tk.TclError, ValueError, TypeError):
        line_h = round(16 * scale)
    row_height = max(scale_px(root, 22), line_h + scale_px(root, 6))
    style.configure("Treeview", rowheight=row_height)
    catalog_row_height = max(scale_px(root, 60), line_h + scale_px(root, 12))
    style.configure(
        "Catalog.Treeview",
        rowheight=catalog_row_height,
        indent=scale_px(root, 14),
    )
    return scale


def _apply_windows_titlebar_theme(window: tk.Misc, *, is_dark: bool) -> None:
    """Toggle dark title bar on Windows 10/11 when supported by DWM."""
    if not sys.platform.startswith("win"):
        return
    try:
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())  # type: ignore[attr-defined]
        if not hwnd:
            hwnd = window.winfo_id()
        value = ctypes.c_int(1 if is_dark else 0)
        dwmapi = ctypes.windll.dwmapi  # type: ignore[attr-defined]
        # DWMWA_USE_IMMUTABLE_DARK_MODE is 20 on Windows 11 / 20H1+, 19 on older Win10 builds
        for attr in (20, 19):
            res = dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                ctypes.c_uint(attr),
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
            if res == 0:
                break
    except Exception:
        pass


def default_font_family(widget: tk.Misc | None = None) -> str:
    """Return the platform's default UI font family from `TkDefaultFont`."""
    try:
        return str(tkfont.nametofont("TkDefaultFont", root=widget).cget("family"))
    except (tk.TclError, ValueError, TypeError):
        return "TkDefaultFont"


def make_bold_font(widget: tk.Misc | None, size: int) -> tuple[str, int, str]:
    """Return a portable `(family, size, 'bold')` font tuple based on `TkDefaultFont`."""
    return (default_font_family(widget), size, "bold")


def _configure_semantic_labels(style: ttk.Style, palette: ThemePalette) -> None:
    style.configure("Muted.TLabel", foreground=palette.fg_muted)
    style.configure("Secondary.TLabel", foreground=palette.fg_secondary)
    style.configure("Success.TLabel", foreground=palette.success)
    style.configure("Warning.TLabel", foreground=palette.warning)
    style.configure("Error.TLabel", foreground=palette.error)


def _configure_accent_button(style: ttk.Style, palette: ThemePalette) -> None:
    style.configure(
        "Accent.TButton",
        background=palette.accent,
        foreground=palette.select_fg,
        bordercolor=palette.accent,
        padding=(8, 3),
    )
    style.map(
        "Accent.TButton",
        background=[
            ("disabled", palette.bg),
            ("pressed", palette.accent),
            ("active", palette.accent),
        ],
        foreground=[("disabled", palette.fg_disabled)],
    )


def _configure_clam_palette(style: ttk.Style, palette: ThemePalette) -> None:
    style.configure(
        ".",
        background=palette.bg,
        foreground=palette.fg,
        troughcolor=palette.surface_alt,
        bordercolor=palette.border,
        darkcolor=palette.border,
        lightcolor=palette.surface_alt,
        focuscolor=palette.accent,
        selectbackground=palette.select_bg,
        selectforeground=palette.select_fg,
    )
    style.configure("TFrame", background=palette.bg)
    style.configure("TLabelframe", background=palette.bg, bordercolor=palette.border)
    style.configure("TLabelframe.Label", background=palette.bg, foreground=palette.fg)
    style.configure("TLabel", background=palette.bg, foreground=palette.fg)
    _configure_semantic_labels(style, palette)
    for label_style in (
        "Muted.TLabel",
        "Secondary.TLabel",
        "Success.TLabel",
        "Warning.TLabel",
        "Error.TLabel",
    ):
        style.configure(label_style, background=palette.bg)

    style.configure(
        "TButton",
        background=palette.surface_alt,
        foreground=palette.fg,
        bordercolor=palette.border,
        width=0,
        padding=(8, 3),
    )
    style.map(
        "TButton",
        background=[
            ("disabled", palette.bg),
            ("pressed", palette.surface_hover),
            ("active", palette.surface_hover),
        ],
        foreground=[("disabled", palette.fg_disabled)],
    )
    _configure_accent_button(style, palette)

    style.configure("Sidebar.TFrame", background=palette.surface)
    style.configure("Sidebar.TLabel", background=palette.surface, foreground=palette.fg)
    style.configure("SidebarMuted.TLabel", background=palette.surface, foreground=palette.fg_muted)
    style.configure(
        "SidebarBadge.TLabel",
        background=palette.surface_alt,
        foreground=palette.fg_secondary,
        padding=(6, 1),
    )
    style.configure(
        "SidebarNav.TButton",
        background=palette.surface,
        foreground=palette.fg,
        bordercolor=palette.surface,
        anchor="w",
        padding=(10, 7),
        relief="flat",
    )
    style.map(
        "SidebarNav.TButton",
        background=[
            ("pressed", palette.surface_hover),
            ("active", palette.surface_hover),
        ],
        foreground=[("disabled", palette.fg_disabled)],
    )
    style.configure(
        "SidebarNavActive.TButton",
        background=palette.select_bg,
        foreground=palette.select_fg,
        bordercolor=palette.select_bg,
        anchor="w",
        padding=(10, 7),
        relief="flat",
    )
    style.map(
        "SidebarNavActive.TButton",
        background=[
            ("pressed", palette.select_bg),
            ("active", palette.select_bg),
        ],
        foreground=[("disabled", palette.fg_disabled)],
    )
    style.configure("Banner.TFrame", background=palette.surface_alt, bordercolor=palette.warning)
    style.configure("Banner.TLabel", background=palette.surface_alt, foreground=palette.warning)
    style.configure(
        "BannerMuted.TLabel",
        background=palette.surface_alt,
        foreground=palette.fg_secondary,
    )
    style.configure(
        "Chip.TButton",
        background=palette.surface_alt,
        foreground=palette.fg_secondary,
        bordercolor=palette.border,
        padding=(8, 2),
    )
    style.map(
        "Chip.TButton",
        background=[("active", palette.surface_hover), ("pressed", palette.surface_hover)],
    )

    style.configure("TCheckbutton", background=palette.bg, foreground=palette.fg)
    style.map(
        "TCheckbutton",
        background=[("active", palette.bg)],
        foreground=[("disabled", palette.fg_disabled)],
        indicatorcolor=[
            ("selected", palette.accent),
            ("disabled", palette.border),
        ],
    )
    style.configure("TRadiobutton", background=palette.bg, foreground=palette.fg)
    style.map(
        "TRadiobutton",
        background=[("active", palette.bg)],
        foreground=[("disabled", palette.fg_disabled)],
    )

    style.configure(
        "TEntry",
        fieldbackground=palette.surface,
        foreground=palette.fg,
        insertcolor=palette.fg,
        bordercolor=palette.border,
    )
    style.map(
        "TEntry",
        fieldbackground=[("disabled", palette.bg), ("readonly", palette.surface_alt)],
        foreground=[("disabled", palette.fg_disabled)],
    )

    style.configure(
        "TCombobox",
        fieldbackground=palette.surface,
        background=palette.surface_alt,
        foreground=palette.fg,
        arrowcolor=palette.fg,
        bordercolor=palette.border,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", palette.surface), ("disabled", palette.bg)],
        foreground=[("readonly", palette.fg), ("disabled", palette.fg_disabled)],
        background=[("active", palette.surface_hover), ("disabled", palette.bg)],
        selectbackground=[("readonly", palette.surface)],
        selectforeground=[("readonly", palette.fg)],
    )

    style.configure(
        "TSpinbox",
        fieldbackground=palette.surface,
        background=palette.surface_alt,
        foreground=palette.fg,
        arrowcolor=palette.fg,
        insertcolor=palette.fg,
        bordercolor=palette.border,
    )
    style.map(
        "TSpinbox",
        fieldbackground=[("disabled", palette.bg)],
        foreground=[("disabled", palette.fg_disabled)],
    )

    style.configure("TNotebook", background=palette.bg, bordercolor=palette.border)
    style.configure(
        "TNotebook.Tab",
        background=palette.surface_alt,
        foreground=palette.fg_muted,
        bordercolor=palette.border,
        padding=(10, 4),
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", palette.bg), ("active", palette.surface_hover)],
        foreground=[("selected", palette.fg), ("active", palette.fg)],
    )

    style.configure(
        "TProgressbar",
        background=palette.accent,
        troughcolor=palette.surface_alt,
        bordercolor=palette.border,
    )
    style.configure(
        "TScrollbar",
        background=palette.surface_alt,
        troughcolor=palette.bg,
        bordercolor=palette.border,
        arrowcolor=palette.fg,
    )
    style.map(
        "TScrollbar",
        background=[("active", palette.surface_hover)],
    )

    for tree_style in ("Treeview", "Catalog.Treeview"):
        style.configure(
            tree_style,
            background=palette.surface,
            fieldbackground=palette.surface,
            foreground=palette.fg,
            bordercolor=palette.border,
        )
        style.map(
            tree_style,
            background=[("selected", palette.select_bg)],
            foreground=[("selected", palette.select_fg)],
        )
        style.configure(
            f"{tree_style}.Heading",
            background=palette.surface_alt,
            foreground=palette.fg,
            bordercolor=palette.border,
        )
        style.map(
            f"{tree_style}.Heading",
            background=[("active", palette.surface_hover)],
        )


def _configure_menu(menu: tk.Menu, palette: ThemePalette) -> None:
    try:
        if menu.tk.call("tk", "windowingsystem") == "aqua":
            return
        menu.configure(
            background=palette.menu_bg,
            foreground=palette.menu_fg,
            activebackground=palette.select_bg,
            activeforeground=palette.select_fg,
            disabledforeground=palette.fg_disabled,
        )
    except tk.TclError:
        pass


def apply_theme_to_widget_tree(widget: tk.Misc, palette: ThemePalette) -> None:
    """Recursively apply `palette` colors to non-ttk Tk widgets (`Toplevel`, `Text`, `Canvas`, `Menu`)."""
    if isinstance(widget, (tk.Tk, tk.Toplevel)):
        try:
            widget.configure(background=palette.bg)
        except tk.TclError:
            pass
    elif isinstance(widget, tk.Text):
        try:
            widget.configure(
                background=palette.text_bg,
                foreground=palette.text_fg,
                insertbackground=palette.text_fg,
                selectbackground=palette.select_bg,
                selectforeground=palette.select_fg,
                highlightbackground=palette.border,
                highlightcolor=palette.accent,
            )
        except tk.TclError:
            pass
    elif isinstance(widget, tk.Listbox):
        try:
            widget.configure(
                background=palette.surface,
                foreground=palette.fg,
                selectbackground=palette.select_bg,
                selectforeground=palette.select_fg,
                highlightbackground=palette.border,
                highlightcolor=palette.accent,
            )
        except tk.TclError:
            pass
    elif isinstance(widget, tk.Menu):
        _configure_menu(widget, palette)
    elif isinstance(widget, tk.Canvas):
        try:
            widget.configure(background=palette.bg)
        except tk.TclError:
            pass

    apply_hook = getattr(widget, "apply_theme", None)
    if callable(apply_hook) and not isinstance(widget, (tk.Tk, tk.Toplevel)):
        try:
            apply_hook(palette)
        except tk.TclError:
            pass

    for child in widget.winfo_children():
        apply_theme_to_widget_tree(child, palette)


def apply_theme_to_window(window: tk.Toplevel, palette: ThemePalette) -> None:
    """Apply `palette` to a `Toplevel` dialog, its classic Tk children, and Windows title bar."""
    if hasattr(window, "_palette"):
        window._palette = palette  # type: ignore[attr-defined]
    apply_theme_to_widget_tree(window, palette)
    _apply_windows_titlebar_theme(window, is_dark=(palette.mode == "dark"))


def apply_theme(root: tk.Misc, style: ttk.Style, theme_mode: str | None) -> ThemePalette:
    """Apply `theme_mode` (`'system'`, `'light'`, `'dark'`) to `style`, `root`, and all child widgets."""
    palette = resolve_palette(theme_mode)
    available = style.theme_names()
    if "clam" in available:
        style.theme_use("clam")
    _configure_clam_palette(style, palette)

    configure_hidpi_scaling(root, style)

    try:
        root.option_add("*TCombobox*Listbox.background", palette.surface)
        root.option_add("*TCombobox*Listbox.foreground", palette.fg)
        root.option_add("*TCombobox*Listbox.selectBackground", palette.select_bg)
        root.option_add("*TCombobox*Listbox.selectForeground", palette.select_fg)
    except tk.TclError:
        pass

    apply_theme_to_widget_tree(root, palette)
    _apply_windows_titlebar_theme(root, is_dark=(palette.mode == "dark"))
    return palette
