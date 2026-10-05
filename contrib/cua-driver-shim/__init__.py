"""cua-driver >= 0.33 compat for Hermes computer_use.

Hermes attaches ``element_token`` to element clicks but still sends ``element_index``; newer drivers declare
``additionalProperties: false`` and refuse the call ("unknown argument element_index"). This shim wraps
``CuaBackend._action`` and drops ``element_index`` whenever a token is present and the live schema does not
list ``element_index``. No-op on drivers that still accept it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("cua_driver_shim")


def _install() -> bool:
    try:
        from tools.computer_use import cua_backend  # type: ignore
    except Exception as e:  # computer_use toolset not available
        logger.debug("cua-driver-shim: backend not importable: %s", e)
        return False
    cls = getattr(cua_backend, "CuaDriverBackend", None) or getattr(cua_backend, "CuaBackend", None)
    if cls is None or getattr(cls, "_cua_shim_installed", False):
        return cls is not None
    original = cls._action

    def _action(self, name, args, *a, **kw):
        try:
            idx = args.get("element_index") if isinstance(args, dict) else None
            token = self._snapshot_tokens.get(idx) if isinstance(idx, int) else None
            sess = self._session
            if token and not sess.supports_input_property(name, "element_index") \
                    and sess.supports_input_property(name, "element_token"):
                args = dict(args)
                args["element_token"] = token
                args.pop("element_index", None)
        except Exception as e:
            logger.debug("cua-driver-shim: passthrough (%s)", e)
        return original(self, name, args, *a, **kw)

    cls._action = _action
    cls._cua_shim_installed = True
    logger.info("cua-driver-shim: element_index -> element_token compat installed on %s", cls.__name__)
    return True


def register(ctx) -> None:  # noqa: D401
    if not _install():
        # toolset may load lazily; retry on first session start
        ctx.register_hook("on_session_start", lambda **_: _install())
