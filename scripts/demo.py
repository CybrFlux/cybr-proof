"""Synthetic end-to-end demo: fakes a computer_use session (the exact result shapes Hermes emits),
feeds it through the recorder hook, renders a video. Run:

    CYBR_PROOF_DIR=/tmp/cybr-proof-demo python scripts/demo.py [out.mp4]
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    spec = importlib.util.spec_from_file_location("cybr_proof", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cybr_proof"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def fake_screen(path: Path, title: str, fields: dict, clicked: str | None = None, typed: str = "") -> None:
    W, H = 1280, 800
    im = Image.new("RGB", (W, H), (245, 247, 250))
    d = ImageDraw.Draw(im)
    f = ImageFont.load_default(size=20) if hasattr(ImageFont, "load_default") else None
    d.rectangle((0, 0, W, 56), fill=(24, 28, 36))
    d.text((24, 16), title, fill=(230, 235, 240), font=f)
    y = 120
    for label, (x0, w) in fields.items():
        d.text((x0, y - 28), label, fill=(60, 66, 76), font=f)
        d.rounded_rectangle((x0, y, x0 + w, y + 44), radius=6, fill="white", outline=(180, 188, 200), width=2)
        if label == "Email" and typed:
            d.text((x0 + 12, y + 11), typed, fill=(20, 20, 20), font=f)
        y += 100
    bx = (W // 2 - 90, y, W // 2 + 90, y + 50)
    d.rounded_rectangle(bx, radius=8, fill=(34, 211, 238) if clicked == "btn" else (30, 110, 220))
    d.text((bx[0] + 50, bx[1] + 13), "Sign in", fill="white", font=f)
    im.save(path)


def capture_result(shot: Path, elements: list[tuple[int, str, str, tuple[int, int, int, int]]], action_result=None) -> dict:
    lines = [f"capture mode=som 1280x800 app=Demo window='Demo — Sign in'", f"{len(elements)} interactable element(s):",
             f"  (shareable screenshot saved to {shot})"]
    lines += [f"  #{i} {role} {label!r} @ {b} [Demo]" for i, role, label, b in elements]
    summary = "\n".join(lines)
    out = {"_multimodal": True, "content": [{"type": "text", "text": summary}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,"}}],
           "text_summary": summary, "meta": {"mode": "som", "width": 1280, "height": 800, "elements": len(elements), "png_bytes": 1,
                                             "screenshot_path": str(shot)}}
    if action_result:
        out["action_result"] = action_result
    return out


def main(out: str | None = None) -> None:
    mod = load_plugin()
    sid = "demo-session"
    tmp = Path(tempfile.mkdtemp(prefix="cybr-proof-shots-"))
    fields = {"Email": (390, 500), "Password": (390, 500)}
    els = [(1, "AXTextField", "Email", (390, 120, 500, 44)), (2, "AXTextField", "Password", (390, 220, 500, 44)),
           (3, "AXButton", "Sign in", (550, 320, 180, 50)), (4, "AXLink", "Forgot password?", (560, 400, 160, 20))]
    ok = {"ok": True, "action": "click", "effect": "confirmed", "verified": True, "verdict": {"next": "done"}}

    s1 = tmp / "s1.png"; fake_screen(s1, "Demo — Sign in", fields)
    mod.recorder.record("computer_use", {"action": "capture", "mode": "som", "app": "Demo"}, capture_result(s1, els), session_id=sid, turn_id="t1")
    mod.recorder.record("computer_use", {"action": "click", "element": 1}, json.dumps(ok), session_id=sid, turn_id="t1")
    mod.recorder.record("computer_use", {"action": "type", "text": "platform@cybrflux.online"}, json.dumps({**ok, "action": "type"}), session_id=sid, turn_id="t1")
    s2 = tmp / "s2.png"; fake_screen(s2, "Demo — Sign in", fields, typed="platform@cybrflux.online")
    mod.recorder.record("computer_use", {"action": "capture", "mode": "som", "app": "Demo"}, capture_result(s2, els), session_id=sid, turn_id="t1")
    mod.recorder.record("computer_use", {"action": "click", "element": 2}, json.dumps(ok), session_id=sid, turn_id="t1")
    mod.recorder.record("computer_use", {"action": "type", "text": "••••••••"}, json.dumps({**ok, "action": "type"}), session_id=sid, turn_id="t1")
    mod.recorder.record("computer_use", {"action": "scroll", "direction": "down", "amount": 2, "coordinate": [640, 500]},
                        json.dumps({**ok, "action": "scroll"}), session_id=sid, turn_id="t1")
    s3 = tmp / "s3.png"; fake_screen(s3, "Demo — Sign in", fields, clicked="btn", typed="platform@cybrflux.online")
    # click with capture_after=True -> capture envelope carrying action_result
    mod.recorder.record("computer_use", {"action": "click", "element": 3, "capture_after": True},
                        capture_result(s3, els, action_result=ok), session_id=sid, turn_id="t1")
    mod.recorder.record("computer_use", {"action": "key", "keys": "return"}, json.dumps({**ok, "action": "key"}), session_id=sid, turn_id="t1")
    s4 = tmp / "s4.png"; fake_screen(s4, "Demo — Dashboard ✓", {"Search": (390, 500)})
    mod.recorder.record("computer_use", {"action": "capture", "mode": "som", "app": "Demo"}, capture_result(s4, [(1, "AXTextField", "Search", (390, 120, 500, 44))]), session_id=sid, turn_id="t1")

    res = mod.render.render(sid, scope="session", opt=mod.render.RenderOptions(fmt=os.environ.get("FMT", "mp4")), out_path=out)
    print(json.dumps({k: v for k, v in res.items() if k != "steps"}, indent=1))
    print(mod.render.steps_markdown(res))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
