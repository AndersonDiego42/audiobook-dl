"""Pure-bytes ISO BMFF PSSH, `tenc`, and HLS DRM initialization data parser."""

from __future__ import annotations

import base64
import binascii
import os
import struct
from collections.abc import Iterator
from urllib.parse import unquote_to_bytes
from uuid import UUID
import xml.etree.ElementTree as ET

from attrs import frozen

from audiobookdl.exceptions import GenericAudiobookDLException

WIDEVINE_SYSTEM_ID = UUID("edef8ba9-79d6-4ace-a3c8-27dcd51d21ed")
PLAYREADY_SYSTEM_ID = UUID("9a04f079-9840-4286-ab92-e65be0885f95")
CLEARKEY_SYSTEM_ID = UUID("1077efec-c0b2-4d02-ace3-3c1e52e2fb4b")
FAIRPLAY_SYSTEM_ID = UUID("94ce86fb-07ff-4f43-adb8-93d2fa968ca2")

_SYSTEM_NAMES: dict[UUID, str] = {
    WIDEVINE_SYSTEM_ID: "Widevine",
    PLAYREADY_SYSTEM_ID: "PlayReady",
    CLEARKEY_SYSTEM_ID: "ClearKey",
    FAIRPLAY_SYSTEM_ID: "FairPlay",
}

_AUDIO_SAMPLE_ENTRY_TYPES = frozenset(
    {b"enca", b"mp4a", b"ec-3", b"ac-3", b"Opus", b"fLaC", b"alac"}
)
_VIDEO_SAMPLE_ENTRY_TYPES = frozenset(
    {b"encv", b"mp4v", b"avc1", b"avc3", b"hev1", b"hvc1", b"vp09", b"av01"}
)
_AUDIO_ENTRY_CHILD_OFFSETS = {0: 28, 1: 44, 2: 64}
_ZERO_KID = b"\x00" * 16
_PLAYREADY_KEYFORMAT = "com.microsoft.playready"
_MAX_CENC_METADATA_BOX_BYTES = 4 * 1024 * 1024
_MAX_CENC_METADATA_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_CENC_TOP_LEVEL_BOXES = 128


def _parse_safe_xml(xml_text: str, *, context: str) -> ET.Element:
    """Parse XML text after rejecting doctype/entity declarations."""
    upper = xml_text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise PsshParseError(f"Disallowed XML doctype or entity in {context}")
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise PsshParseError(f"Invalid {context} XML: {exc}") from exc


class PsshParseError(GenericAudiobookDLException):
    """Raised when PSSH, `tenc`, or DRM initialization data is malformed."""


