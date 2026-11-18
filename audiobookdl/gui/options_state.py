"""GUI option variables, template previews, DownloadOptions assembly, and config reload."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import os
import shlex
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import TYPE_CHECKING, Any

from audiobookdl import output
from audiobookdl.config import (
    DEFAULT_PROFILE,
    VALID_AUTO_SYNC_MODES,
    normalize_profile_name,
    split_source_profile_key,
)
from audiobookdl.gui.formatting import (
    PRESET_LABEL_TO_TEMPLATE,
    SAMPLE_PREVIEW_METADATA,
    TEMPLATE_TO_PRESET_LABEL,
    default_output_dir,
    parse_url_text,
)
from audiobookdl.gui.window_manager import open_path_in_os
from audiobookdl.options_schema import (
    GUI_OPTION_SPECS,
    PERSISTED_GUI_OPTION_SPECS,
    DownloadOptions,
    build_default_options,
)
from audiobookdl.sources import ServiceInfo
from audiobookdl.utils.archive import resolve_archive_path
from audiobookdl.utils.audiobook import AudiobookMetadata
from audiobookdl.utils.enrichment import parse_provider_list

if TYPE_CHECKING:
    from audiobookdl.gui.auth_panel import ServiceAuthCoordinator
    from audiobookdl.gui.controller import GuiController
    from audiobookdl.gui.coordinators import AutomationCoordinator
    from audiobookdl.gui.pages import DownloadPage
    from audiobookdl.gui.shell import SidebarShell


SAMPLE_STANDALONE_METADATA = AudiobookMetadata(
    title="Project Hail Mary",
    authors=["Andy Weir"],
    source="audible",
)


class OptionsCoordinator:
    """Manages GUI option variables, output templates, option building, and config reload."""

    def __init__(
        self,
        root: tk.Tk,
        owner: Any,
        controller: GuiController,
        services: Sequence[ServiceInfo],
        *,
        on_log: Callable[[str], None],
    ) -> None:
        self._root = root
        self._owner = owner
        self._controller = controller
        self._services = list(services)
        self._on_log = on_log

        self.cached_proxy: str | None = None
        self.cached_timeout: float | None = None

        self._shell: SidebarShell | None = None
        self._auth: ServiceAuthCoordinator | None = None
        self._automation: AutomationCoordinator | None = None
        self._set_theme: Callable[[str], object] | None = None

    def bind_collaborators(
        self,
        *,
        shell: SidebarShell,
        auth: ServiceAuthCoordinator,
        automation: AutomationCoordinator,
        set_theme: Callable[[str], object],
    ) -> None:
        """Bind collaborators created after initial variable construction."""
        self._shell = shell
        self._auth = auth
        self._automation = automation
        self._set_theme = set_theme

    def init_variables(self) -> None:
        """Populate Tkinter option, auth, preference, and status variables on the app."""
        o = self._owner
        cfg = self._controller.config
        saved_output_dir = (cfg.gui.output_dir or "").strip()
        o.output_dir_var = tk.StringVar(value=saved_output_dir or default_output_dir())
        saved_template = (cfg.gui.output_template or "").strip() or "{title}"
        o.template_var = tk.StringVar(value=saved_template)
        o.template_preset_var = tk.StringVar(
            value=TEMPLATE_TO_PRESET_LABEL.get(saved_template, "Custom")
        )
        o.template_preview_var = tk.StringVar(value="")
        o.template_standalone_preview_var = tk.StringVar(value="")

        first_label = self._services[0].label if self._services else ""
        o.service_label_var = tk.StringVar(value=first_label)
        o.profile_var = tk.StringVar(value=DEFAULT_PROFILE)
        o.service_hint_var = tk.StringVar(value="")
        o.saved_badge_var = tk.StringVar(value="")

        o.cookie_file_var = tk.StringVar(value="")
        o.username_var = tk.StringVar(value="")
        o.password_var = tk.StringVar(value="")
        o.library_var = tk.StringVar(value="")
        o.show_password_var = tk.BooleanVar(value=False)
        o.remember_login_var = tk.BooleanVar(value=True)

        for spec in GUI_OPTION_SPECS:
            binding = spec.gui
            if binding is None:
                continue
            cfg_val = getattr(cfg, spec.config_key, None) if spec.config_key else None
            initial = binding.init_value(cfg_val)
            var: tk.Variable = (
                tk.BooleanVar(value=bool(initial))
                if binding.var_type == "bool"
                else tk.StringVar(value=str(initial))
            )
            setattr(o, binding.var_attr, var)
        if cfg.gui.use_download_archive is not None:
            o.download_archive_var.set(cfg.gui.use_download_archive)
        o.m4b_var = tk.BooleanVar(value=(o.output_format_var.get() == "m4b"))
        notify_default = True if cfg.gui.notify_on_complete is None else cfg.gui.notify_on_complete
        o.notify_on_complete_var = tk.BooleanVar(value=notify_default)

        o.clipboard_monitor_var = tk.BooleanVar(value=bool(cfg.gui.clipboard_monitor))
        o.minimize_to_tray_var = tk.BooleanVar(value=bool(cfg.gui.minimize_to_tray))
        o.check_updates_var = tk.BooleanVar(value=(cfg.check_updates is not False))
        o.status_var = tk.StringVar(value="Ready")
        o.active_job_var = tk.StringVar(value="Idle")
        o.progress_var = tk.DoubleVar(value=0.0)
        o.batch_var = tk.StringVar(value="")
        o.metrics_var = tk.StringVar(value="")
        o.ffmpeg_status_var = tk.StringVar(value="")
        self.sync_cached_network_prefs()
        o.proxy_var.trace_add("write", lambda *_args: self.sync_cached_network_prefs())
        o.timeout_var.trace_add("write", lambda *_args: self.sync_cached_network_prefs())

    def wire_preview_traces(self) -> None:
        """Attach traces that keep template previews and summary chips updated."""
        o = self._owner
        for var in (
            o.output_dir_var,
            o.template_var,
            o.output_format_var,
            o.media_type_var,
            o.combine_var,
            o.remove_chars_var,
            o.cover_only_var,
            o.split_chapters_var,
            o.intro_chapter_mode_var,
        ):
            var.trace_add("write", lambda *_args: self.update_template_preview())
        o.template_var.trace_add("write", lambda *_args: self.sync_preset_from_template())
        self.update_template_preview()

        for summary_var in (
            o.service_label_var,
            o.profile_var,
            o.saved_badge_var,
            o.output_format_var,
            o.audio_bitrate_var,
            o.sample_rate_var,
            o.combine_var,
            o.enrich_metadata_var,
            o.cover_only_var,
            o.template_var,
            o.template_preset_var,
        ):
            summary_var.trace_add("write", lambda *_args: self.update_download_summaries())

    def sync_cached_network_prefs(self) -> None:
        o = self._owner
        raw_proxy = o.proxy_var.get().strip()
        self.cached_proxy = raw_proxy or None
        raw_timeout = o.timeout_var.get().strip()
        try:
            parsed_timeout = float(raw_timeout) if raw_timeout else None
            self.cached_timeout = parsed_timeout if (parsed_timeout and parsed_timeout > 0) else None
        except ValueError:
            self.cached_timeout = None

    def on_m4b_toggled(self) -> None:
        o = self._owner
        if o.m4b_var.get():
            o.output_format_var.set("m4b")
            o.combine_var.set(True)
        elif o.output_format_var.get() == "m4b":
            o.output_format_var.set("Original")

    def on_format_selected(self, _event: tk.Event | None = None) -> None:
        o = self._owner
        is_m4b = o.output_format_var.get() == "m4b"
        o.m4b_var.set(is_m4b)
        if is_m4b:
            o.combine_var.set(True)

    def on_preset_selected(self, _event: tk.Event | None = None) -> None:
        o = self._owner
        chosen = o.template_preset_var.get()
        tpl = PRESET_LABEL_TO_TEMPLATE.get(chosen)
        if tpl:
            o.template_var.set(tpl)

    def sync_preset_from_template(self) -> None:
        o = self._owner
        tpl = o.template_var.get().strip()
        target_label = TEMPLATE_TO_PRESET_LABEL.get(tpl, "Custom")
        if o.template_preset_var.get() != target_label:
            o.template_preset_var.set(target_label)

    def update_template_preview(self) -> None:
        o = self._owner
        folder = o.output_dir_var.get().strip() or default_output_dir()
        tpl = o.template_var.get().strip() or "{title}"
        full_tpl = os.path.join(folder, tpl)
        fmt = o.output_format_var.get().strip()
        if fmt in ("", "Original"):
            media = o.media_type_var.get().strip().lower()
            ext = "mp4" if media == "video" else "m4b"
        else:
            ext = fmt
        remove_chars = o.remove_chars_var.get()
        try:
            rendered_series = output.gen_output_location(
                full_tpl, SAMPLE_PREVIEW_METADATA, remove_chars
            )
            rendered_standalone = output.gen_output_location(
                full_tpl, SAMPLE_STANDALONE_METADATA, remove_chars
            )
            if o.cover_only_var.get():
                suffix = ".jpg"
            elif o.split_chapters_var.get() or not o.combine_var.get():
                intro_val = o.intro_chapter_mode_var.get().strip().lower()
                first_num = "00" if intro_val.startswith("always") else "01"
                suffix = f"/{first_num}.{ext}"
            else:
                suffix = f".{ext}"
            o.template_preview_var.set(f"{rendered_series}{suffix}")
            o.template_standalone_preview_var.set(f"{rendered_standalone}{suffix}")
        except Exception as exc:
            o.template_preview_var.set(f"(invalid template: {exc})")
            o.template_standalone_preview_var.set("")

    def update_download_summaries(self, page: DownloadPage | None = None) -> None:
        o = self._owner
        if self._shell is not None and self._shell.active_page_id == "accounts":
            svc_lbl = o.service_label_var.get().strip()
            prof_lbl = normalize_profile_name(o.profile_var.get())
            if svc_lbl:
                self._shell.set_breadcrumb_detail(f"{svc_lbl} ({prof_lbl})")
        dl_page = (
            page
            if page is not None
            else (self._shell.get_page("download") if self._shell is not None else None)
        )
        if dl_page is None:
            return
        dl_page.update_account_chip(
            o.service_label_var.get(),
            o.profile_var.get() or DEFAULT_PROFILE,
            o.saved_badge_var.get(),
        )
        preset = o.template_preset_var.get() or "Custom"
        tpl = o.template_var.get().strip() or "{title}"
        dl_page.naming_section.set_summary(f"{preset} ({tpl})")

        fmt = o.output_format_var.get() or "Original"
        flags: list[str] = []
        if o.combine_var.get():
            flags.append("Combine")
        bitrate_val = o.audio_bitrate_var.get().strip()
        if bitrate_val and bitrate_val.lower() != "auto":
            flags.append(bitrate_val)
        sr_val = o.sample_rate_var.get().strip()
        if sr_val and sr_val.lower() not in ("auto", "original"):
            flags.append(f"{sr_val} Hz" if sr_val.isdigit() else sr_val)
        if o.enrich_metadata_var.get():
            flags.append("Enrich")
        if o.cover_only_var.get():
            flags.append("Cover only")
        suffix = f" ({', '.join(flags)})" if flags else ""
        dl_page.options_section.set_summary(f"Format: {fmt}{suffix}")

    def read_gui_option_value(self, spec: Any, custom_providers: Mapping[str, Any]) -> Any:
        raw_val = getattr(self._owner, spec.gui.var_attr).get()
        if spec.name == "enrich_providers":
            cleaned = str(raw_val).strip()
            if not cleaned:
                return None
            return ",".join(parse_provider_list(cleaned, custom_providers=custom_providers))
        return spec.gui.read_option(raw_val)

    def build_options(self, urls_override: Sequence[str] | None = None) -> DownloadOptions:
        """Build `DownloadOptions` from current UI field values."""
        o = self._owner
        output_dir = o.output_dir_var.get().strip() or default_output_dir()
        template = o.template_var.get().strip() or "{title}"
        output_template = os.path.join(output_dir, template)
        custom_providers = self._controller.config_snapshot().custom_metadata_providers
        option_values = {
            spec.name: self.read_gui_option_value(spec, custom_providers)
            for spec in GUI_OPTION_SPECS
            if spec.gui is not None
        }
        use_download_archive = bool(option_values.pop("download_archive", False))
        archive_path = resolve_archive_path(self._controller.config_snapshot())
        download_archive = archive_path if use_download_archive else None

        if urls_override is not None:
            urls = list(urls_override)
        else:
            dl_page = self._shell.get_page("download") if self._shell is not None else None
            urls = (
                parse_url_text(dl_page.urls_text.get("1.0", tk.END))
                if dl_page is not None
                else []
            )
        assert self._auth is not None
        auth_kwargs = self._auth.extract_options_auth(urls)
        config_loc = self._controller.config_location
        effective_config_location = (
            config_loc if (config_loc and os.path.exists(config_loc)) else None
        )

        return build_default_options(
            urls=urls,
            output_template=output_template,
            download_archive=download_archive,
            config_location=effective_config_location,
            **auth_kwargs,
            **option_values,
        )

    def try_build_options(
        self,
        urls_override: Sequence[str] | None = None,
        parent: tk.Misc | None = None,
    ) -> DownloadOptions | None:
        """Build `DownloadOptions`, displaying a validation dialog on invalid numeric/text input."""
        try:
            return self.build_options(urls_override=urls_override)
        except ValueError as exc:
            self._owner.status_var.set("Invalid option value")
            messagebox.showerror(
                "Invalid Option",
                str(exc),
                parent=parent or self._root,
            )
            return None

    def read_persisted_gui_preferences(self) -> dict[str, Any]:
        """Read valid persisted GUI toggle/option values without failing on transient invalid input."""
        prefs: dict[str, Any] = {}
        custom_providers = self._controller.config_snapshot().custom_metadata_providers
        for spec in PERSISTED_GUI_OPTION_SPECS:
            binding = spec.gui
            if binding is None:
                continue
            try:
                prefs[spec.name] = self.read_gui_option_value(spec, custom_providers)
            except ValueError:
                continue
        return prefs

    def save_run_preferences(self, options: DownloadOptions) -> None:
        o = self._owner
        assert self._auth is not None
        if o.remember_login_var.get():
            self._auth.save_current_credentials(quiet=True)
        self._controller.save_run_preferences(
            options,
            output_dir=o.output_dir_var.get().strip() or default_output_dir(),
            output_template=o.template_var.get().strip() or "{title}",
            use_download_archive=o.download_archive_var.get(),
        )

    def sync_custom_providers_ui(self, *, download_page: DownloadPage | None = None) -> None:
        providers = self._controller.config_snapshot().custom_metadata_providers
        names = [name for name, prov in providers.items() if prov.enabled]
        dl_page = (
            download_page
            if download_page is not None
            else (self._shell.get_page("download") if self._shell is not None else None)
        )
        if dl_page is not None:
            dl_page.options_panel.update_custom_providers_hint(names)

    def browse_output_dir(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self._root,
            title="Select output folder",
            initialdir=self._owner.output_dir_var.get() or default_output_dir(),
        )
        if chosen:
            self._owner.output_dir_var.set(chosen)

    def open_output_folder(self) -> None:
        folder = self._owner.output_dir_var.get().strip() or default_output_dir()
        if not os.path.isdir(folder):
            messagebox.showinfo(
                "Folder Missing", f"Folder does not exist yet:\n{folder}", parent=self._root
            )
            return
        open_path_in_os(self._root, folder, reveal_in_folder=False)

    def reload_config(self) -> None:
        o = self._owner
        assert self._automation is not None
        assert self._auth is not None
        assert self._shell is not None
        assert self._set_theme is not None
        try:
            previous_post_exec = o.post_exec_var.get().strip()
            cfg = self._controller.reload_config()
            if (cfg.gui.output_dir or "").strip():
                o.output_dir_var.set((cfg.gui.output_dir or "").strip())
            if (cfg.gui.output_template or "").strip():
                o.template_var.set((cfg.gui.output_template or "").strip())
            for spec in GUI_OPTION_SPECS:
                binding = spec.gui
                if binding is None or not spec.config_key:
                    continue
                cfg_val = getattr(cfg, spec.config_key, None)
                getattr(o, binding.var_attr).set(binding.init_value(cfg_val))
            raw_post_exec = cfg.post_exec
            new_post_exec = (
                shlex.join(raw_post_exec)
                if isinstance(raw_post_exec, list)
                else (raw_post_exec or "").strip()
            )
            if new_post_exec and new_post_exec != previous_post_exec:
                approved = messagebox.askyesno(
                    "Confirm Post-Exec Command",
                    "The reloaded configuration defines a shell post-exec command:\n\n"
                    f"{new_post_exec}\n\n"
                    "This command will run via the system shell after each completed audiobook. "
                    "Keep this command enabled?",
                    parent=self._root,
                )
                if not approved:
                    o.post_exec_var.set("")
                    self._on_log("[WARN] Disabled post_exec command from reloaded configuration.")
            if cfg.gui.use_download_archive is not None:
                o.download_archive_var.set(cfg.gui.use_download_archive)
            if cfg.gui.notify_on_complete is not None:
                o.notify_on_complete_var.set(cfg.gui.notify_on_complete)
            if cfg.gui.clipboard_monitor is not None:
                o.clipboard_monitor_var.set(cfg.gui.clipboard_monitor)
                self._automation.apply_clipboard_monitor(quiet=True, persist=False)
            if cfg.gui.minimize_to_tray is not None:
                o.minimize_to_tray_var.set(cfg.gui.minimize_to_tray)
            o.check_updates_var.set(cfg.check_updates is not False)
            if cfg.gui.auto_sync_enabled is not None:
                self._automation.auto_sync_enabled = cfg.gui.auto_sync_enabled
            if cfg.gui.auto_sync_minutes is not None:
                self._automation.auto_sync_minutes = max(1, cfg.gui.auto_sync_minutes)
            if cfg.gui.auto_sync_mode in VALID_AUTO_SYNC_MODES:
                self._automation.auto_sync_mode = cfg.gui.auto_sync_mode
            if cfg.gui.auto_sync_targets is not None:
                self._automation.auto_sync_targets = [
                    split_source_profile_key(t) for t in cfg.gui.auto_sync_targets
                ]
            self._automation.configure_auto_sync_timer(
                self._automation.auto_sync_enabled,
                self._automation.auto_sync_minutes,
            )
            lib_page = self._shell.get_page("library")
            if lib_page is not None:
                lib_page.sync_tab.apply_saved_schedule(
                    enabled=self._automation.auto_sync_enabled,
                    minutes=self._automation.auto_sync_minutes,
                    mode=self._automation.auto_sync_mode,
                    targets=self._automation.auto_sync_targets or None,
                )
            self._set_theme(cfg.gui.theme or "system")
            o.m4b_var.set(o.output_format_var.get() == "m4b")
            self._auth.load_service_into_form()
            self._auth.update_field_states()
            self.sync_custom_providers_ui()
            settings_page = self._shell.get_page("settings")
            if settings_page is not None:
                settings_page.on_show()
            o.status_var.set("Config reloaded")
            self._on_log("Reloaded configuration.")
        except Exception as exc:
            messagebox.showerror("Config Error", str(exc), parent=self._root)
