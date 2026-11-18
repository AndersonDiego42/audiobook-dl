"""UI-free DRM / PSSH inspection helpers for local files, byte buffers, and URLs."""

from __future__ import annotations

import os
from typing import Any, Literal
from urllib.parse import urljoin, urlparse
from uuid import UUID

from attrs import frozen
import m3u8
import requests

from audiobookdl.exceptions import (
    CloudflareBlocked,
    GenericAudiobookDLException,
    RequestError,
    UserNotAuthorized,
)
from audiobookdl.utils.http import (
    DEFAULT_HTTP_TIMEOUT,
    create_configured_http_session,
    is_cloudflare_challenge,
    is_transient_network_error,
)
from audiobookdl.utils.pssh import (
    CLEARKEY_SYSTEM_ID,
    FAIRPLAY_SYSTEM_ID,
    PLAYREADY_SYSTEM_ID,
    WIDEVINE_SYSTEM_ID,
    PsshBox,
    PsshParseError,
    extract_cenc_kids_safe,
    parse_hls_key_data,
    parse_mpd_content_protection,
    parse_pssh_boxes,
    parse_tenc_default_kids,
    read_mp4_init_boxes,
)

DEFAULT_INSPECT_MAX_BYTES = 8 * 1024 * 1024
_ISOBMFF_BOX_TYPES = frozenset(
    {b"ftyp", b"styp", b"moov", b"moof", b"pssh", b"sidx", b"free", b"skip", b"wide"}
)
_MP4_PROTECTION_SCHEME = "urn:mpeg:dash:mp4protection:2011"
_HLS_KEYFORMAT_NAMES: dict[str, str] = {
    f"urn:uuid:{WIDEVINE_SYSTEM_ID}": "Widevine",
    f"urn:uuid:{PLAYREADY_SYSTEM_ID}": "PlayReady",
    "com.microsoft.playready": "PlayReady",
    f"urn:uuid:{CLEARKEY_SYSTEM_ID}": "ClearKey",
    f"urn:uuid:{FAIRPLAY_SYSTEM_ID}": "FairPlay",
    "com.apple.streamingkeydelivery": "FairPlay",
}


@frozen
class ProtectionReport:
    """Summary of DRM systems, KIDs, and PSSH boxes discovered in an asset or manifest."""

    source_label: str
    container: Literal["isobmff", "hls", "dash", "unknown"]
    systems: tuple[str, ...]
    key_ids: tuple[bytes, ...]
    pssh_boxes: tuple[PsshBox, ...]
    encrypted: bool


def _looks_like_isobmff(data: bytes) -> bool:
    return len(data) >= 8 and data[4:8] in _ISOBMFF_BOX_TYPES


def _decode_text_prefix(data: bytes, limit: int = 2048) -> str:
    return data[:limit].decode("utf-8-sig", errors="replace").lstrip()


def _extract_m3u8_keyid(key_obj: Any) -> tuple[bytes, ...]:
    raw_kid = getattr(key_obj, "keyid", None)
    if not raw_kid:
        extra = getattr(key_obj, "_extra_params", None)
        if isinstance(extra, dict):
            raw_kid = extra.get("keyid")
    if not isinstance(raw_kid, str):
        return ()
    cleaned = raw_kid.strip().lower().removeprefix("0x").replace("-", "")
    if len(cleaned) != 32:
        return ()
    try:
        return (bytes.fromhex(cleaned),)
    except ValueError:
        return ()


