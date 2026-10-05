"""Cybr Proof — recorder. Observes ``post_tool_call`` for ``computer_use`` and writes a trace.

No extra screenshots, no extra driver calls: a capture result already carries the screenshot
path + element bounds, an input result already carries the arguments the agent sent. We stitch
those into frames + pointer targets and let the renderer animate the gap.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import store

logger = logging.getLogger("cybr_proof")

# "  #7 Link 'Sign In' @ (900, 420, 80, 24) [Chrome]"
_ELEMENT_LINE = re.compile(r"^\s*#(\d+)\s+(\S+)\s+(.*?)\s+@\s+\(\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+)\s*\)", re.M)
_SCREENSHOT_NOTE = re.compile(r"shareable screenshot saved to (\S+)")
_CAPTURE_HEAD = re.compile(r"capture mode=(\w+) (\d+)x(\d+)")

_INPUT_ACTIONS = {"click", "double_click", "right_click", "middle_click", "drag", "scroll", "type", "key", "set_value", "wait"}
# per-session in-memory state: last frame geometry + element map, and whether a render is pending
_STATE: Dict[str, Dict[str, Any]] = {}


def _sid(session_id: Optional[str]) -> str:
    return str(session_id or "sessionless")


def state_for(session_id: Optional[str]) -> Dict[str, Any]:
    return _STATE.setdefault(_sid(session_id), {"elements": {}, "bounds_scale": None, "size": None,
                                                 "pointer": None, "dirty": False, "last_render_index": 0})


def _parse_elements_text(text: str) -> Dict[int, Tuple[int, int, int, int]]:
    out: Dict[int, Tuple[int, int, int, int]] = {}
    for m in _ELEMENT_LINE.finditer(text or ""):
        idx = int(m.group(1))
        x, y, w, h = (int(m.group(i)) for i in range(4, 8))
        if (x, y, w, h) != (0, 0, 0, 0):
            out[idx] = (x, y, w, h)
    return out


def _parse_labels_text(text: str) -> Dict[int, str]:
    return {int(m.group(1)): f"{m.group(2)} {m.group(3)}" for m in _ELEMENT_LINE.finditer(text or "")}


def _load_elements_file(path: Optional[str]) -> Dict[int, Tuple[int, int, int, int]]:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        out = {}
        for e in data.get("elements", []):
            b = e.get("bounds") or [0, 0, 0, 0]
            if any(b):
                out[int(e["index"])] = tuple(int(v) for v in b)  # type: ignore[assignment]
        return out
    except Exception:
        return {}


def _capture_fields(result: Any) -> Optional[Dict[str, Any]]:
    """Normalise a capture result (multimodal envelope or JSON text) -> dict, or None when not a capture."""
    if isinstance(result, dict) and result.get("_multimodal"):
        meta = result.get("meta") or {}
        text = result.get("text_summary") or ""
        shot = meta.get("screenshot_path") or (m.group(1) if (m := _SCREENSHOT_NOTE.search(text)) else None)
        head = _CAPTURE_HEAD.search(text)
        return {
            "screenshot": shot,
            "width": meta.get("width") or (int(head.group(2)) if head else None),
            "height": meta.get("height") or (int(head.group(3)) if head else None),
            "bounds_scale": meta.get("bounds_scale"),
            "elements_file": meta.get("elements_file"),
            "elements": _parse_elements_text(text),
            "labels": _parse_labels_text(text),
            "action_result": result.get("action_result"),
            "text": text,
        }
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except Exception:
            return None
        if not isinstance(data, dict) or "mode" not in data or "width" not in data:
            return None
        elements, labels = {}, {}
        for e in data.get("elements", []):
            b = e.get("bounds")
            if b and any(b):
                elements[int(e["index"])] = tuple(int(v) for v in b)
            labels[int(e["index"])] = f"{e.get('role', '')} {e.get('label', '')!r}"
        return {
            "screenshot": data.get("screenshot_path"),
            "width": data.get("width"), "height": data.get("height"),
            "bounds_scale": data.get("bounds_scale"), "elements_file": data.get("elements_file"),
            "elements": elements, "labels": labels,
            "action_result": {k: data[k] for k in ("ok", "effect", "verdict", "message", "path") if k in data} if "ok" in data else None,
            "text": data.get("summary", ""),
        }
    return None


def _action_fields(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return result.get("action_result") or {}
    if isinstance(result, str):
        try:
            data = json.loads(result)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _center(b: Tuple[int, int, int, int], scale: Optional[float]) -> Tuple[float, float]:
    x, y, w, h = b
    cx, cy = x + w / 2.0, y + h / 2.0
    if scale and scale > 0:
        cx, cy = cx / scale, cy / scale
    return cx, cy


def _resolve_point(st: Dict[str, Any], element: Any, coordinate: Any) -> Optional[List[float]]:
    if coordinate and isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2 and coordinate[0] is not None:
        return [float(coordinate[0]), float(coordinate[1])]
    if element is not None:
        b = st["elements"].get(int(element))
        if b:
            return list(_center(b, st.get("bounds_scale")))
    return None


def _label_for(st: Dict[str, Any], element: Any) -> Optional[str]:
    if element is None:
        return None
    return (st.get("labels") or {}).get(int(element))


_SECRET_FIELD = re.compile(r"pass(word|wd|code|phrase)?|secret|token|api.?key|\bpin\b|cvc|cvv|otp|2fa|verification", re.I)
_SECRET_SHAPE = re.compile(r"^(sk-|ghp_|xox[abp]-|AKIA|eyJ)[A-Za-z0-9_\-\.]{8,}")


def mask_all_typed() -> bool:
    try:
        from hermes_cli.config import load_config  # type: ignore

        return bool(((load_config() or {}).get("cybr_proof") or {}).get("mask_typed", False))
    except Exception:
        return False


def _mask_typed(st: Dict[str, Any], text: str) -> str:
    """Never let a secret into the video: mask when the last clicked field looks like a secret field,
    when the text itself looks like a key/token, or when ``cybr_proof.mask_typed`` is on."""
    if not text:
        return text
    last = str(st.get("last_label") or "")
    if mask_all_typed() or _SECRET_FIELD.search(last) or _SECRET_SHAPE.match(text.strip()):
        return "•" * min(len(text), 12)
    return text


def record(tool_name: str, args: Dict[str, Any], result: Any, *, session_id: Optional[str] = None,
           turn_id: Optional[str] = None, duration_ms: int = 0, status: Optional[str] = None, **_: Any) -> None:
    if tool_name != "computer_use" or not isinstance(args, dict):
        return
    action = str(args.get("action") or "")
    sid = _sid(session_id)
    st = state_for(sid)
    now = time.time()

    cap = _capture_fields(result)
    act = _action_fields(result) if action in _INPUT_ACTIONS else {}

    # 1) the input action itself (click/type/...). Recorded BEFORE a trailing capture_after frame so the
    #    renderer animates cursor -> target on the previous frame, then cuts to the new state.
    if action in _INPUT_ACTIONS:
        ok = act.get("ok")
        if ok is False and action not in ("wait",):
            # refused / failed input: still note it (useful in the step log) but don't move the cursor
            pass
        ev: Dict[str, Any] = {
            "t": now, "kind": "action", "action": action, "turn_id": turn_id, "duration_ms": duration_ms,
            "ok": ok, "effect": act.get("effect"), "path": act.get("path"), "message": act.get("message"),
        }
        if action in ("click", "double_click", "right_click", "middle_click", "set_value", "scroll"):
            ev["point"] = _resolve_point(st, args.get("element"), args.get("coordinate"))
            ev["element"] = args.get("element")
            ev["label"] = _label_for(st, args.get("element"))
            if action == "scroll":
                ev["direction"], ev["amount"] = args.get("direction"), args.get("amount", 3)
            if action == "set_value":
                ev["value"] = str(args.get("value", ""))[:80]
        elif action == "drag":
            ev["from"] = _resolve_point(st, args.get("from_element"), args.get("from_coordinate"))
            ev["to"] = _resolve_point(st, args.get("to_element"), args.get("to_coordinate"))
        elif action == "type":
            ev["text"] = _mask_typed(st, str(args.get("text", "")))
        elif action == "key":
            ev["keys"] = str(args.get("keys", ""))
        elif action == "wait":
            ev["seconds"] = float(args.get("seconds") or 0.5)
        if args.get("modifiers"):
            ev["modifiers"] = list(args["modifiers"])
        if ev.get("point"):
            st["pointer"] = ev["point"]
        if ev.get("label") is not None:
            st["last_label"] = ev["label"]
        store.append_event(sid, ev)
        st["dirty"] = True

    # 2) a frame (capture, or capture_after piggybacking on an input)
    if cap and cap.get("screenshot"):
        copied = store.copy_frame(sid, cap["screenshot"])
        elements = _load_elements_file(cap.get("elements_file")) or cap["elements"]
        if elements:
            st["elements"] = elements
            st["labels"] = cap.get("labels") or {}
        st["bounds_scale"] = cap.get("bounds_scale")
        st["size"] = (cap.get("width"), cap.get("height"))
        if copied:
            store.append_event(sid, {
                "t": now + 0.001, "kind": "frame", "path": copied, "width": cap.get("width"), "height": cap.get("height"),
                "bounds_scale": cap.get("bounds_scale"), "turn_id": turn_id, "app": args.get("app"),
                "elements": len(elements), "after": action if action != "capture" else None,
            })
            st["dirty"] = True
    elif cap is not None and action == "capture":
        # ax-only / unchanged-screen capture: nothing visual to add, but refresh the element map
        elements = _load_elements_file(cap.get("elements_file")) or cap["elements"]
        if elements:
            st["elements"] = elements
            st["labels"] = cap.get("labels") or {}
