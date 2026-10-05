"""Cybr Proof — storage layout and trace I/O.

Everything lives under ``$HERMES_HOME/cybr-proof/<session_id>/``::

    trace.jsonl      one JSON event per line (frames + actions), append-only
    frames/0001.png  copies of the computer_use screenshots (Hermes' own cache keeps only ~20)
    proof-*.mp4      rendered videos
    proof-*.json     step sidecar (what happened, when, where) — paste into a PR body
    latest           path of the newest render
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

_LOCK = threading.Lock()
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def root_dir() -> Path:
    override = os.environ.get("CYBR_PROOF_DIR")
    base = Path(override).expanduser() if override else hermes_home() / "cybr-proof"
    base.mkdir(parents=True, exist_ok=True)
    return base


def session_dir(session_id: str) -> Path:
    sid = _SAFE.sub("_", str(session_id or "sessionless"))[:96] or "sessionless"
    d = root_dir() / sid
    (d / "frames").mkdir(parents=True, exist_ok=True)
    return d


def append_event(session_id: str, event: Dict[str, Any]) -> None:
    event.setdefault("t", time.time())
    line = json.dumps(event, ensure_ascii=False, default=str)
    with _LOCK:
        with (session_dir(session_id) / "trace.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def read_events(session_id: str) -> List[Dict[str, Any]]:
    p = session_dir(session_id) / "trace.jsonl"
    if not p.exists():
        return []
    out: List[Dict[str, Any]] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def copy_frame(session_id: str, src: str) -> Optional[str]:
    """Copy a screenshot into the session's frames dir; returns the new path (None if src vanished)."""
    s = Path(src)
    if not s.is_file():
        return None
    d = session_dir(session_id) / "frames"
    with _LOCK:
        n = len([p for p in d.iterdir() if p.is_file()]) + 1
        dst = d / f"{n:04d}{s.suffix.lower() or '.png'}"
        try:
            shutil.copy2(s, dst)
        except OSError:
            return None
        try:
            os.chmod(dst, 0o600)
        except OSError:
            pass
    return str(dst)


def set_latest(session_id: str, path: str) -> None:
    (session_dir(session_id) / "latest").write_text(path, encoding="utf-8")
    (root_dir() / "latest").write_text(path, encoding="utf-8")


def get_latest(session_id: Optional[str] = None) -> Optional[str]:
    p = (session_dir(session_id) if session_id else root_dir()) / "latest"
    if not p.exists():
        return None
    v = p.read_text(encoding="utf-8").strip()
    return v if v and Path(v).exists() else None


def list_sessions() -> Iterator[Dict[str, Any]]:
    for d in sorted(root_dir().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        trace = d / "trace.jsonl"
        videos = sorted(d.glob("proof-*.mp4")) + sorted(d.glob("proof-*.gif")) + sorted(d.glob("proof-*.webm"))
        yield {
            "session_id": d.name,
            "events": sum(1 for _ in trace.open(encoding="utf-8")) if trace.exists() else 0,
            "frames": len(list((d / "frames").glob("*"))) if (d / "frames").exists() else 0,
            "videos": [str(v) for v in videos],
            "updated": d.stat().st_mtime,
        }
