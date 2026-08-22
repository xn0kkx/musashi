"""musashi-effector daemon entrypoint.

    python -m musashi_effector                       # vsock + local unix socket
    python -m musashi_effector --unix /tmp/eff.sock  # host-side testing, unix only
    python -m musashi_effector --no-unix             # vsock only

**Two listeners, one registry.** The voice loop moved into the guest, so its
client is now a local process rather than the host. Rather than choose, the
daemon serves both transports at once:

    vsock  VMADDR_CID_ANY:5000     the host — socat, and any out-of-VM tooling
    unix   /run/musashi/effector.sock   musashi-voice, in this same VM

Both are the same JSON-lines protocol and, critically, the same `Registry` and
the same dispatch lock: the effectors behind it are not thread-safe with
respect to each other, so two servers must not hold two locks. See the note in
server.py. Each listener runs `serve_forever()` on its own thread; SIGTERM
stops both.

Config: packaged effector.toml, overridden section-by-section by
/etc/musashi/effector.toml — the same two-layer tomllib merge the gesture
engine uses, and deliberately a different file from config.toml (see the note
in effector.toml).
"""
from __future__ import annotations

import argparse
import importlib.resources
import logging
import logging.handlers
import signal
import sys
import threading
import tomllib

from .registry import build_registry
from .server import IntentServer

log = logging.getLogger("musashi-effector")

SYSTEM_CONFIG = "/etc/musashi/effector.toml"
DEFAULT_LOG_FILE = "/tmp/musashi-effector.log"


def load_config(extra_path: str | None = None) -> dict:
    data = importlib.resources.files("musashi_effector").joinpath("effector.toml").read_bytes()
    cfg = tomllib.loads(data.decode())
    # Packaged defaults, then the system file, then an optional explicit file
    # (--config) that wins — the host dev profile that enables shell.exec
    # without needing sudo to write into /etc/musashi.
    for path in (SYSTEM_CONFIG, extra_path):
        if not path:
            continue
        try:
            with open(path, "rb") as f:
                for section, values in tomllib.load(f).items():
                    cfg.setdefault(section, {}).update(values)
        except FileNotFoundError:
            pass
    return cfg


def _setup_logging(verbose: bool, log_file: str) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if log_file:
        try:
            fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=2)
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError:
            log.warning("could not open log file %s, logging to stderr only", log_file)


def main() -> int:
    parser = argparse.ArgumentParser(prog="musashi-effector")
    parser.add_argument("--unix", metavar="PATH",
                        help="listen on a unix socket INSTEAD of vsock (host-side testing)")
    parser.add_argument("--unix-path", metavar="PATH", default=None,
                        help="override the local unix socket served alongside vsock")
    parser.add_argument("--no-unix", action="store_true",
                        help="serve vsock only, without the local unix socket")
    parser.add_argument("--config", metavar="PATH", default=None,
                        help="extra effector.toml layered on top of the system "
                             "config (host dev profile: enables [shell] without sudo)")
    parser.add_argument("--vsock-port", type=int, default=None,
                        help="override the vsock port from config")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE,
                        help="path to log file (empty string disables file logging)")
    args = parser.parse_args()

    _setup_logging(args.verbose, args.log_file)
    cfg = load_config(args.config)

    registry, apps, ui = build_registry(cfg)
    log.info("tools: %s", registry.names())
    log.info("app allowlist: %s", cfg.get("apps", {}).get("allow", []))

    # One lock for every listener: the registry's effectors are not thread-safe
    # with respect to each other, and two servers with two locks serialize
    # nothing. This is the whole reason IntentServer takes a lock at all.
    dispatch_lock = threading.Lock()
    servers: list[IntentServer] = []

    if args.unix:
        # Explicit --unix means "this socket and nothing else" — the host-side
        # no-VM test path, where binding vsock would fail anyway.
        servers.append(IntentServer.on_unix(registry, args.unix, dispatch_lock))
    else:
        port = args.vsock_port or cfg["server"]["vsock_port"]
        servers.append(IntentServer.on_vsock(registry, port, dispatch_lock))
        if not args.no_unix:
            path = args.unix_path or cfg["server"].get("unix_path", "")
            if path:
                try:
                    servers.append(IntentServer.on_unix(registry, path, dispatch_lock))
                except OSError as exc:
                    # Losing the local channel must not take down the host
                    # channel; the guest voice loop degrades, gestures do not.
                    log.error("could not open local unix socket %s: %s", path, exc)

    def stop(_sig, _frm):
        log.info("shutting down")
        for s in servers:
            s.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    threads = [threading.Thread(target=s.serve_forever, name=f"serve-{i}", daemon=True)
               for i, s in enumerate(servers[1:], start=1)]
    for t in threads:
        t.start()

    try:
        servers[0].serve_forever()
    finally:
        for s in servers:
            s.stop()
        for t in threads:
            t.join(timeout=2.0)
        ui.close()
        apps.reap_all()
        log.info("musashi-effector stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
