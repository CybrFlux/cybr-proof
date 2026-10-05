"""pytest: recorder parsing + render pipeline (gif path needs no ffmpeg; mp4 path runs when ffmpeg exists)."""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture()
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("CYBR_PROOF_DIR", str(tmp_path / "proof"))
    import demo

    mod = demo.load_plugin()
    return mod, demo


def _capture(demo, tmp_path, name, **kw):
    p = tmp_path / f"{name}.png"
    demo.fake_screen(p, "T", {"Email": (390, 500), "Password": (390, 500)}, **kw)
    els = [(1, "AXTextField", "Email", (390, 120, 500, 44)), (2, "AXTextField", "Password", (390, 220, 500, 44)),
           (3, "AXButton", "Sign in", (550, 320, 180, 50))]
    return demo.capture_result(p, els)


def test_recorder_resolves_element_center_and_masks_secrets(plugin, tmp_path):
    mod, demo = plugin
    sid = "s1"
    ok = json.dumps({"ok": True, "action": "click", "effect": "confirmed"})
    mod.recorder.record("computer_use", {"action": "capture"}, _capture(demo, tmp_path, "a"), session_id=sid)
    mod.recorder.record("computer_use", {"action": "click", "element": 2}, ok, session_id=sid)
    mod.recorder.record("computer_use", {"action": "type", "text": "hunter2"}, ok, session_id=sid)
    mod.recorder.record("computer_use", {"action": "click", "element": 1}, ok, session_id=sid)
    mod.recorder.record("computer_use", {"action": "type", "text": "me@x.io"}, ok, session_id=sid)
    mod.recorder.record("computer_use", {"action": "type", "text": "sk-abcdefghijklmnop"}, ok, session_id=sid)
    mod.recorder.record("read_file", {"path": "x"}, "nope", session_id=sid)
    ev = mod.store.read_events(sid)
    kinds = [e["kind"] for e in ev]
    assert kinds == ["frame", "action", "action", "action", "action", "action"]
    assert ev[1]["point"] == [640.0, 242.0]          # center of Password bounds
    assert ev[2]["text"] == "•••••••"                 # typed into a password field
    assert ev[4]["text"] == "me@x.io"                 # typed into Email: kept
    assert ev[5]["text"].startswith("•")              # looks like an API key
    assert Path(ev[0]["path"]).is_file() and "frames" in ev[0]["path"]


def test_bounds_scale_applied(plugin, tmp_path):
    mod, demo = plugin
    cap = _capture(demo, tmp_path, "b")
    cap["meta"]["bounds_scale"] = 2.0
    mod.recorder.record("computer_use", {"action": "capture"}, cap, session_id="s2")
    mod.recorder.record("computer_use", {"action": "click", "element": 1}, json.dumps({"ok": True}), session_id="s2")
    assert mod.store.read_events("s2")[1]["point"] == [320.0, 71.0]


def test_render_gif_without_ffmpeg(plugin, tmp_path, monkeypatch):
    mod, demo = plugin
    monkeypatch.setattr(mod.render, "find_ffmpeg", lambda: None)
    sid = "s3"
    mod.recorder.record("computer_use", {"action": "capture"}, _capture(demo, tmp_path, "c"), session_id=sid)
    mod.recorder.record("computer_use", {"action": "click", "element": 3}, json.dumps({"ok": True}), session_id=sid)
    mod.recorder.record("computer_use", {"action": "click", "element": 1, "capture_after": True},
                        {**_capture(demo, tmp_path, "d", typed="x"), "action_result": {"ok": True}}, session_id=sid)
    res = mod.render.render(sid, scope="session", opt=mod.render.RenderOptions(fmt="gif", fps=10))
    assert res["ok"] and Path(res["video"]).stat().st_size > 1000
    assert [s["label"] for s in res["steps"]] == ["click AXButton 'Sign in'", "click AXTextField 'Email'"]
    assert Path(res["sidecar"]).exists()
    # turn scope: nothing new after a render
    assert mod.render.render(sid, scope="turn")["error"] == "nothing new to render"


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg") and not list(Path.home().glob(".hermes/tools/ffmpeg-*/bin/ffmpeg")),
                    reason="ffmpeg not available")
def test_render_mp4(plugin, tmp_path):
    mod, demo = plugin
    sid = "s4"
    mod.recorder.record("computer_use", {"action": "capture"}, _capture(demo, tmp_path, "e"), session_id=sid)
    mod.recorder.record("computer_use", {"action": "drag", "from_element": 1, "to_element": 3}, json.dumps({"ok": True}), session_id=sid)
    mod.recorder.record("computer_use", {"action": "scroll", "direction": "down", "coordinate": [600, 400]}, json.dumps({"ok": True}), session_id=sid)
    res = mod.render.render(sid, scope="session", opt=mod.render.RenderOptions(speed=3.0))
    assert res["ok"] and res["video"].endswith(".mp4") and Path(res["video"]).stat().st_size > 5000
