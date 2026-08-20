"""JSON-lines intent server over AF_VSOCK (or AF_UNIX for host-side testing).

Wire protocol, one connection = any number of request/response pairs:

    ->  {"tool": "app.launch", "args": {"id": "foot.desktop"}}\\n
    <-  {"ok": true, "result": {"id": "foot.desktop", "pid": 812}, "error": null}\\n

    ->  {"tool": "app.launch", "args": {"id": "/bin/sh"}}\\n
    <-  {"ok": false, "result": null, "error": "app.launch: 'id' value not allowed: '/bin/sh'"}\\n

Optional request fields `source` and `confidence` populate the Intent, so the
egress log V2 adds can record who proposed what. They are advisory metadata,
never authority: a caller claiming source="human" gets exactly the same
validation as one claiming nothing.

Connections are handled one thread each. Effectors are not free-threaded
(TouchSequencer serializes on its own queue; AppManager mutates a dict), so a
single lock serializes dispatch. Throughput is irrelevant here — this carries
a handful of intents per minute, spoken by a person.

That lock is per-*registry*, not per-server. Since voice moved into the guest
the daemon listens on two sockets at once (vsock for the host, a local unix
socket for musashi-voice), which means two IntentServer instances over one
registry. Each instantiating its own lock would serialize nothing across them,
so the lock is injectable and __main__ passes the same one to both. The default
still creates a private lock, so a lone server is correct with no ceremony.
"""
from __future__ import annotations

import json
import logging
import socket
import threading

from musashi_gestures.intents import Intent

log = logging.getLogger("musashi-effector.server")

MAX_LINE = 64 * 1024   # a proposed intent is a few hundred bytes; cap the rest


class IntentServer:
    def __init__(self, registry, sock: socket.socket, lock: "threading.Lock | None" = None):
        self.registry = registry
        self.sock = sock
        # Shared when several servers front one registry; see the module docstring.
        self._lock = lock if lock is not None else threading.Lock()
        self._stop = threading.Event()

    @classmethod
    def on_vsock(cls, registry, port: int, lock=None) -> "IntentServer":
        s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((socket.VMADDR_CID_ANY, port))
        s.listen(8)
        log.info("listening on vsock port %d", port)
        return cls(registry, s, lock)

    @classmethod
    def on_unix(cls, registry, path: str, lock=None) -> "IntentServer":
        """Same protocol over a unix socket. Two callers: host-side testing
        with no VM at all, and — inside the guest — the local channel that
        musashi-voice uses now that the voice loop runs in the VM."""
        import os
        import stat
        parent = os.path.dirname(path)
        if parent:
            # /run/musashi is a systemd RuntimeDirectory in the image, but the
            # daemon must also work when started by hand.
            os.makedirs(parent, exist_ok=True)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(path)
        # The socket is a capability endpoint: owner-and-group only, never
        # world-connectable, whatever umask the daemon inherited.
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)
        s.listen(8)
        log.info("listening on unix socket %s", path)
        return cls(registry, s, lock)

    # -- request handling -----------------------------------------------
    def handle_line(self, line: str) -> str:
        """Parse -> Intent -> Registry -> JSON response. Never raises."""
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            return json.dumps({"ok": False, "result": None, "error": f"bad json: {exc}"})

        if not isinstance(req, dict):
            return json.dumps({"ok": False, "result": None, "error": "request must be an object"})

        name = req.get("tool")
        if not isinstance(name, str):
            return json.dumps({"ok": False, "result": None, "error": "missing 'tool'"})

        args = req.get("args", {})
        if not isinstance(args, dict):
            return json.dumps({"ok": False, "result": None, "error": "'args' must be an object"})

        source = req.get("source")
        confidence = req.get("confidence")
        intent = Intent(
            name=name,
            args=args,
            source=source if isinstance(source, str) else "vsock",
            confidence=float(confidence) if isinstance(confidence, (int, float)) else 1.0,
        )

        with self._lock:
            res = self.registry.dispatch(intent)

        # Every proposal is logged, accepted or not — this line is the seed of
        # the append-only egress log V2 formalizes.
        log.info("intent %s -> %s", intent, "ok" if res.ok else f"REJECTED: {res.error}")
        try:
            return json.dumps(res.as_dict())
        except (TypeError, ValueError):
            # A handler returned something unserializable — report success
            # honestly, but don't take the connection down over it.
            return json.dumps({"ok": res.ok, "result": str(res.result), "error": res.error})

    # -- loop ------------------------------------------------------------
    def serve_forever(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self.sock.accept()
            except OSError:
                if self._stop.is_set():
                    return
                raise
            threading.Thread(target=self._serve_conn, args=(conn, addr), daemon=True).start()

    def _serve_conn(self, conn: socket.socket, addr) -> None:
        log.debug("connection from %s", addr)
        try:
            with conn, conn.makefile("rwb") as f:
                for raw in f:
                    if len(raw) > MAX_LINE:
                        f.write(b'{"ok": false, "result": null, "error": "line too long"}\n')
                        f.flush()
                        return
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    f.write(self.handle_line(line).encode() + b"\n")
                    f.flush()
        except OSError as exc:
            log.debug("connection %s ended: %s", addr, exc)

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
