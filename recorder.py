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
    return _load_elements_full(path)[0]


def _load_elements_full(path: Optional[str]) -> Tuple[Dict[int, Tuple[int, int, int, int]], Dict[int, str]]:
    """(bounds, 'role label') from the full element spill file — untruncated, includes the window frame."""
    if not path:
        return {}, {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        out, labels = {}, {}
        for e in data.get("elements", []):
            b = e.get("bounds") or [0, 0, 0, 0]
            idx = int(e["index"])
            labels[idx] = f"{e.get('role', '')} {str(e.get('label', ''))[:60]!r}"
            if any(b):
                out[idx] = tuple(int(v) for v in b)  # type: ignore[assignment]
        return out, labels
    except Exception:
        return {}, {}


def _apply_capture_state(st: Dict[str, Any], cap: Dict[str, Any]) -> Dict[int, Tuple[int, int, int, int]]:
    elements, labels = _load_elements_full(cap.get("elements_file"))
    if not elements:
        elements, labels = cap["elements"], cap.get("labels") or {}
    st["bounds_scale"] = cap.get("bounds_scale")
    st["size"] = (cap.get("width"), cap.get("height"))
    st["app"], st["window_title"] = cap.get("app"), cap.get("window_title")
    if elements:
        st["elements"], st["labels"] = elements, labels
        st["frame"] = _detect_frame(elements, labels, st["size"])
    return elements


def _capture_fields(result: Any) -> Optional[Dict[str, Any]]:
    """Normalise a capture result (multimodal envelope or JSON text) -> dict, or None when not a capture."""
    if isinstance(result, dict) and result.get("_multimodal"):
        meta = result.get("meta") or {}
        text = result.get("text_summary") or ""
        shot = meta.get("screenshot_path") or (m.group(1) if (m := _SCREENSHOT_NOTE.search(text)) else None)
        head = _CAPTURE_HEAD.search(text)
        first = text.split("\n", 1)[0]
        app_m, title_m = re.search(r" app=(\S+)", first), re.search(r" window='(.*)'$", first)
        return {
            "app": app_m.group(1) if app_m else None, "window_title": title_m.group(1) if title_m else None,
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
            "text": data.get("summary", ""), "app": data.get("app"), "window_title": data.get("window_title"),
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


_FRAME_ROLES = {"frame", "window", "axwindow", "dialog", "application"}


def _detect_frame(elements: Dict[int, Tuple[int, int, int, int]], roles: Dict[int, str],
                  img: Tuple[Optional[int], Optional[int]]) -> Optional[Tuple[int, int, int, int]]:
    """The captured window's rect in element (screen) space. On Wayland/HiDPI element bounds are screen-logical
    while the screenshot is window-physical, so clicks must be mapped relative to this rect."""
    if not elements:
        return None
    for idx in sorted(elements):
        role = (roles.get(idx) or "").split(" ", 1)[0].lower()
        if role in _FRAME_ROLES:
            return elements[idx]
    # fallback: the element whose aspect ratio matches the screenshot and encloses the most others
    W, H = img
    if W and H:
        best = None
        for b in elements.values():
            x, y, w, h = b
            if w <= 0 or h <= 0:
                continue
            ratio_err = abs((w / h) - (W / H))
            if ratio_err < 0.05 and (best is None or w * h > best[2] * best[3]):
                best = b
        return best
    return None


def _to_image_space(st: Dict[str, Any], x: float, y: float) -> Tuple[float, float]:
    """Element/screen coordinates -> screenshot pixels."""
    frame, img = st.get("frame"), st.get("size") or (None, None)
    if frame and img[0] and img[1] and frame[2] > 0 and frame[3] > 0:
        fx, fy, fw, fh = frame
        return (x - fx) * (img[0] / fw), (y - fy) * (img[1] / fh)
    scale = st.get("bounds_scale")
    if scale and 0 < scale <= 4:  # Hermes' heuristic; anything larger is a stray off-screen element, not HiDPI
        return x / scale, y / scale
    return x, y


def _center(b: Tuple[int, int, int, int], st: Dict[str, Any]) -> Tuple[float, float]:
    x, y, w, h = b
    return _to_image_space(st, x + w / 2.0, y + h / 2.0)


def _resolve_point(st: Dict[str, Any], element: Any, coordinate: Any) -> Optional[List[float]]:
    if coordinate and isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2 and coordinate[0] is not None:
        cx, cy = float(coordinate[0]), float(coordinate[1])
        img, frame = st.get("size") or (None, None), st.get("frame")
        # documented as screenshot-relative, but models follow the "native coordinates" note when present:
        # a point outside the screenshot yet inside the window rect is in screen space -> map it
        if img[0] and img[1] and (cx > img[0] or cy > img[1]) and frame and \
                frame[0] <= cx <= frame[0] + frame[2] and frame[1] <= cy <= frame[1] + frame[3]:
            return list(_to_image_space(st, cx, cy))
        return [cx, cy]
    if element is not None:
        b = st["elements"].get(int(element))
        if b:
            return list(_center(b, st))
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
                ev["value"] = _mask_typed({**st, "last_label": ev.get("label") or ""}, str(args.get("value", "")))[:80]
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
        elements = _apply_capture_state(st, cap)
        if copied:
            store.append_event(sid, {
                "t": now + 0.001, "kind": "frame", "path": copied, "width": cap.get("width"), "height": cap.get("height"),
                "bounds_scale": cap.get("bounds_scale"), "frame": st.get("frame"), "turn_id": turn_id, "app": args.get("app"),
                "elements": len(elements), "after": action if action != "capture" else None,
            })
            st["dirty"] = True
    elif cap is not None and action in _INPUT_ACTIONS | {"capture"} and cap.get("elements") is not None:
        # image-less capture (cua-driver on Wayland returns the AX tree but no pixels for app windows):
        # optionally grab the composited screen once and crop it to the window frame
        elements = _apply_capture_state(st, cap)
        grabbed = _screen_fallback(sid, st) if cap.get("width") in (0, None) and SCREEN_GRAB is not None else None
        if grabbed:
            store.append_event(sid, {
                "t": now + 0.001, "kind": "frame", "path": grabbed[0], "width": grabbed[1], "height": grabbed[2],
                "bounds_scale": None, "frame": st.get("frame"), "turn_id": turn_id, "app": args.get("app"),
                "elements": len(elements), "after": action if action != "capture" else None, "source": "screen-crop",
            })
            st["size"] = (grabbed[1], grabbed[2])
            st["dirty"] = True


# ── screen-crop fallback ───────────────────────────────────────────────────────
# Set by the plugin entry point: () -> (png_path, width, height) | None, a composited full-screen grab via the
# same cua-driver session. Only used when an app capture came back without pixels.
SCREEN_GRAB: Optional[Any] = None


def _logical_screen() -> Optional[Tuple[float, float, float, float]]:
    """(x, y, logical_w, logical_h) of the focused monitor. Hyprland via hyprctl; otherwise None (assume 1:1)."""
    import shutil
    import subprocess

    if shutil.which("hyprctl"):
        try:
            mons = json.loads(subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True, timeout=3).stdout)
            mon = next((m for m in mons if m.get("focused")), mons[0] if mons else None)
            if mon and mon.get("scale"):
                s = float(mon["scale"])
                return float(mon.get("x", 0)), float(mon.get("y", 0)), mon["width"] / s, mon["height"] / s
        except Exception:
            return None
    return None


def _hypr_window_rect(app_name: str, title: str = "") -> Optional[Tuple[int, int, int, int]]:
    """Hyprland: the on-screen logical rect of the captured window (None if unknown or not on the active
    workspace — a screen grab can't contain a window that isn't shown). AT-SPI frame bounds on Wayland are
    window-local, so the compositor is the only source of the window's position."""
    import shutil
    import subprocess

    if not app_name or not shutil.which("hyprctl"):
        return None
    try:
        clients = json.loads(subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=3).stdout)
        active = json.loads(subprocess.run(["hyprctl", "activeworkspace", "-j"], capture_output=True, text=True, timeout=3).stdout)
    except Exception:
        return None
    want = app_name.lower()
    cands = [c for c in clients if c.get("mapped") and not c.get("hidden")
             and (want in str(c.get("class", "")).lower() or want in str(c.get("initialClass", "")).lower())]
    if title:
        exact = [c for c in cands if title.lower() in str(c.get("title", "")).lower()]
        cands = exact or cands
    cands = [c for c in cands if (c.get("workspace") or {}).get("id") == active.get("id")]
    if not cands:
        return None
    c = sorted(cands, key=lambda c: c.get("focusHistoryID", 99))[0]
    (x, y), (w, h) = c["at"], c["size"]
    return int(x), int(y), int(w), int(h)


