"""Four-layer config: packaged voice.toml -> /etc -> user file -> environment.

Same tomllib merge the gesture engine and the effector use, plus a user layer
and an env layer, because the two knobs that change most often while iterating
(`stt.model`, `vsock.cid`) are exactly the ones you want to override for a
single run without editing a file.

`/etc/musashi/voice.toml` sits between the packaged defaults and the user file
for the same reason effector.toml has a system layer: inside the guest this
package runs as a system-wide install with no meaningful $HOME to configure,
and the guest's answers (`stt.model = "small"` on CPU, the local unix socket,
the image's Piper voice path) are properties of the machine, not of a user.
On the host the file simply does not exist and nothing changes.

The merge is section-by-section and the env layer is key-by-key; a value's
type is taken from the packaged default, so MUSASHI_VOICE_VSOCK_CID=4 arrives
as an int and MUSASHI_VOICE_INTENT_MIN_SCORE=85 as a float.
"""
from __future__ import annotations

import importlib.resources
import logging
import os
import pathlib
import tomllib

log = logging.getLogger("musashi-voice.config")

SYSTEM_CONFIG = pathlib.Path("/etc/musashi/voice.toml")
USER_CONFIG = pathlib.Path.home() / ".config" / "musashi" / "voice.toml"
ENV_PREFIX = "MUSASHI_VOICE_"


def _packaged() -> dict:
    data = importlib.resources.files("musashi_voice").joinpath("voice.toml").read_bytes()
    return tomllib.loads(data.decode())


def _coerce(default, raw: str):
    """Coerce an env string to the type of the packaged default."""
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, list):
        return [p.strip() for p in raw.split(",") if p.strip()]
    return raw


def apply_env(cfg: dict, environ=None) -> dict:
    """Overlay MUSASHI_VOICE_<SECTION>_<KEY> onto an already-merged config."""
    environ = os.environ if environ is None else environ
    for section, values in cfg.items():
        if not isinstance(values, dict):
            continue
        for key, default in values.items():
            var = f"{ENV_PREFIX}{section.upper()}_{key.upper()}"
            if var in environ:
                try:
                    values[key] = _coerce(default, environ[var])
                except ValueError:
                    log.warning("ignoring %s=%r: not a %s",
                                var, environ[var], type(default).__name__)
    return cfg


def _merge_file(cfg: dict, path: pathlib.Path | str, label: str) -> None:
    """Overlay one toml file onto cfg, section by section. Missing is fine."""
    try:
        with open(path, "rb") as f:
            for section, values in tomllib.load(f).items():
                cfg.setdefault(section, {}).update(values)
        log.info("loaded %s config %s", label, path)
    except FileNotFoundError:
        pass
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("ignoring %s config %s: %s", label, path, exc)


def load_config(user_path: pathlib.Path | str | None = USER_CONFIG,
                environ=None,
                system_path: pathlib.Path | str | None = SYSTEM_CONFIG) -> dict:
    cfg = _packaged()
    if system_path is not None:
        _merge_file(cfg, system_path, "system")
    if user_path is not None:
        _merge_file(cfg, user_path, "user")
    return apply_env(cfg, environ)


def expand_path(value: str) -> str:
    """`~/.local/share/piper/x.onnx` -> absolute. Empty stays empty."""
    return str(pathlib.Path(value).expanduser()) if value else ""
