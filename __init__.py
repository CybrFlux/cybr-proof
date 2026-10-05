"""Cybr Proof — Hermes plugin entry point.

Hooks:
  post_tool_call   -> recorder.record (computer_use only; zero extra screenshots)
  on_session_end   -> render the turn's video in a background thread as soon as the actions end

Also registers the ``cybr_proof`` tool (render now / last / list / status), the ``/proof`` slash
command and ``hermes proof`` CLI.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, Optional

from . import recorder, render, store

logger = logging.getLogger("cybr_proof")

_RENDER_LOCKS: Dict[str, threading.Lock] = {}
_LAST_RESULT: Dict[str, Dict[str, Any]] = {}
_CFG: Dict[str, Any] = {}


def _cfg() -> Dict[str, Any]:
    """``cybr_proof:`` section of config.yaml (auto_render, format, fps, speed, max_width, watermark, hud)."""
    if _CFG:
        return _CFG
    try:
        from hermes_cli.config import load_config  # type: ignore

        _CFG.update((load_config() or {}).get("cybr_proof") or {})
    except Exception:
        pass
    return _CFG


def _options(overrides: Optional[Dict[str, Any]] = None) -> render.RenderOptions:
    c = {**_cfg(), **(overrides or {})}
    opt = render.RenderOptions()
    for k in ("fps", "max_width", "hold", "speed", "cursor_scale", "watermark", "hud"):
        if k in c and c[k] is not None:
            setattr(opt, k, type(getattr(opt, k))(c[k]))
    if c.get("format") in ("mp4", "gif", "webm"):
        opt.fmt = c["format"]
    return opt


def _render_now(session_id: str, scope: str = "turn", overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    lock = _RENDER_LOCKS.setdefault(session_id, threading.Lock())
    with lock:
        try:
            res = render.render(session_id, scope=scope, opt=_options(overrides))
        except Exception as e:  # never let a render failure touch the agent loop
            logger.warning("cybr-proof render failed: %s", e, exc_info=True)
            res = {"ok": False, "error": str(e), "session_id": session_id}
    _LAST_RESULT[session_id] = res
    if res.get("ok"):
        logger.info("cybr-proof: %s (%ss, %s steps)", res["video"], res["duration_s"], len(res["steps"]))
    return res


# ── driver self-selection (survives `hermes update`, works per profile without .env edits) ─────────
def _pick_cua_driver() -> Optional[str]:
    """Newest cua-driver >= 0.33 under ~/.local/opt (or ``cybr_proof.cua_driver`` in config.yaml).
    Hermes pins an old driver that cannot capture pixels / deliver input on Hyprland; on Linux+Wayland
    we point HERMES_CUA_DRIVER_CMD at ours unless the operator already set one."""
    import glob
    import platform

    explicit = _cfg().get("cua_driver")
    if explicit and os.path.exists(os.path.expanduser(str(explicit))):
        return os.path.expanduser(str(explicit))
    if platform.system() != "Linux" or not os.environ.get("WAYLAND_DISPLAY"):
        return None
    hits = glob.glob(os.path.expanduser("~/.local/opt/cua-driver-*/cua-driver-rs-*/cua-driver"))
    hits = [h for h in hits if os.access(h, os.X_OK)]
    if not hits:
        return None

    def ver(p: str):
        try:
            return tuple(int(x) for x in p.split("cua-driver-rs-")[1].split("-")[0].split("."))
        except Exception:
            return (0,)
    return max(hits, key=ver)


def _ensure_driver_env() -> None:
    if os.environ.get("HERMES_CUA_DRIVER_CMD") or _cfg().get("manage_driver", True) is False:
        return
    drv = _pick_cua_driver()
    if drv:
        os.environ["HERMES_CUA_DRIVER_CMD"] = drv
        os.environ.setdefault("CUA_DRIVER_RS_ENABLE_WAYLAND", "1")
        logger.info("cybr-proof: using cua-driver %s", drv)


def _ensure_hyprland_plugin() -> None:
    """Hyprland: make sure the cua compositor plugin is loaded (rebuilds after a Hyprland update)."""
    import shutil
    import subprocess

    if not shutil.which("hyprctl") or _cfg().get("manage_hyprland_plugin", True) is False:
        return
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "hyprland-plugin.sh")
    if not os.path.exists(script):
        return
    try:
        st = subprocess.run(["hyprctl", "cua:status"], capture_output=True, text=True, timeout=3).stdout
        if "transport: ready" in st:
            return
        subprocess.Popen(["bash", script, "ensure"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception as e:
        logger.debug("cybr-proof: hyprland plugin check skipped: %s", e)


# ── hooks ──────────────────────────────────────────────────────────────────────
def _screen_grab(session_id: str):
    """Composited full-screen capture through the same cua-driver session, with the post_tool_call hook
    suppressed so it never records itself. Used only when an app capture returned no pixels."""
    if not _cfg().get("screen_fallback", True):
        return None
    from model_tools import suppress_post_tool_call_hook  # type: ignore
    from tools.registry import registry  # type: ignore
    import tools.computer_use_tool  # noqa: F401  ensure the tool is registered in this process

    with suppress_post_tool_call_hook():
        res = registry.dispatch("computer_use", {"action": "capture", "app": "screen", "mode": "vision"},
                                session_id=session_id)
    meta = (res.get("meta") or {}) if isinstance(res, dict) else {}
    if not meta.get("screenshot_path"):
        try:
            data = json.loads(res) if isinstance(res, str) else {}
            meta = {"screenshot_path": data.get("screenshot_path"), "width": data.get("width"), "height": data.get("height")}
        except Exception:
            return None
    if not meta.get("screenshot_path"):
        return None
    return meta["screenshot_path"], meta.get("width"), meta.get("height")


recorder.SCREEN_GRAB = _screen_grab


def _on_post_tool_call(**kw: Any) -> None:
    try:
        recorder.record(**kw)
    except Exception as e:
        logger.debug("cybr-proof record error: %s", e, exc_info=True)


def _on_session_end(session_id: Optional[str] = None, **_: Any) -> None:
    if not session_id or not _cfg().get("auto_render", True):
        return
    st = recorder.state_for(session_id)
    if not st.get("dirty"):
        return
    st["dirty"] = False
    # non-daemon: a one-shot `hermes chat -q` must not exit before the mp4 is written (render is bounded)
    threading.Thread(target=_render_now, args=(session_id, "turn"), name="cybr-proof-render", daemon=False).start()


# ── tool ───────────────────────────────────────────────────────────────────────
_TOOL_SCHEMA = {
    "name": "cybr_proof",
    "description": (
        "Cybr Proof: the computer_use session is being recorded for free (screenshots + click targets). "
        "action='render' renders the video now (scope 'turn' = since last render, 'session' = everything) and "
        "returns its path — deliver it with MEDIA:<path>. 'last' returns the newest rendered video, 'steps' the "
        "markdown step list for a PR body, 'status' the trace counters, 'list' all recorded sessions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["render", "last", "steps", "status", "list"]},
            "scope": {"type": "string", "enum": ["turn", "session"], "description": "render only"},
            "format": {"type": "string", "enum": ["mp4", "gif", "webm"], "description": "render only (default mp4)"},
            "speed": {"type": "number", "description": "render only; 1.0 = real pacing, 2.0 = twice as fast"},
            "session_id": {"type": "string", "description": "another session's id (default: current)"},
        },
        "required": ["action"],
    },
}


def _tool(args: Dict[str, Any], session_id: Optional[str] = None, **_: Any) -> str:
    action = args.get("action")
    sid = args.get("session_id") or session_id or "sessionless"
    if action == "render":
        ov = {k: args[k] for k in ("format", "speed") if args.get(k) is not None}
        res = _render_now(sid, args.get("scope") or "turn", ov)
        if res.get("ok"):
            res["hint"] = f"deliver with: MEDIA:{res['video']}"
            res["steps_markdown"] = render.steps_markdown(res)
        return json.dumps(res, default=str)
    if action == "last":
        p = store.get_latest(sid) or store.get_latest(None)
        return json.dumps({"ok": bool(p), "video": p, "hint": f"MEDIA:{p}" if p else "nothing rendered yet",
                           **({"last_render": _LAST_RESULT.get(sid)} if sid in _LAST_RESULT else {})}, default=str)
    if action == "steps":
        meta = _LAST_RESULT.get(sid)
        if not meta or not meta.get("ok"):
            p = store.get_latest(sid)
            side = p and os.path.splitext(p)[0] + ".json"
            if side and os.path.exists(side):
                meta = json.load(open(side, encoding="utf-8"))
        return json.dumps({"ok": bool(meta), "markdown": render.steps_markdown(meta) if meta else None})
    if action == "status":
        ev = store.read_events(sid)
        return json.dumps({"ok": True, "session_id": sid, "events": len(ev),
                           "frames": sum(1 for e in ev if e.get("kind") == "frame"),
                           "actions": sum(1 for e in ev if e.get("kind") == "action"),
                           "dir": str(store.session_dir(sid)), "latest": store.get_latest(sid),
                           "ffmpeg": render.find_ffmpeg(), "auto_render": _cfg().get("auto_render", True)})
    if action == "list":
        return json.dumps({"ok": True, "sessions": list(store.list_sessions())[:30]}, default=str)
    return json.dumps({"ok": False, "error": f"unknown action {action!r}"})


# ── slash command + CLI ────────────────────────────────────────────────────────
def _slash(raw: str = "") -> str:
    arg = (raw or "").strip().lower()
    if arg.startswith("render"):
        scope = "session" if "session" in arg else "turn"
        res = _render_now(_current_session() or "sessionless", scope)
        return f"MEDIA:{res['video']}" if res.get("ok") else f"cybr-proof: {res.get('error')}"
    if arg.startswith("list"):
        rows = list(store.list_sessions())[:15]
        return "\n".join(f"{r['session_id']}  frames={r['frames']}  videos={len(r['videos'])}" for r in rows) or "no recordings"
    p = store.get_latest(_current_session()) or store.get_latest(None)
    return f"MEDIA:{p}" if p else "cybr-proof: nothing rendered yet. Usage: /proof [render [session] | list]"


def _current_session() -> Optional[str]:
    try:
        from hermes_cli import plugins as _p  # type: ignore

        agent = getattr(getattr(_p._delivery_manager(), "_cli_ref", None), "agent", None)
        return getattr(agent, "session_id", None)
    except Exception:
        return None


def _cli_setup(sub) -> None:
    sub.add_argument("verb", choices=["render", "list", "last", "steps"], nargs="?", default="list")
    sub.add_argument("session", nargs="?", help="session id (see `hermes proof list`)")
    sub.add_argument("--scope", choices=["turn", "session"], default="session")
    sub.add_argument("--format", choices=["mp4", "gif", "webm"])
    sub.add_argument("--speed", type=float)
    sub.add_argument("--out", help="output path")


def _cli(args) -> int:
    if args.verb == "list":
        for r in store.list_sessions():
            print(f"{r['session_id']:<40} frames={r['frames']:<4} events={r['events']:<5} videos={len(r['videos'])}")
        return 0
    sid = args.session or (store.list_sessions().__next__()["session_id"] if any(True for _ in store.list_sessions()) else None)
    if not sid:
        print("no recordings")
        return 1
    if args.verb == "last":
        print(store.get_latest(sid) or "nothing rendered")
        return 0
    if args.verb == "steps":
        p = store.get_latest(sid)
        side = p and os.path.splitext(p)[0] + ".json"
        if not side or not os.path.exists(side):
            print("no render yet"); return 1
        print(render.steps_markdown(json.load(open(side, encoding="utf-8"))))
        return 0
    ov = {k: v for k, v in (("format", args.format), ("speed", args.speed)) if v}
    try:
        res = render.render(sid, scope=args.scope, opt=_options(ov), out_path=args.out)
    except Exception as e:
        print(f"render failed: {e}"); return 1
    print(res["video"] if res.get("ok") else f"render failed: {res.get('error')}")
    return 0 if res.get("ok") else 1


# ── registration ───────────────────────────────────────────────────────────────
def register(ctx) -> None:  # noqa: D401 — plugin entry point
    _ensure_driver_env()
    _ensure_hyprland_plugin()
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_tool(
        name="cybr_proof", toolset="cybr-proof", schema=_TOOL_SCHEMA, handler=_tool,
        description="Render / fetch the ghost-cursor video of this session's computer_use actions", emoji="🎞️",
    )
    ctx.register_command("proof", _slash, description="Cybr Proof: latest computer_use video (render|list)",
                         args_hint="[render [session] | list]")
    try:
        ctx.register_cli_command("proof", help="Cybr Proof: render/list computer_use session videos",
                                 setup_fn=_cli_setup, handler_fn=_cli)
    except Exception as e:  # older cores without CLI command registration
        logger.debug("cybr-proof: CLI command not registered: %s", e)
    try:
        ctx.register_system_prompt_section(
            "cybr-proof.hint",
            "Cybr Proof records every computer_use action into a ghost-cursor video (no extra screenshots). "
            "When you finish a computer_use task the user may want to see, call cybr_proof(action='render') and "
            "deliver the returned video path with MEDIA:<path>; cybr_proof(action='steps') gives a PR-ready step list.",
            max_chars=600,
        )
    except Exception as e:
        logger.debug("cybr-proof: prompt section not registered: %s", e)
