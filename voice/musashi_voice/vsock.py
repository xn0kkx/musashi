"""Host-side client for the guest's musashi-effector, over AF_VSOCK.

Wire protocol (mirror of effector/musashi_effector/server.py):

    ->  {"tool": "app.launch", "args": {"id": "foot.desktop"}, "source": "voice"}\\n
    <-  {"ok": true, "result": {"id": "foot.desktop", "pid": 812}, "error": null}\\n

Two properties this module is built around:

  * **It never raises for anything the guest can do to us.** A guest that is
    not running, a socket that dies mid-call, a truncated line, a response
    that is not JSON — all of those come back as `Response(ok=False, error=...)`,
    because "the VM is down" must degrade to the TTS saying so, not to a
    traceback in the middle of the voice loop.

  * **The proposer has no authority.** `source` and `confidence` are sent
    because the effector logs them, not because they buy anything: the guest
    validates every arg against its own allowlist regardless. See the
    effector's test_source_and_confidence_are_metadata_not_authority.

The socket-opening step is injectable (`connect=`) so the whole request /
response path is testable with an in-memory stream, and so the same client can
speak to `python -m musashi_effector --unix /tmp/eff.sock` on a plain host
with no VM at all.
"""
from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("musashi-voice.vsock")

MAX_LINE = 1024 * 1024      # sys.tools is the big one; still bound it


@dataclass(frozen=True)
class Response:
    ok: bool
    result: Any = None
    error: str | None = None

    @classmethod
    def failure(cls, msg: str) -> "Response":
        return cls(ok=False, result=None, error=msg)


def encode_request(tool: str, args: dict | None = None, source: str = "voice",
                   confidence: float = 1.0) -> bytes:
    """One JSON line, newline-terminated. Pure — no socket involved."""
    req = {"tool": tool, "args": dict(args or {}),
           "source": source, "confidence": float(confidence)}
    return (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")


def decode_response(line: bytes | str) -> Response:
    """One JSON line -> Response. Never raises."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    line = line.strip()
    if not line:
        return Response.failure("effector closed the connection")
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        return Response.failure(f"bad response json: {exc}")
    if not isinstance(payload, dict):
        return Response.failure("response was not an object")
    return Response(ok=bool(payload.get("ok")),
                    result=payload.get("result"),
                    error=payload.get("error"))


@dataclass
class EffectorClient:
    """Lazily-connected, auto-reconnecting client. One call = one line pair."""
    cid: int = 3
    port: int = 5000
    timeout_s: float = 5.0
    # Returns a binary read/write file object; swapped out by the tests and by
    # `over_unix()`. Default is a real AF_VSOCK connection to the guest.
    connect: Callable[[], Any] | None = None
    target: str = ""            # human-readable override, for logs only
    _stream: Any = field(default=None, repr=False)
    _sock: Any = field(default=None, repr=False)

    def describe(self) -> str:
        return self.target or f"vsock cid={self.cid} port={self.port}"

    # -- construction ----------------------------------------------------
    @classmethod
    def from_config(cls, cfg: dict) -> "EffectorClient":
        """The configured transport: `[vsock].unix_path` wins when set.

        Set on the host it is empty, so this is a vsock client reaching into
        the VM — unchanged. Inside the guest, /etc/musashi/voice.toml sets it to
        the effector's local socket, because voice and effector are now the same
        machine and there is no VM boundary left to cross."""
        v = cfg.get("vsock", {})
        timeout_s = float(v.get("timeout_s", 5.0))
        path = str(v.get("unix_path", "") or "")
        if path:
            return cls.over_unix(path, timeout_s)
        return cls(cid=int(v.get("cid", 3)),
                   port=int(v.get("port", 5000)),
                   timeout_s=timeout_s)

    @classmethod
    def over_unix(cls, path: str, timeout_s: float = 5.0) -> "EffectorClient":
        """Speak to `musashi-effector --unix PATH` — no VM required."""
        def _connect():
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout_s)
            s.connect(path)
            return s
        return cls(timeout_s=timeout_s, connect=_connect, target=f"unix {path}")

    # -- connection ------------------------------------------------------
    def _connect_vsock(self):
        if not hasattr(socket, "AF_VSOCK"):
            raise OSError("this kernel/python has no AF_VSOCK support")
        s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        s.settimeout(self.timeout_s)
        s.connect((self.cid, self.port))
        return s

    def _ensure_stream(self):
        if self._stream is not None:
            return self._stream
        opener = self.connect or self._connect_vsock
        obj = opener()
        # A real socket needs makefile(); a test double already is a stream.
        if hasattr(obj, "makefile"):
            self._sock, self._stream = obj, obj.makefile("rwb")
        else:
            self._sock, self._stream = None, obj
        log.info("connected to effector (%s)", self.describe())
        return self._stream

    def close(self) -> None:
        for obj in (self._stream, self._sock):
            try:
                if obj is not None:
                    obj.close()
            except OSError:
                pass
        self._stream = self._sock = None

    def __enter__(self) -> "EffectorClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- requests --------------------------------------------------------
    def call(self, tool: str, args: dict | None = None, source: str = "voice",
             confidence: float = 1.0, _retry: bool = True) -> Response:
        """Propose one intent. Returns a Response; never raises."""
        try:
            stream = self._ensure_stream()
        except (OSError, ValueError) as exc:
            log.error("effector unreachable: %s", exc)
            return Response.failure(f"effector unreachable: {exc}")

        try:
            stream.write(encode_request(tool, args, source, confidence))
            stream.flush()
            line = stream.readline(MAX_LINE)
        except (OSError, ValueError) as exc:
            self.close()
            if _retry:
                # A daemon restart between two utterances is routine; a single
                # silent reconnect is worth it, an unbounded loop is not.
                log.warning("effector call failed (%s), reconnecting once", exc)
                return self.call(tool, args, source, confidence, _retry=False)
            log.error("effector call failed: %s", exc)
            return Response.failure(f"effector call failed: {exc}")

        if not line:
            self.close()
            if _retry:
                return self.call(tool, args, source, confidence, _retry=False)
            return Response.failure("effector closed the connection")
        return decode_response(line)

    def tools(self) -> list[dict]:
        """The live tool table (`sys.tools`). Empty list if unreachable.

        Fetched at runtime, never hardcoded: the grammar is derived from this,
        so a tool added to the effector becomes speakable without touching the
        host."""
        res = self.call("sys.tools", source="voice-bootstrap")
        if not res.ok or not isinstance(res.result, list):
            log.error("could not read the tool table: %s", res.error)
            return []
        return [t for t in res.result if isinstance(t, dict) and "name" in t]