def _screen_fallback(sid: str, st: Dict[str, Any]) -> Optional[Tuple[str, int, int]]:
    frame = st.get("frame")
    if frame and frame[0] == 0 and frame[1] == 0:
        # window-local frame (Wayland AT-SPI) — ask the compositor where the window actually is
        rect = _hypr_window_rect(str(st.get("app") or ""), str(st.get("window_title") or ""))
        if rect is None:
            return None
        frame = rect
        st["frame"] = (0, 0, rect[2], rect[3])  # element coords stay window-local for click mapping
    if not frame or frame[2] <= 0 or frame[3] <= 0:
        return None
    try:
        grab = SCREEN_GRAB(sid)  # type: ignore[misc]
    except Exception as e:
        logger.debug("cybr-proof screen grab failed: %s", e)
        return None
    if not grab:
        return None
    path, gw, gh = grab
    try:
        from PIL import Image

        im = Image.open(path)
        gw, gh = im.size
        mon = _logical_screen()
        if mon:
            mx, my, lw, lh = mon
            sx, sy = gw / lw, gh / lh
        else:
            mx = my = 0.0
            sx = sy = 1.0
        fx, fy, fw, fh = frame
        box = (int((fx - mx) * sx), int((fy - my) * sy), int((fx - mx + fw) * sx), int((fy - my + fh) * sy))
        box = (max(0, box[0]), max(0, box[1]), min(gw, box[2]), min(gh, box[3]))
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            return None
        crop = im.crop(box)
        d = store.session_dir(sid) / "frames"
        n = len([p for p in d.iterdir() if p.is_file()]) + 1
        out = d / f"{n:04d}.png"
        crop.save(out)
        return str(out), crop.size[0], crop.size[1]
    except Exception as e:
        logger.debug("cybr-proof screen crop failed: %s", e)
        return None