@frozen
class PsshBox:
    """Parsed ISO BMFF Protection System Specific Header (`pssh`) box."""

    system_id: UUID
    version: int
    key_ids: tuple[bytes, ...]
    data: bytes

    @property
    def system_name(self) -> str:
        """Human-readable DRM system name, or canonical UUID string if unknown."""
        return _SYSTEM_NAMES.get(self.system_id, str(self.system_id))


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Decode an unsigned protobuf base-128 varint starting at `pos`."""
    result = 0
    shift = 0
    total = len(buf)
    for _ in range(10):
        if pos >= total:
            raise PsshParseError("Truncated protobuf varint in Widevine PSSH data")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if (byte & 0x80) == 0:
            return result, pos
        shift += 7
    raise PsshParseError("Overflowing protobuf varint in Widevine PSSH data")


def _parse_widevine_pssh_data(data: bytes) -> tuple[bytes, ...]:
    """Extract 16-byte `key_id` entries (protobuf field 2) from `WidevinePsshData`."""
    kids: list[bytes] = []
    pos = 0
    total = len(data)
    while pos < total:
        tag, pos = _read_varint(data, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if field_number == 0:
            raise PsshParseError("Invalid protobuf field number 0 in Widevine PSSH data")
        if wire_type == 0:
            _, pos = _read_varint(data, pos)
        elif wire_type == 1:
            if pos + 8 > total:
                raise PsshParseError("Truncated 64-bit field in Widevine PSSH data")
            pos += 8
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            if length > total - pos:
                raise PsshParseError("Truncated length-delimited field in Widevine PSSH data")
            payload = data[pos : pos + length]
            pos += length
            if field_number == 2 and len(payload) == 16:
                kids.append(payload)
        elif wire_type == 5:
            if pos + 4 > total:
                raise PsshParseError("Truncated 32-bit field in Widevine PSSH data")
            pos += 4
        else:
            raise PsshParseError(f"Unsupported protobuf wire type {wire_type} in Widevine PSSH data")
    return tuple(dict.fromkeys(kids))


def _parse_playready_wrmheader(xml_text: str) -> tuple[bytes, ...]:
    """Extract big-endian 16-byte KIDs from PlayReady WRMHEADER XML (v4.0-v4.3)."""
    cleaned = xml_text.lstrip("\ufeff").rstrip("\x00").strip()
    if not cleaned:
        return ()
    root = _parse_safe_xml(cleaned, context="PlayReady WRMHEADER")
    if root.tag.rsplit("}", 1)[-1] != "WRMHEADER":
        raise PsshParseError("Expected WRMHEADER root element in PlayReady header")

    kids: list[bytes] = []
    for elem in root.iter():
        if elem.tag.rsplit("}", 1)[-1] != "KID":
            continue
        raw_b64 = (elem.attrib.get("VALUE") or elem.attrib.get("value") or elem.text or "").strip()
        if not raw_b64:
            continue
        try:
            raw_guid = base64.b64decode(raw_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PsshParseError("Invalid base64 KID in PlayReady WRMHEADER") from exc
        if len(raw_guid) != 16:
            raise PsshParseError(f"Invalid PlayReady KID length ({len(raw_guid)} bytes; expected 16)")
        # PlayReady encodes KIDs as little-endian Microsoft GUIDs; swap to RFC 4122 big-endian bytes.
        kids.append(UUID(bytes_le=raw_guid).bytes)
    return tuple(dict.fromkeys(kids))


def _parse_playready_pssh_data(data: bytes) -> tuple[bytes, ...]:
    """Parse a PlayReady Object (PRO) binary payload and extract KIDs from its WRMHEADER."""
    if len(data) < 6:
        raise PsshParseError("Truncated PlayReady Object header")
    pro_len, record_count = struct.unpack_from("<IH", data, 0)
    if pro_len < 6 or pro_len > len(data):
        raise PsshParseError(f"Invalid PlayReady Object length {pro_len} (buffer {len(data)} bytes)")
    offset = 6
    kids: list[bytes] = []
    for _ in range(record_count):
        if offset + 4 > pro_len:
            raise PsshParseError("Truncated PlayReady Object record header")
        rec_type, rec_len = struct.unpack_from("<HH", data, offset)
        offset += 4
        if rec_len > pro_len - offset:
            raise PsshParseError("Truncated PlayReady Object record payload")
        rec_payload = data[offset : offset + rec_len]
        offset += rec_len
        if rec_type == 0x0001:
            try:
                xml_text = rec_payload.decode("utf-16-le")
            except UnicodeDecodeError as exc:
                raise PsshParseError("Invalid UTF-16LE PlayReady WRMHEADER") from exc
            kids.extend(_parse_playready_wrmheader(xml_text))
    return tuple(dict.fromkeys(kids))


def _read_box_header(
    view: memoryview,
    offset: int,
    end: int,
    *,
    allow_eof_mdat: bool = False,
) -> tuple[bytes, int, int]:
    """Parse one ISO BMFF box header at `offset`, returning `(box_type, payload_offset, box_end)`."""
    if offset + 8 > end:
        raise PsshParseError(f"Truncated ISO BMFF box header at offset {offset}")
    size, box_type = struct.unpack_from(">I4s", view, offset)
    header_size = 8
    if size == 1:
        if offset + 16 > end:
            raise PsshParseError(f"Truncated 64-bit ISO BMFF box header for {box_type!r}")
        size = struct.unpack_from(">Q", view, offset + 8)[0]
        header_size = 16
    elif size == 0:
        if allow_eof_mdat and box_type == b"mdat":
            return box_type, offset + header_size, end
        raise PsshParseError(f"Invalid zero-size ISO BMFF box {box_type!r}")
    if size < header_size:
        raise PsshParseError(
            f"Invalid ISO BMFF box size {size} for {box_type!r} (minimum {header_size})"
        )
    if size > end - offset:
        raise PsshParseError(
            f"ISO BMFF box {box_type!r} extends past buffer end ({offset + size} > {end})"
        )
    return box_type, offset + header_size, offset + size


def _iter_boxes(
    view: memoryview,
    start: int,
    end: int,
    *,
    allow_eof_mdat: bool = False,
) -> Iterator[tuple[bytes, int, int, int]]:
    """Yield `(box_type, box_start, payload_start, box_end)` for sequential boxes in `[start, end)`."""
    offset = start
    while offset < end:
        box_type, payload_start, box_end = _read_box_header(
            view,
            offset,
            end,
            allow_eof_mdat=allow_eof_mdat,
        )
        yield box_type, offset, payload_start, box_end
        offset = box_end


def _parse_pssh_box_slice(view: memoryview, payload_start: int, box_end: int) -> PsshBox:
    """Parse the FullBox body of a single `pssh` box spanning `[payload_start, box_end)`."""
    cursor = payload_start
    if cursor + 20 > box_end:
        raise PsshParseError("Truncated PSSH box header")
    version = view[cursor]
    cursor += 4  # version (1) + flags (3)
    if version not in (0, 1):
        raise PsshParseError(f"Unsupported PSSH box version: {version}")

    system_id = UUID(bytes=bytes(view[cursor : cursor + 16]))
    cursor += 16

    header_kids: list[bytes] = []
    if version == 1:
        if cursor + 4 > box_end:
            raise PsshParseError("Truncated PSSH v1 KID count")
        kid_count = struct.unpack_from(">I", view, cursor)[0]
        cursor += 4
        if kid_count > (box_end - cursor) // 16:
            raise PsshParseError(f"PSSH v1 KID count ({kid_count}) exceeds box bounds")
        for i in range(kid_count):
            start = cursor + i * 16
            header_kids.append(bytes(view[start : start + 16]))
        cursor += kid_count * 16

    if cursor + 4 > box_end:
        raise PsshParseError("Truncated PSSH data size")
    data_size = struct.unpack_from(">I", view, cursor)[0]
    cursor += 4
    if data_size > box_end - cursor:
        raise PsshParseError(
            f"PSSH data size ({data_size}) exceeds box bounds ({box_end - cursor} remaining)"
        )
    pssh_data = bytes(view[cursor : cursor + data_size])
    cursor += data_size
    if cursor != box_end:
        raise PsshParseError(f"Unexpected trailing bytes ({box_end - cursor}) in PSSH box")

    extracted_kids: tuple[bytes, ...] = ()
    if pssh_data:
        if system_id == WIDEVINE_SYSTEM_ID:
            extracted_kids = _parse_widevine_pssh_data(pssh_data)
        elif system_id == PLAYREADY_SYSTEM_ID:
            extracted_kids = _parse_playready_pssh_data(pssh_data)

    all_kids = tuple(dict.fromkeys([*header_kids, *extracted_kids]))
    return PsshBox(
        system_id=system_id,
        version=version,
        key_ids=all_kids,
        data=pssh_data,
    )


def parse_pssh_box(box: bytes) -> PsshBox:
    """Parse a single serialized ISO BMFF `pssh` box into a `PsshBox`."""
    view = memoryview(box)
    box_type, payload_start, box_end = _read_box_header(view, 0, len(view))
    if box_type != b"pssh":
        raise PsshParseError(f"Expected 'pssh' box, got {box_type!r}")
    if box_end != len(view):
        raise PsshParseError(f"Unexpected trailing bytes after 'pssh' box ({len(view) - box_end})")
    return _parse_pssh_box_slice(view, payload_start, box_end)


def parse_pssh_boxes(data: bytes) -> list[PsshBox]:
    """Scan top-level boxes and `moov`/`moof` containers in `data` for `pssh` boxes."""
    if not data:
        return []
    view = memoryview(data)
    boxes: list[PsshBox] = []
    for box_type, _start, payload_start, box_end in _iter_boxes(view, 0, len(view), allow_eof_mdat=True):
        if box_type == b"pssh":
            boxes.append(_parse_pssh_box_slice(view, payload_start, box_end))
        elif box_type in (b"moov", b"moof"):
            for child_type, _cstart, child_payload, child_end in _iter_boxes(view, payload_start, box_end):
                if child_type == b"pssh":
                    boxes.append(_parse_pssh_box_slice(view, child_payload, child_end))
    return boxes


def _parse_sinf_tenc_kids(view: memoryview, sinf_start: int, sinf_end: int) -> list[bytes]:
    kids: list[bytes] = []
    for child_type, _cs, child_payload, child_end in _iter_boxes(view, sinf_start, sinf_end):
        if child_type != b"schi":
            continue
        for schi_type, _ss, tenc_payload, tenc_end in _iter_boxes(view, child_payload, child_end):
            if schi_type != b"tenc":
                continue
            if tenc_end - tenc_payload < 24:
                raise PsshParseError("Truncated 'tenc' box")
            is_protected = view[tenc_payload + 6]
            default_kid = bytes(view[tenc_payload + 8 : tenc_payload + 24])
            if is_protected != 0 and default_kid != _ZERO_KID:
                kids.append(default_kid)
    return kids


def _parse_stsd_tenc_kids(view: memoryview, stsd_payload: int, stsd_end: int) -> list[bytes]:
    if stsd_end - stsd_payload < 8:
        raise PsshParseError("Truncated 'stsd' box")
    entry_count = struct.unpack_from(">I", view, stsd_payload + 4)[0]
    kids: list[bytes] = []
    parsed = 0
    for entry_type, _es, entry_payload, entry_end in _iter_boxes(view, stsd_payload + 8, stsd_end):
        if parsed >= entry_count:
            break
        parsed += 1
        child_offset: int | None = None
        if entry_type in _AUDIO_SAMPLE_ENTRY_TYPES:
            if entry_end - entry_payload < 28:
                raise PsshParseError(f"Truncated audio sample entry {entry_type!r}")
            entry_version = struct.unpack_from(">H", view, entry_payload + 8)[0]
            child_offset = _AUDIO_ENTRY_CHILD_OFFSETS.get(entry_version)
        elif entry_type in _VIDEO_SAMPLE_ENTRY_TYPES:
            if entry_end - entry_payload < 78:
                raise PsshParseError(f"Truncated video sample entry {entry_type!r}")
            child_offset = 78
        if child_offset is not None and entry_payload + child_offset <= entry_end:
            for box_type, _bs, box_payload, box_end in _iter_boxes(
                view,
                entry_payload + child_offset,
                entry_end,
            ):
                if box_type == b"sinf":
                    kids.extend(_parse_sinf_tenc_kids(view, box_payload, box_end))
    return kids


def parse_tenc_default_kids(data: bytes) -> tuple[bytes, ...]:
    """Extract deduplicated 16-byte default KIDs from `moov/trak/mdia/minf/stbl/stsd/*/sinf/schi/tenc`."""
    if not data:
        return ()
    view = memoryview(data)
    kids: list[bytes] = []
    for top_type, _ts, moov_start, moov_end in _iter_boxes(view, 0, len(view), allow_eof_mdat=True):
        if top_type != b"moov":
            continue
        for trak_type, _tks, trak_start, trak_end in _iter_boxes(view, moov_start, moov_end):
            if trak_type != b"trak":
                continue
            for mdia_type, _ms, mdia_start, mdia_end in _iter_boxes(view, trak_start, trak_end):
                if mdia_type != b"mdia":
                    continue
                for minf_type, _mfs, minf_start, minf_end in _iter_boxes(view, mdia_start, mdia_end):
                    if minf_type != b"minf":
                        continue
                    for stbl_type, _sbs, stbl_start, stbl_end in _iter_boxes(view, minf_start, minf_end):
                        if stbl_type != b"stbl":
                            continue
                        for stsd_type, _sds, stsd_start, stsd_end in _iter_boxes(
                            view, stbl_start, stbl_end
                        ):
                            if stsd_type == b"stsd":
                                kids.extend(_parse_stsd_tenc_kids(view, stsd_start, stsd_end))
    return tuple(dict.fromkeys(kids))


def extract_cenc_kids(data: bytes) -> tuple[bytes, ...]:
    """Return deduplicated 16-byte KIDs from `pssh` and `tenc` boxes in `data`."""
    kids: list[bytes] = []
    for box in parse_pssh_boxes(data):
        kids.extend(box.key_ids)
    kids.extend(parse_tenc_default_kids(data))
    return tuple(dict.fromkeys(kids))


def extract_cenc_kids_safe(data: bytes) -> tuple[bytes, ...]:
    """Best-effort variant of `extract_cenc_kids` that ignores non-ISO-BMFF or corrupt stubs."""
    if not data:
        return ()
    kids: list[bytes] = []
    try:
        for box in parse_pssh_boxes(data):
            kids.extend(box.key_ids)
    except PsshParseError:
        pass
    try:
        kids.extend(parse_tenc_default_kids(data))
    except PsshParseError:
        pass
    return tuple(dict.fromkeys(kids))


def _decode_data_uri(uri: str) -> bytes:
    header, sep, payload = uri.partition(",")
    if not sep or not payload:
        raise PsshParseError("Invalid inline data: URI in HLS #EXT-X-KEY")
    if ";base64" in header.lower():
        try:
            decoded = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PsshParseError("Invalid base64 payload in HLS #EXT-X-KEY data: URI") from exc
    else:
        decoded = unquote_to_bytes(payload)
    if not decoded:
        raise PsshParseError("Empty HLS #EXT-X-KEY data: URI payload")
    return decoded


def _looks_like_isobmff_pssh_stream(raw: bytes) -> bool:
    if len(raw) < 8:
        return False
    return raw[4:8] in (b"pssh", b"ftyp", b"moov", b"moof")


def _try_decode_wrmheader_bytes(raw: bytes) -> str | None:
    if raw.startswith((b"\xff\xfe<\x00", b"<\x00W\x00R\x00M\x00")):
        try:
            return raw.decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise PsshParseError("Invalid UTF-16LE PlayReady WRMHEADER") from exc
    stripped = raw.lstrip()
    if stripped.startswith(b"<WRMHEADER"):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PsshParseError("Invalid UTF-8 PlayReady WRMHEADER") from exc
    return None


def parse_hls_key_data(uri: str, *, key_format: str | None = None) -> list[PsshBox]:
    """
    Parse an HLS `#EXT-X-KEY` `data:` URI containing PSSH box(es) or raw Widevine/PlayReady init data.

    Returns an empty list for non-`data:` URIs (such as `https://` or `skd://`).
    Raises `PsshParseError` for malformed `data:` URIs or unrecognized/corrupt payloads.
    """
    if not uri.lower().startswith("data:"):
        return []
    raw = _decode_data_uri(uri)

    if _looks_like_isobmff_pssh_stream(raw):
        boxes = parse_pssh_boxes(raw)
        if not boxes:
            raise PsshParseError("No PSSH boxes found in ISO BMFF HLS key payload")
        return boxes

    fmt_lower = (key_format or "").strip().lower()
    if fmt_lower == f"urn:uuid:{WIDEVINE_SYSTEM_ID}":
        kids = _parse_widevine_pssh_data(raw)
        return [PsshBox(system_id=WIDEVINE_SYSTEM_ID, version=0, key_ids=kids, data=raw)]

    if fmt_lower in (f"urn:uuid:{PLAYREADY_SYSTEM_ID}", _PLAYREADY_KEYFORMAT):
        xml_text = _try_decode_wrmheader_bytes(raw)
        if xml_text is not None:
            kids = _parse_playready_wrmheader(xml_text)
        else:
            kids = _parse_playready_pssh_data(raw)
        return [PsshBox(system_id=PLAYREADY_SYSTEM_ID, version=0, key_ids=kids, data=raw)]

    xml_text = _try_decode_wrmheader_bytes(raw)
    if xml_text is not None:
        kids = _parse_playready_wrmheader(xml_text)
        return [PsshBox(system_id=PLAYREADY_SYSTEM_ID, version=0, key_ids=kids, data=raw)]

    if len(raw) >= 6 and struct.unpack_from("<I", raw, 0)[0] == len(raw):
        kids = _parse_playready_pssh_data(raw)
        return [PsshBox(system_id=PLAYREADY_SYSTEM_ID, version=0, key_ids=kids, data=raw)]

    kids = _parse_widevine_pssh_data(raw)
    if kids:
        return [PsshBox(system_id=WIDEVINE_SYSTEM_ID, version=0, key_ids=kids, data=raw)]

    raise PsshParseError("Unrecognized DRM initialization payload in HLS #EXT-X-KEY data: URI")


@frozen
class MpdProtection:
    """Parsed DASH MPD `<ContentProtection>` entry."""

    scheme_id: str
    system_id: UUID | None
    default_kid: bytes | None
    pssh_boxes: tuple[PsshBox, ...]

    @property
    def system_name(self) -> str:
        """Human-readable DRM system name when `system_id` is known, else `scheme_id`."""
        if self.system_id is not None:
            return _SYSTEM_NAMES.get(self.system_id, str(self.system_id))
        return self.scheme_id


def parse_mpd_content_protection(xml: bytes) -> list[MpdProtection]:
    """
    Parse DASH MPD `<ContentProtection>` elements under `AdaptationSet` and `Representation`.

    Extracts `schemeIdUri`, `cenc:default_KID`, `<cenc:pssh>` boxes, and `<mspr:pro>` records.
    Raises `PsshParseError` for invalid XML, doctype declarations, or malformed `cenc:pssh`/KID values.
    """
    try:
        text = xml.decode("utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise PsshParseError("Invalid UTF-8 in DASH MPD XML") from exc
    if not text:
        raise PsshParseError("Empty DASH MPD XML")

    root = _parse_safe_xml(text, context="DASH MPD")
    if root.tag.rsplit("}", 1)[-1] != "MPD":
        raise PsshParseError("Expected MPD root element in DASH manifest")

    results: list[MpdProtection] = []
    for parent in root.iter():
        if parent.tag.rsplit("}", 1)[-1] not in ("AdaptationSet", "Representation"):
            continue
        for cp in parent:
            if cp.tag.rsplit("}", 1)[-1] != "ContentProtection":
                continue
            scheme_id = (cp.attrib.get("schemeIdUri") or "").strip()
            system_id: UUID | None = None
            if scheme_id.lower().startswith("urn:uuid:"):
                try:
                    system_id = UUID(scheme_id[9:].strip())
                except ValueError:
                    system_id = None

            default_kid: bytes | None = None
            for attr_key, attr_val in cp.attrib.items():
                if attr_key.rsplit("}", 1)[-1].lower() == "default_kid":
                    raw_kid = attr_val.strip()
                    if raw_kid:
                        try:
                            default_kid = UUID(raw_kid).bytes
                        except ValueError as exc:
                            raise PsshParseError(
                                f"Invalid cenc:default_KID '{raw_kid}' in DASH MPD"
                            ) from exc
                    break

            boxes: list[PsshBox] = []
            for child in cp:
                local = child.tag.rsplit("}", 1)[-1].lower()
                if local == "pssh":
                    b64_text = "".join((child.text or "").split())
                    if not b64_text:
                        raise PsshParseError("Empty <cenc:pssh> element in DASH MPD")
                    try:
                        raw_pssh = base64.b64decode(b64_text, validate=True)
                    except (binascii.Error, ValueError) as exc:
                        raise PsshParseError("Invalid base64 in DASH MPD <cenc:pssh>") from exc
                    parsed = parse_pssh_boxes(raw_pssh)
                    if not parsed:
                        raise PsshParseError("No PSSH box found in DASH MPD <cenc:pssh>")
                    boxes.extend(parsed)
                elif local == "pro":
                    b64_text = "".join((child.text or "").split())
                    if not b64_text:
                        raise PsshParseError("Empty <mspr:pro> element in DASH MPD")
                    try:
                        raw_pro = base64.b64decode(b64_text, validate=True)
                    except (binascii.Error, ValueError) as exc:
                        raise PsshParseError("Invalid base64 in DASH MPD <mspr:pro>") from exc
                    pro_kids = _parse_playready_pssh_data(raw_pro)
                    boxes.append(
                        PsshBox(
                            system_id=PLAYREADY_SYSTEM_ID,
                            version=0,
                            key_ids=pro_kids,
                            data=raw_pro,
                        )
                    )

            results.append(
                MpdProtection(
                    scheme_id=scheme_id,
                    system_id=system_id,
                    default_kid=default_kid,
                    pssh_boxes=tuple(boxes),
                )
            )
    return results


def read_mp4_init_boxes(path: str) -> bytes:
    """Read top-level `moov`/`moof`/`pssh` boxes from an MP4 file on disk without loading `mdat`."""
    try:
        file_size = os.path.getsize(path)
    except OSError:
        return b""
    if file_size < 8:
        return b""
    collected = bytearray()
    pos = 0
    try:
        with open(path, "rb") as src:
            for _ in range(_MAX_CENC_TOP_LEVEL_BOXES):
                if pos + 8 > file_size:
                    break
                src.seek(pos)
                header = src.read(8)
                if len(header) < 8:
                    break
                raw_size, box_type = struct.unpack(">I4s", header)
                header_size = 8
                if raw_size == 1:
                    if pos + 16 > file_size:
                        break
                    ext_hdr = src.read(8)
                    if len(ext_hdr) < 8:
                        break
                    box_size = struct.unpack(">Q", ext_hdr)[0]
                    header_size = 16
                elif raw_size == 0:
                    box_size = file_size - pos
                else:
                    box_size = raw_size
                if box_size < header_size or pos + box_size > file_size:
                    break
                if (
                    box_type in (b"moov", b"moof", b"pssh")
                    and box_size <= _MAX_CENC_METADATA_BOX_BYTES
                    and len(collected) + box_size <= _MAX_CENC_METADATA_TOTAL_BYTES
                ):
                    src.seek(pos)
                    chunk = src.read(box_size)
                    if len(chunk) == box_size:
                        collected.extend(chunk)
                pos += box_size
    except OSError:
        return b""
    return bytes(collected)
