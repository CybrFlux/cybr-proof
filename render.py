"""Cybr Proof — renderer. Frames + action trace -> mp4/gif/webm with an animated ghost cursor.

Pure Pillow compositing piped into ffmpeg as raw RGB. No OpenCV, no headless browser.
"""

from __future__ import annotations

import glob
import json
import math
import os
import random
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import store

ACCENT = (34, 211, 238)        # Cybr cyan
ACCENT_DIM = (34, 211, 238, 110)
INK = (12, 14, 18)
FPS = 30


@dataclass
class RenderOptions:
    fps: int = FPS
    fmt: str = "mp4"                 # mp4 | gif | webm
    max_width: int = 1600            # downscale wide captures; keeps files small
    hold: float = 0.55               # seconds a frame rests with no actions
    move_min: float = 0.35
    move_max: float = 0.9
    click_hold: float = 0.45
    caption_hold: float = 0.9
    hud: bool = True
    watermark: str = "CYBR PROOF"
    cursor_scale: float = 1.0
    seed: int = 7
    speed: float = 1.0               # >1 = faster video


# ── fonts ──────────────────────────────────────────────────────────────────────
_FONT_CANDIDATES = [
    "/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/TTF/Inter-Regular.ttf", "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Helvetica.ttc", "C:/Windows/Fonts/segoeui.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for c in _FONT_CANDIDATES:
        if os.path.exists(c):
            try:
                return ImageFont.truetype(c, size)
            except OSError:
                continue
    for pat in ("/usr/share/fonts/**/*Sans*-Regular.ttf", "/usr/share/fonts/**/*Sans*.ttf"):
        hits = sorted(glob.glob(pat, recursive=True))
        if hits:
            try:
                return ImageFont.truetype(hits[0], size)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


# ── ffmpeg ─────────────────────────────────────────────────────────────────────
def find_ffmpeg() -> Optional[str]:
    env = os.environ.get("FFMPEG_PATH")
    if env and Path(env).exists():
        return env
    try:
        from tools.transcription_audio import _find_ffmpeg_binary  # type: ignore

        found = _find_ffmpeg_binary()
        if found:
            return found
    except Exception:
        pass
    if w := shutil.which("ffmpeg"):
        return w
    hits = sorted(glob.glob(str(store.hermes_home() / "tools" / "ffmpeg-*" / "bin" / "ffmpeg*")))
    return hits[-1] if hits else None


# ── easing / paths (the "ghost cursor" bit) ────────────────────────────────────
def _ease(t: float) -> float:
    # ease-in-out cubic with a tiny overshoot-free settle
    return 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2


def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return (u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1])


def ghost_path(a: Tuple[float, float], b: Tuple[float, float], n: int, rng: random.Random) -> List[Tuple[float, float]]:
    """Human-ish curved path from a to b with n samples (eased)."""
    if n <= 1:
        return [b]
    dx, dy = b[0] - a[0], b[1] - a[1]
    dist = math.hypot(dx, dy) or 1.0
    # perpendicular bulge proportional to distance, random side
    nx, ny = -dy / dist, dx / dist
    bulge = min(120.0, dist * 0.18) * rng.choice((-1, 1))
    c1 = (a[0] + dx * 0.3 + nx * bulge, a[1] + dy * 0.3 + ny * bulge)
    c2 = (a[0] + dx * 0.7 + nx * bulge * 0.5, a[1] + dy * 0.7 + ny * bulge * 0.5)
    return [_bezier(a, c1, c2, b, _ease(i / (n - 1))) for i in range(n)]


# ── drawing ────────────────────────────────────────────────────────────────────
_CURSOR = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]  # classic arrow, 20px tall


def draw_cursor(canvas: Image.Image, x: float, y: float, scale: float = 1.0, pressed: bool = False) -> None:
    s = 1.35 * scale
    pts = [(x + px * s, y + py * s) for px, py in _CURSOR]
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    r = 16 * scale
    g.ellipse((x - r, y - r, x + r, y + r), fill=(ACCENT[0], ACCENT[1], ACCENT[2], 70 if not pressed else 120))
    glow = glow.filter(ImageFilter.GaussianBlur(6 * scale))
    canvas.alpha_composite(glow)
    d = ImageDraw.Draw(canvas)
    d.polygon(pts, fill=(255, 255, 255, 255) if not pressed else ACCENT + (255,), outline=INK + (255,))
    d.line(pts + [pts[0]], fill=INK + (255,), width=max(1, int(1.5 * scale)))