def _inspect_hls_playlist(playlist: m3u8.M3U8, *, source_label: str) -> ProtectionReport:
    systems: list[str] = []
    kids: list[bytes] = []
    boxes: list[PsshBox] = []
    encrypted = False

    candidates: list[Any] = []
    candidates.extend(getattr(playlist, "session_keys", None) or ())
    candidates.extend(getattr(playlist, "keys", None) or ())
    for seg in getattr(playlist, "segments", None) or ():
        if getattr(seg, "key", None) is not None:
            candidates.append(seg.key)

    seen_keys: set[tuple[str, str, str, str]] = set()
    for key_obj in candidates:
        if key_obj is None:
            continue
        method = str(getattr(key_obj, "method", "") or "").strip().upper()
        if not method or method == "NONE":
            continue
        encrypted = True
        key_format = str(getattr(key_obj, "keyformat", None) or "identity").strip()
        fmt_lower = key_format.lower()
        key_uri = str(getattr(key_obj, "absolute_uri", None) or getattr(key_obj, "uri", None) or "").strip()
        keyid_raw = str(
            getattr(key_obj, "keyid", None)
            or (getattr(key_obj, "_extra_params", None) or {}).get("keyid", "")
        )
        dedupe_sig = (method, fmt_lower, key_uri, keyid_raw)
        if dedupe_sig in seen_keys:
            continue
        seen_keys.add(dedupe_sig)

        if key_uri.lower().startswith("data:"):
            try:
                parsed_boxes = parse_hls_key_data(key_uri, key_format=fmt_lower)
            except PsshParseError:
                parsed_boxes = []
            for box in parsed_boxes:
                boxes.append(box)
                systems.append(box.system_name)
                kids.extend(box.key_ids)

        if fmt_lower in _HLS_KEYFORMAT_NAMES:
            systems.append(_HLS_KEYFORMAT_NAMES[fmt_lower])
        elif fmt_lower == "identity":
            systems.append(f"HLS {method}")
        else:
            systems.append(key_format)

        kids.extend(_extract_m3u8_keyid(key_obj))

    return ProtectionReport(
        source_label=source_label,
        container="hls",
        systems=tuple(dict.fromkeys(systems)),
        key_ids=tuple(dict.fromkeys(kids)),
        pssh_boxes=tuple(boxes),
        encrypted=encrypted,
    )


def inspect_bytes(data: bytes, *, source_label: str = "<bytes>") -> ProtectionReport:
    """Sniff container format in `data` (ISO BMFF, HLS M3U8, or DASH MPD) and return a `ProtectionReport`."""
    if not data:
        return ProtectionReport(
            source_label=source_label,
            container="unknown",
            systems=(),
            key_ids=(),
            pssh_boxes=(),
            encrypted=False,
        )

    if _looks_like_isobmff(data):
        boxes = parse_pssh_boxes(data)
        tenc_kids = parse_tenc_default_kids(data)
        kids: list[bytes] = []
        for box in boxes:
            kids.extend(box.key_ids)
        kids.extend(tenc_kids)
        systems = tuple(dict.fromkeys(box.system_name for box in boxes))
        unique_kids = tuple(dict.fromkeys(kids))
        return ProtectionReport(
            source_label=source_label,
            container="isobmff",
            systems=systems,
            key_ids=unique_kids,
            pssh_boxes=tuple(boxes),
            encrypted=bool(boxes or unique_kids),
        )

    text_head = _decode_text_prefix(data)
    if text_head.startswith("#EXTM3U"):
        base_uri = source_label if "://" in source_label else ""
        playlist = m3u8.loads(data.decode("utf-8-sig", errors="replace"), uri=base_uri)
        return _inspect_hls_playlist(playlist, source_label=source_label)

    if text_head.startswith("<MPD") or (text_head.startswith("<?xml") and "<MPD" in text_head):
        protections = parse_mpd_content_protection(data)
        boxes_list: list[PsshBox] = []
        kids_list: list[bytes] = []
        systems_list: list[str] = []
        for cp in protections:
            if cp.default_kid is not None:
                kids_list.append(cp.default_kid)
            for box in cp.pssh_boxes:
                boxes_list.append(box)
                systems_list.append(box.system_name)
                kids_list.extend(box.key_ids)
            if cp.scheme_id.lower() != _MP4_PROTECTION_SCHEME and cp.system_name:
                systems_list.append(cp.system_name)
        if not systems_list and protections:
            systems_list.append("Common Encryption (CENC)")
        return ProtectionReport(
            source_label=source_label,
            container="dash",
            systems=tuple(dict.fromkeys(systems_list)),
            key_ids=tuple(dict.fromkeys(kids_list)),
            pssh_boxes=tuple(boxes_list),
            encrypted=bool(protections or boxes_list or kids_list),
        )

    return ProtectionReport(
        source_label=source_label,
        container="unknown",
        systems=(),
        key_ids=(),
        pssh_boxes=(),
        encrypted=False,
    )


