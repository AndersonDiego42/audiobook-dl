"""Typed holder for the GUI's Tk option/status variables.

`OptionsCoordinator.init_variables()` populates every field exactly once on the
Tk main thread; `AudiobookDLApp.__getattr__` delegates ``app.<name>_var`` lookups
to this object so existing call sites keep working without dynamic ``setattr``
stamping on the app instance.
"""

from __future__ import annotations

from dataclasses import dataclass
import tkinter as tk


@dataclass(slots=True)
class OptionsViewModel:
    """Explicit, typed container for all GUI option and status Tk variables."""

    # -- Spec-driven option variables (GUI_OPTION_SPECS bindings) -----------
    combine_var: tk.BooleanVar
    remove_chars_var: tk.StringVar
    debug_var: tk.BooleanVar
    cover_only_var: tk.BooleanVar
    no_chapters_var: tk.BooleanVar
    split_chapters_var: tk.BooleanVar
    output_format_var: tk.StringVar
    media_type_var: tk.StringVar
    skip_downloaded_var: tk.BooleanVar
    skip_unavailable_var: tk.BooleanVar
    download_archive_var: tk.BooleanVar
    write_json_var: tk.BooleanVar
    write_cover_var: tk.BooleanVar
    write_opf_var: tk.BooleanVar
    write_abs_metadata_var: tk.BooleanVar
    upload_to_abs_var: tk.BooleanVar
    write_cue_var: tk.BooleanVar
    resume_var: tk.BooleanVar
    audio_bitrate_var: tk.StringVar
    sample_rate_var: tk.StringVar
    overwrite_var: tk.StringVar
    max_workers_var: tk.StringVar
    book_delay_var: tk.StringVar
    limit_rate_var: tk.StringVar
    post_exec_var: tk.StringVar
    loudnorm_var: tk.BooleanVar
    mono_var: tk.BooleanVar
    chapters_from_silence_var: tk.BooleanVar
    override_chapters_var: tk.BooleanVar
    chapter_offset_ms_var: tk.StringVar
    intro_chapter_mode_var: tk.StringVar
    square_cover_var: tk.BooleanVar
    max_cover_size_var: tk.StringVar
    proxy_var: tk.StringVar
    timeout_var: tk.StringVar
    max_retries_var: tk.StringVar
    impersonate_var: tk.StringVar
    decryption_key_var: tk.StringVar
    enrich_metadata_var: tk.BooleanVar
    enrich_providers_var: tk.StringVar
    enrich_interactive_var: tk.BooleanVar

    # -- Manually managed GUI variables --------------------------------------
    output_dir_var: tk.StringVar
    template_var: tk.StringVar
    template_preset_var: tk.StringVar
    template_preview_var: tk.StringVar
    template_standalone_preview_var: tk.StringVar
    service_label_var: tk.StringVar
    profile_var: tk.StringVar
    service_hint_var: tk.StringVar
    saved_badge_var: tk.StringVar
    cookie_file_var: tk.StringVar
    username_var: tk.StringVar
    password_var: tk.StringVar
    library_var: tk.StringVar
    show_password_var: tk.BooleanVar
    remember_login_var: tk.BooleanVar
    m4b_var: tk.BooleanVar
    notify_on_complete_var: tk.BooleanVar
    clipboard_monitor_var: tk.BooleanVar
    minimize_to_tray_var: tk.BooleanVar
    check_updates_var: tk.BooleanVar
    status_var: tk.StringVar
    active_job_var: tk.StringVar
    progress_var: tk.DoubleVar
    batch_var: tk.StringVar
    metrics_var: tk.StringVar
    ffmpeg_status_var: tk.StringVar