def draw_ripple(canvas: Image.Image, x: float, y: float, t: float, scale: float = 1.0) -> None:
    """t in [0,1]: expanding ring fading out."""
    if t >= 1:
        return
    r = (8 + 34 * t) * scale
    a = int(220 * (1 - t))
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse((x - r, y - r, x + r, y + r), outline=ACCENT + (a,), width=max(2, int(3 * scale)))
    canvas.alpha_composite(layer)


_font_scale = [1.0]


def draw_chip(canvas: Image.Image, text: str, anchor: str = "bottom", font_size: int = 18) -> None:
    W, H = canvas.size
    font_size = int(font_size * _font_scale[0])
    f = _font(font_size)
    pad = int(10 * _font_scale[0])
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    bbox = d.textbbox((0, 0), text, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    w, h = tw + pad * 2, th + pad * 2
    if anchor == "bottom":
        x0, y0 = (W - w) // 2, H - h - 22
    elif anchor == "topleft":
        x0, y0 = 16, 14
    elif anchor == "bottomleft":
        x0, y0 = 16, H - h - 22
    elif anchor == "bottomright":
        x0, y0 = W - w - 16, H - h - 22
    else:
        x0, y0 = W - w - 16, 14
    d.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=8, fill=(12, 14, 18, 200), outline=ACCENT + (140,), width=1)
    d.text((x0 + pad - bbox[0], y0 + pad - bbox[1]), text, font=f, fill=(240, 244, 248, 255))
    canvas.alpha_composite(layer)


def draw_scroll_hint(canvas: Image.Image, x: float, y: float, direction: str, t: float) -> None:
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    off = 26 * math.sin(t * math.pi)
    dx, dy = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}.get(direction, (0, 1))
    cx, cy = x + dx * off, y + dy * off
    d.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=ACCENT + (200,))
    canvas.alpha_composite(layer)


# ── timeline → frames ──────────────────────────────────────────────────────────
@dataclass
class Scene:
    image_path: str
    actions: List[Dict[str, Any]] = field(default_factory=list)


def build_scenes(events: Sequence[Dict[str, Any]], start: int = 0, skip_actions_before: int = 0) -> List[Scene]:
    """Group events into (frame, actions-until-next-frame). Actions before the first frame are dropped."""
    scenes: List[Scene] = []
    for i, ev in enumerate(list(events)[start:], start=start):
        if ev.get("kind") == "frame" and ev.get("path") and Path(ev["path"]).is_file():
            scenes.append(Scene(ev["path"]))
        elif ev.get("kind") == "action" and scenes and i >= skip_actions_before:
            scenes[-1].actions.append(ev)
    return scenes


def _describe(ev: Dict[str, Any]) -> str:
    a = ev.get("action")
    lab = f" {ev['label']}" if ev.get("label") else (f" #{ev['element']}" if ev.get("element") is not None else "")
    if a in ("click", "double_click", "right_click", "middle_click"):
        return f"{a.replace('_', ' ')}{lab}"
    if a == "type":
        t = ev.get("text", "")
        t = t if len(t) <= 48 else t[:45] + "…"
        return f"type  “{t}”"
    if a == "key":
        return f"key  {ev.get('keys', '')}"
    if a == "scroll":
        return f"scroll {ev.get('direction', '')} ×{ev.get('amount', 3)}"
    if a == "drag":
        return "drag"
    if a == "set_value":
        return f"set{lab} = {ev.get('value', '')}"
    if a == "wait":
        return f"wait {ev.get('seconds', 0.5)}s"
    return str(a)