def inspect_file(path: str, *, max_bytes: int = DEFAULT_INSPECT_MAX_BYTES) -> ProtectionReport:
    """Inspect a local MP4/fMP4, HLS `.m3u8`, or DASH `.mpd` file for PSSH/DRM metadata."""
    if not os.path.isfile(path):
        raise GenericAudiobookDLException(f"File not found: {path}")
    with open(path, "rb") as src:
        head = src.read(min(2048, max_bytes))
    if not head:
        return inspect_bytes(b"", source_label=path)

    text_head = _decode_text_prefix(head)
    if text_head.startswith("#EXTM3U") or text_head.startswith("<MPD") or (
        text_head.startswith("<?xml") and "<MPD" in text_head
    ):
        with open(path, "rb") as src:
            raw = src.read(max_bytes)
        return inspect_bytes(raw, source_label=path)

    if _looks_like_isobmff(head):
        init_boxes = read_mp4_init_boxes(path)
        if init_boxes:
            return inspect_bytes(init_boxes, source_label=path)
        with open(path, "rb") as src:
            raw = src.read(max_bytes)
        return inspect_bytes(raw, source_label=path)

    return ProtectionReport(
        source_label=path,
        container="unknown",
        systems=(),
        key_ids=(),
        pssh_boxes=(),
        encrypted=False,
    )


def _fetch_bounded_bytes(
    url: str,
    session: requests.Session,
    *,
    max_bytes: int,
    timeout: float | tuple[float, float] = DEFAULT_HTTP_TIMEOUT,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, str]:
    try:
        resp = session.get(url, stream=True, timeout=timeout, headers=headers or {})
    except Exception as exc:
        if is_transient_network_error(exc):
            raise RequestError(detail=f"Failed to fetch {url}: {exc}") from exc
        raise GenericAudiobookDLException(f"Failed to fetch {url}: {exc}") from exc

    try:
        if is_cloudflare_challenge(resp):
            raise CloudflareBlocked(
                detail=f"HTTP {resp.status_code} Cloudflare challenge from {url}"
            )
        if resp.status_code in (401, 403):
            raise UserNotAuthorized(detail=f"HTTP {resp.status_code} from {url}")
        if resp.status_code not in (200, 206):
            raise RequestError(detail=f"HTTP {resp.status_code} from {url}")

        buf = bytearray()
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            remaining = max_bytes - len(buf)
            if remaining <= 0:
                break
            buf.extend(chunk[:remaining])
            if len(buf) >= max_bytes:
                break
        resolved_url = getattr(resp, "url", None)
        final_url = resolved_url if isinstance(resolved_url, str) and resolved_url else url
        return bytes(buf), final_url
    finally:
        resp.close()


def _parse_hls_map_range_header(raw_range: str | None) -> dict[str, str] | None:
    if not raw_range:
        return None
    text = raw_range.strip()
    if "@" in text:
        length_str, offset_str = text.split("@", 1)
        start = int(offset_str)
        length = int(length_str)
    else:
        start = 0
        length = int(text)
    if length <= 0 or start < 0:
        return None
    return {"Range": f"bytes={start}-{start + length - 1}"}


def _merge_reports(primary: ProtectionReport, extra: ProtectionReport) -> ProtectionReport:
    systems = tuple(dict.fromkeys([*primary.systems, *extra.systems]))
    kids = tuple(dict.fromkeys([*primary.key_ids, *extra.key_ids]))
    boxes = (*primary.pssh_boxes, *extra.pssh_boxes)
    return ProtectionReport(
        source_label=primary.source_label,
        container=primary.container,
        systems=systems,
        key_ids=kids,
        pssh_boxes=boxes,
        encrypted=primary.encrypted or extra.encrypted,
    )


