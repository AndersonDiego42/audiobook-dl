"""Structured failure diagnostics capture and persistence for E2E tests."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional


_SENSITIVE_HEADER_NAMES = frozenset({"authorization", "cookie", "set-cookie", "proxy-authorization"})
_SENSITIVE_ENV_FRAGMENTS = ("TOKEN", "SECRET", "PASSWORD", "KEY")


def _redact_headers(headers: Dict[str, str]) -> Dict[str, str]:
    redacted: Dict[str, str] = {}
    for key, val in headers.items():
        if key.lower() in _SENSITIVE_HEADER_NAMES:
            redacted[key] = "<redacted>" if val else ""
        else:
            redacted[key] = val
    return redacted


def _redact_env(env: Dict[str, str]) -> Dict[str, str]:
    cleaned: Dict[str, str] = {}
    for key, val in sorted(env.items()):
        upper = key.upper()
        if any(fragment in upper for fragment in _SENSITIVE_ENV_FRAGMENTS):
            cleaned[key] = "<redacted>"
        else:
            cleaned[key] = val
    return cleaned


def _sanitize_nodeid(nodeid: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", nodeid).strip("_") or "e2e_test"


@dataclass
class SubprocessRecord:
    """Captured details for a single CLI or GUI subprocess invocation."""

    command: List[str]
    cwd: str
    returncode: Optional[int]
    duration_seconds: float
    timed_out: bool
    stdout: str
    stderr: str
    env_overrides: Dict[str, str] = field(default_factory=dict)


@dataclass
class HttpExchangeRecord:
    """Captured details for an HTTP request handled by the local test server."""

    method: str
    raw_path: str
    normalized_path: str
    status_code: int
    bytes_sent: int
    headers: Dict[str, str] = field(default_factory=dict)
    body_preview: str = ""


class E2EDiagnosticRecorder:
    """Collects runtime artifacts during an E2E test for post-failure inspection."""

    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self.subprocesses: List[SubprocessRecord] = []
        self.http_exchanges: List[HttpExchangeRecord] = []
        self.gui_snapshots: List[Dict[str, Any]] = []
        self.notes: List[str] = []
        self.workspace_root: Optional[Path] = None

    def set_workspace_root(self, root: Path) -> None:
        self.workspace_root = root

    def add_note(self, message: str) -> None:
        self.notes.append(message)

    def record_subprocess(
        self,
        *,
        command: List[str],
        cwd: str,
        returncode: Optional[int],
        duration_seconds: float,
        timed_out: bool,
        stdout: str,
        stderr: str,
        env_overrides: Optional[Dict[str, str]] = None,
    ) -> None:
        self.subprocesses.append(
            SubprocessRecord(
                command=list(command),
                cwd=cwd,
                returncode=returncode,
                duration_seconds=round(duration_seconds, 3),
                timed_out=timed_out,
                stdout=stdout,
                stderr=stderr,
                env_overrides=_redact_env(env_overrides or {}),
            )
        )

    def record_http_exchange(
        self,
        *,
        method: str,
        raw_path: str,
        normalized_path: str,
        status_code: int,
        bytes_sent: int,
        headers: Optional[Dict[str, str]] = None,
        body_preview: str = "",
    ) -> None:
        self.http_exchanges.append(
            HttpExchangeRecord(
                method=method,
                raw_path=raw_path,
                normalized_path=normalized_path,
                status_code=status_code,
                bytes_sent=bytes_sent,
                headers=_redact_headers(headers or {}),
                body_preview=body_preview,
            )
        )

    def record_gui_snapshot(self, snapshot: Dict[str, Any]) -> None:
        self.gui_snapshots.append(snapshot)

    def _collect_workspace_tree(self) -> List[str]:
        if self.workspace_root is None or not self.workspace_root.exists():
            return []
        entries: List[str] = []
        for dirpath, _, filenames in os.walk(self.workspace_root):
            rel_dir = os.path.relpath(dirpath, self.workspace_root)
            for name in sorted(filenames):
                full_path = Path(dirpath) / name
                rel_path = name if rel_dir == "." else os.path.join(rel_dir, name)
                try:
                    size = full_path.stat().st_size
                except OSError:
                    size = -1
                entries.append(f"{rel_path} ({size} bytes)")
        return sorted(entries)

    def render_report(self) -> str:
        """Render a human-readable summary of all captured E2E state."""
        lines: List[str] = [f"=== E2E Diagnostics: {self.nodeid} ==="]

        if self.notes:
            lines.append("\n[Notes]")
            lines.extend(f"  - {note}" for note in self.notes)

        if self.subprocesses:
            lines.append(f"\n[Subprocesses ({len(self.subprocesses)})]")
            for idx, proc in enumerate(self.subprocesses, start=1):
                lines.append(
                    f"  #{idx}: cmd={proc.command!r} cwd={proc.cwd!r} "
                    f"rc={proc.returncode} timeout={proc.timed_out} dur={proc.duration_seconds}s"
                )
                if proc.env_overrides:
                    lines.append(f"       env_overrides={proc.env_overrides}")
                if proc.stdout.strip():
                    lines.append("       stdout:")
                    for sline in proc.stdout.strip().splitlines()[-30:]:
                        lines.append(f"         | {sline}")
                if proc.stderr.strip():
                    lines.append("       stderr:")
                    for eline in proc.stderr.strip().splitlines()[-30:]:
                        lines.append(f"         | {eline}")

        if self.http_exchanges:
            lines.append(f"\n[HTTP Exchanges ({len(self.http_exchanges)})]")
            for idx, ex in enumerate(self.http_exchanges, start=1):
                lines.append(
                    f"  #{idx}: {ex.method} {ex.raw_path} (path={ex.normalized_path}) "
                    f"-> {ex.status_code} ({ex.bytes_sent} bytes)"
                )

        if self.gui_snapshots:
            lines.append(f"\n[GUI Snapshots ({len(self.gui_snapshots)})]")
            for idx, snap in enumerate(self.gui_snapshots, start=1):
                lines.append(f"  #{idx}: {json.dumps(snap, default=str)}")

        tree = self._collect_workspace_tree()
        if tree:
            lines.append(f"\n[Workspace Files ({len(tree)})]")
            for item in tree[:80]:
                lines.append(f"  - {item}")
            if len(tree) > 80:
                lines.append(f"  ... ({len(tree) - 80} more files)")

        return "\n".join(lines)

    def persist_bundle(self, artifacts_dir: Path) -> Path:
        """Write detailed diagnostic artifacts to disk and return the test bundle directory."""
        bundle_dir = artifacts_dir / _sanitize_nodeid(self.nodeid)
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "report.txt").write_text(self.render_report(), encoding="utf-8")
        (bundle_dir / "http_exchanges.json").write_text(
            json.dumps([asdict(ex) for ex in self.http_exchanges], indent=2),
            encoding="utf-8",
        )
        for idx, proc in enumerate(self.subprocesses, start=1):
            (bundle_dir / f"subprocess_{idx}_stdout.log").write_text(proc.stdout, encoding="utf-8")
            (bundle_dir / f"subprocess_{idx}_stderr.log").write_text(proc.stderr, encoding="utf-8")
        if self.gui_snapshots:
            (bundle_dir / "gui_snapshots.json").write_text(
                json.dumps(self.gui_snapshots, indent=2, default=str),
                encoding="utf-8",
            )
        return bundle_dir