class _Sink:
    """Feeds RGB frames into ffmpeg (or collects PIL frames for GIF)."""

    def __init__(self, out: Path, size: Tuple[int, int], opt: RenderOptions, ffmpeg: Optional[str]):
        self.out, self.size, self.opt, self.n = out, size, opt, 0
        self.gif_frames: List[Image.Image] = []
        self.proc = None
        if opt.fmt in ("mp4", "webm"):
            if not ffmpeg:
                raise RuntimeError("ffmpeg not found (set FFMPEG_PATH or run `hermes setup` to fetch it)")
            w, h = size
            if opt.fmt == "mp4":
                codec = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "22", "-movflags", "+faststart"]
            else:
                codec = ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "34", "-pix_fmt", "yuv420p", "-row-mt", "1"]
            cmd = [ffmpeg, "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                   "-r", str(opt.fps), "-i", "-", *codec, str(out)]
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def push(self, frame: Image.Image) -> None:
        self.n += 1
        if self.proc is not None:
            assert self.proc.stdin is not None
            self.proc.stdin.write(frame.convert("RGB").tobytes())
        else:  # gif: keep every 3rd frame at ~10fps to stay small
            if self.n % 3 == 1:
                self.gif_frames.append(frame.convert("RGB").quantize(colors=128, method=Image.Quantize.FASTOCTREE))

    def close(self) -> None:
        if self.proc is not None:
            assert self.proc.stdin is not None
            self.proc.stdin.close()
            err = self.proc.stderr.read().decode("utf-8", "ignore") if self.proc.stderr else ""
            if self.proc.wait() != 0:
                raise RuntimeError(f"ffmpeg failed: {err.strip()[:500]}")
        elif self.gif_frames:
            self.gif_frames[0].save(self.out, save_all=True, append_images=self.gif_frames[1:],
                                    duration=int(3000 / self.opt.fps), loop=0, optimize=True)


def render(session_id: str, *, scope: str = "turn", opt: Optional[RenderOptions] = None,
           out_path: Optional[str] = None, events: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Render a proof video. ``scope``: 'turn' = events since the last render, 'session' = everything."""
    opt = opt or RenderOptions()
    events = events if events is not None else store.read_events(session_id)
    sdir = store.session_dir(session_id)
    marker = sdir / "rendered_upto"
    start = 0
    marker_idx = 0
    if scope == "turn" and marker.exists():
        try:
            start = int(marker.read_text().strip() or 0)
        except ValueError:
            start = 0
        start = min(start, len(events))
        marker_idx = start
        # a turn may begin with actions on the frame from the previous turn — back up to include that frame
        while start > 0 and events[start - 1].get("kind") != "frame":
            start -= 1
        if start > 0:
            start -= 1
    scenes = build_scenes(events, start, skip_actions_before=marker_idx)
    if not scenes:
        return {"ok": False, "error": "no frames to render", "session_id": session_id, "events": len(events)}
    if scope == "turn" and marker_idx > 0:
        new_frames = sum(1 for e in events[marker_idx:] if e.get("kind") == "frame")
        if not any(s.actions for s in scenes) and new_frames < 2:
            return {"ok": False, "error": "nothing new to render", "session_id": session_id, "events": len(events)}

    first = Image.open(scenes[0].image_path)
    W, H = first.size
    scale = min(1.0, opt.max_width / W) if W > opt.max_width else 1.0
    W2, H2 = int(W * scale) // 2 * 2, int(H * scale) // 2 * 2  # even dims for yuv420p
    ffmpeg = find_ffmpeg()
    ui = max(1.0, H2 / 900.0)  # HiDPI captures: keep cursor + chips legible
    opt.cursor_scale *= ui
    _font_scale[0] = ui
    ext = {"mp4": ".mp4", "gif": ".gif", "webm": ".webm"}[opt.fmt]
    out = Path(out_path) if out_path else sdir / f"proof-{time.strftime('%Y%m%d-%H%M%S')}{ext}"
    sink = _Sink(out, (W2, H2), opt, ffmpeg)
    rng = random.Random(opt.seed)
    fps = opt.fps
    spd = max(0.2, opt.speed)

    def secs(s: float) -> int:
        return max(1, int(round(s * fps / spd)))

    total_steps = sum(len(s.actions) for s in scenes)
    step_no = 0
    pointer: Tuple[float, float] = (W2 * 0.5, H2 * 0.6)
    steps_log: List[Dict[str, Any]] = []
    t0 = time.time()

    def base(scene: Scene) -> Image.Image:
        im = Image.open(scene.image_path).convert("RGBA")
        if im.size != (W2, H2):
            im = im.resize((W2, H2), Image.Resampling.LANCZOS)
        return im

    def hud(canvas: Image.Image, label: Optional[str]) -> None:
        if not opt.hud:
            return
        if opt.watermark:
            draw_chip(canvas, opt.watermark, anchor="bottomright", font_size=14)
        if label:
            draw_chip(canvas, f"{step_no}/{total_steps}  ·  {label}", anchor="bottomleft", font_size=16)

    def emit(canvas_fn, n: int) -> None:
        for i in range(n):
            sink.push(canvas_fn(i / max(1, n - 1) if n > 1 else 1.0))

    for si, scene in enumerate(scenes):
        bg = base(scene)
        if not scene.actions:
            # rest frame — show the cursor where it was
            def still(_t, bg=bg):
                c = bg.copy()
                draw_cursor(c, *pointer, opt.cursor_scale)
                hud(c, None)
                return c
            emit(still, secs(opt.hold if si < len(scenes) - 1 else opt.hold * 1.6))
            continue

        for ev in scene.actions:
            step_no += 1
            label = _describe(ev)
            steps_log.append({"step": step_no, "t": ev.get("t"), "action": ev.get("action"), "label": label,
                              "ok": ev.get("ok"), "effect": ev.get("effect")})
            a = ev.get("action")
            target = ev.get("point")
            if target:
                target = (float(target[0]) * scale, float(target[1]) * scale)

            if a in ("click", "double_click", "right_click", "middle_click", "set_value") and target:
                path = ghost_path(pointer, target, secs(_move_dur(pointer, target, opt)), rng)
                for p in path:
                    c = bg.copy(); draw_cursor(c, *p, opt.cursor_scale); hud(c, label); sink.push(c)
                pointer = target
                clicks = 2 if a == "double_click" else 1
                for k in range(clicks):
                    n = secs(opt.click_hold)
                    for i in range(n):
                        t = i / max(1, n - 1)
                        c = bg.copy()
                        draw_ripple(c, *pointer, t, opt.cursor_scale)
                        draw_cursor(c, *pointer, opt.cursor_scale, pressed=t < 0.3)
                        hud(c, label)
                        sink.push(c)
            elif a == "scroll":
                if target:
                    for p in ghost_path(pointer, target, secs(_move_dur(pointer, target, opt)), rng):
                        c = bg.copy(); draw_cursor(c, *p, opt.cursor_scale); hud(c, label); sink.push(c)
                    pointer = target
                n = secs(opt.caption_hold)
                for i in range(n):
                    c = bg.copy()
                    draw_scroll_hint(c, *pointer, str(ev.get("direction") or "down"), i / max(1, n - 1))
                    draw_cursor(c, *pointer, opt.cursor_scale)
                    hud(c, label); draw_chip(c, label); sink.push(c)
            elif a == "drag" and ev.get("from") and ev.get("to"):
                f = (ev["from"][0] * scale, ev["from"][1] * scale)
                to = (ev["to"][0] * scale, ev["to"][1] * scale)
                for p in ghost_path(pointer, f, secs(_move_dur(pointer, f, opt)), rng):
                    c = bg.copy(); draw_cursor(c, *p, opt.cursor_scale); hud(c, label); sink.push(c)
                for p in ghost_path(f, to, secs(_move_dur(f, to, opt) * 1.3), rng):
                    c = bg.copy()
                    ImageDraw.Draw(c).line([f, p], fill=ACCENT + (160,), width=3)
                    draw_cursor(c, *p, opt.cursor_scale, pressed=True); hud(c, label); sink.push(c)
                pointer = to
                emit(lambda _t, bg=bg: _with(bg, lambda c: (draw_ripple(c, *pointer, _t, opt.cursor_scale),
                                                            draw_cursor(c, *pointer, opt.cursor_scale), hud(c, label))),
                     secs(opt.click_hold))
            else:  # type / key / wait / set_value-without-geometry / unresolved click
                n = secs(opt.caption_hold if a != "wait" else min(2.0, float(ev.get("seconds") or 0.5)))
                for i in range(n):
                    c = bg.copy()
                    draw_cursor(c, *pointer, opt.cursor_scale)
                    hud(c, label)
                    if a in ("type", "key", "set_value") or (a and a.endswith("click")):
                        draw_chip(c, label)
                    sink.push(c)

        # settle on the frame before cutting to the next state
        emit(lambda _t, bg=bg: _with(bg, lambda c: (draw_cursor(c, *pointer, opt.cursor_scale), hud(c, None))),
             secs(opt.hold * 0.6))

    sink.close()
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    marker.write_text(str(len(events)), encoding="utf-8")
    sidecar = out.with_suffix(".json")
    meta = {
        "session_id": session_id, "video": str(out), "format": opt.fmt, "fps": fps, "frames": sink.n,
        "duration_s": round(sink.n / fps, 2), "size": [W2, H2], "scenes": len(scenes), "steps": steps_log,
        "render_seconds": round(time.time() - t0, 2), "scope": scope,
    }
    sidecar.write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    store.set_latest(session_id, str(out))
    return {"ok": True, **meta, "sidecar": str(sidecar)}


def _with(bg: Image.Image, fn) -> Image.Image:
    c = bg.copy()
    fn(c)
    return c


def _move_dur(a: Tuple[float, float], b: Tuple[float, float], opt: RenderOptions) -> float:
    d = math.hypot(b[0] - a[0], b[1] - a[1])
    return max(opt.move_min, min(opt.move_max, opt.move_min + d / 1400.0))


def steps_markdown(meta: Dict[str, Any]) -> str:
    lines = [f"**Cybr Proof** — {meta.get('scenes')} screens, {len(meta.get('steps', []))} actions, "
             f"{meta.get('duration_s')}s · `{meta.get('video')}`", ""]
    for s in meta.get("steps", []):
        flag = "" if s.get("ok") in (True, None) else " ⚠️"
        eff = f" _({s['effect']})_" if s.get("effect") and s["effect"] != "confirmed" else ""
        lines.append(f"{s['step']}. {s['label']}{eff}{flag}")
    return "\n".join(lines)