def inspect_url(
    url: str,
    session: requests.Session | None = None,
    *,
    max_bytes: int = DEFAULT_INSPECT_MAX_BYTES,
    timeout: float | tuple[float, float] = DEFAULT_HTTP_TIMEOUT,
) -> ProtectionReport:
    """Fetch up to `max_bytes` from `url` and return a `ProtectionReport`."""
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise GenericAudiobookDLException(f"Invalid HTTP(S) URL: {url}")

    owns_session = session is None
    active_session = session or create_configured_http_session()
    try:
        raw, resolved_url = _fetch_bounded_bytes(
            cleaned,
            active_session,
            max_bytes=max_bytes,
            timeout=timeout,
        )
        report = inspect_bytes(raw, source_label=resolved_url)
        if report.container == "hls":
            playlist = m3u8.loads(raw.decode("utf-8-sig", errors="replace"), uri=resolved_url)
            if playlist.is_variant and playlist.playlists and not report.encrypted:
                variant_uri = playlist.playlists[0].absolute_uri or urljoin(
                    resolved_url, str(playlist.playlists[0].uri or "")
                )
                if variant_uri:
                    var_raw, var_url = _fetch_bounded_bytes(
                        variant_uri,
                        active_session,
                        max_bytes=max_bytes,
                        timeout=timeout,
                    )
                    var_report = inspect_bytes(var_raw, source_label=var_url)
                    report = _merge_reports(report, var_report)
                    playlist = m3u8.loads(
                        var_raw.decode("utf-8-sig", errors="replace"), uri=var_url
                    )

            for seg in getattr(playlist, "segments", None) or ():
                init_sec = getattr(seg, "init_section", None)
                init_uri = (
                    getattr(init_sec, "absolute_uri", None)
                    or (
                        urljoin(resolved_url, str(getattr(init_sec, "uri", "")))
                        if getattr(init_sec, "uri", None)
                        else None
                    )
                )
                if init_uri:
                    range_hdr = _parse_hls_map_range_header(getattr(init_sec, "byterange", None))
                    init_raw, _ = _fetch_bounded_bytes(
                        str(init_uri),
                        active_session,
                        max_bytes=max_bytes,
                        timeout=timeout,
                        headers=range_hdr,
                    )
                    init_boxes = parse_pssh_boxes(init_raw) if _looks_like_isobmff(init_raw) else []
                    init_kids = extract_cenc_kids_safe(init_raw)
                    init_report = ProtectionReport(
                        source_label=str(init_uri),
                        container="isobmff",
                        systems=tuple(dict.fromkeys(b.system_name for b in init_boxes)),
                        key_ids=init_kids,
                        pssh_boxes=tuple(init_boxes),
                        encrypted=bool(init_boxes or init_kids),
                    )
                    report = _merge_reports(report, init_report)
                    break
        return report
    finally:
        if owns_session:
            active_session.close()


def format_report(report: ProtectionReport) -> str:
    """Format a `ProtectionReport` into a human-readable multi-line summary."""
    lines = [
        f"Source: {report.source_label}",
        f"Container: {report.container.upper() if report.container != 'unknown' else 'Unknown'}",
        f"Encrypted: {'Yes' if report.encrypted else 'No'}",
        f"DRM Systems: {', '.join(report.systems) if report.systems else 'None detected'}",
    ]
    if report.key_ids:
        lines.append(f"Key IDs ({len(report.key_ids)}):")
        for kid in report.key_ids:
            lines.append(f"  - {kid.hex()} ({UUID(bytes=kid)})")
    else:
        lines.append("Key IDs: None detected")

    if report.pssh_boxes:
        lines.append(f"PSSH Boxes ({len(report.pssh_boxes)}):")
        for idx, box in enumerate(report.pssh_boxes, start=1):
            box_kids = ", ".join(k.hex() for k in box.key_ids) if box.key_ids else "none"
            lines.append(
                f"  [{idx}] {box.system_name} v{box.version} "
                f"(system_id={box.system_id}, kids={box_kids}, data={len(box.data)} bytes)"
            )
    else:
        lines.append("PSSH Boxes: None")
    return "\n".join(lines)
