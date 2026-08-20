#!/usr/bin/env python3
"""Host-side tests for the effector client's wire protocol — no VM, no vsock.

The socket-opening step is injectable, so the whole request/response path runs
against an in-memory stand-in for the guest daemon. What is verified is the
property the voice loop depends on: *nothing the guest does can raise*. A dead
VM, a half-open socket, a truncated line and a non-JSON reply all have to come
back as Response(ok=False), because the loop's error handling is "say it out
loud", not "traceback".

Style matches gesture-engine/tests/: plain assert + print("name: OK").

Run: python3 voice/tests/test_vsock_protocol.py
"""
import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))                      # voice/

from musashi_voice.vsock import (                          # noqa: E402
    EffectorClient, Response, decode_response, encode_request)

TOOL_TABLE = [
    {"name": "app.launch", "class": "effect", "destructive": False, "doc": "",
     "args": {"id": {"type": "str", "required": True,
                     "choices": ["foot.desktop"]}}},
    {"name": "sys.tools", "class": "query", "destructive": False, "doc": "", "args": {}},
]


class FakeEffector:
    """A duplex byte stream that answers like musashi_effector.server."""

    def __init__(self, responder=None, die_after=None):
        self.requests = []
        self.pending = []
        self.closed = False
        self.die_after = die_after          # raise OSError on the Nth write
        self.responder = responder or self._default

    def _default(self, req: dict) -> dict:
        if req.get("tool") == "sys.tools":
            return {"ok": True, "result": TOOL_TABLE, "error": None}
        if req.get("tool") == "app.launch":
            if req.get("args", {}).get("id") != "foot.desktop":
                return {"ok": False, "result": None,
                        "error": "app.launch: 'id' value not allowed"}
            return {"ok": True, "result": {"id": "foot.desktop", "pid": 812}, "error": None}
        return {"ok": False, "result": None, "error": f"unknown tool: {req.get('tool')!r}"}

    # -- stream interface -------------------------------------------------
    def write(self, data: bytes) -> int:
        if self.die_after is not None and len(self.requests) >= self.die_after:
            raise OSError("broken pipe")
        req = json.loads(data.decode())
        self.requests.append(req)
        self.pending.append((json.dumps(self.responder(req)) + "\n").encode())
        return len(data)

    def flush(self):
        pass

    def readline(self, _size=-1) -> bytes:
        return self.pending.pop(0) if self.pending else b""

    def close(self):
        self.closed = True


def _client(**kw) -> tuple[EffectorClient, list]:
    made = []

    def connect():
        stream = FakeEffector(**kw)
        made.append(stream)
        return stream

    return EffectorClient(connect=connect), made


# -- encoding -----------------------------------------------------------
def test_request_is_one_newline_terminated_json_object():
    raw = encode_request("app.launch", {"id": "foot.desktop"})
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    req = json.loads(raw)
    assert req == {"tool": "app.launch", "args": {"id": "foot.desktop"},
                   "source": "voice", "confidence": 1.0}
    print("test_request_is_one_newline_terminated_json_object: OK")


def test_missing_args_encode_as_empty_object():
    assert json.loads(encode_request("sys.tools"))["args"] == {}
    print("test_missing_args_encode_as_empty_object: OK")


def test_accented_transcripts_survive_the_wire():
    req = json.loads(encode_request("app.launch", {"id": "calculadora.desktop"},
                                    source="voz"))
    assert req["source"] == "voz"
    print("test_accented_transcripts_survive_the_wire: OK")


# -- decoding -----------------------------------------------------------
def test_decode_success_and_rejection():
    ok = decode_response('{"ok": true, "result": {"pid": 812}, "error": null}')
    assert ok.ok and ok.result == {"pid": 812} and ok.error is None
    bad = decode_response('{"ok": false, "result": null, "error": "not allowed"}')
    assert not bad.ok and bad.error == "not allowed"
    print("test_decode_success_and_rejection: OK")


def test_garbage_from_the_guest_is_a_response_not_a_crash():
    for line in (b"", b"\n", b"{not json", b"[1,2,3]", b'"hello"', b"null"):
        res = decode_response(line)
        assert isinstance(res, Response) and not res.ok, line
        assert res.error
    print("test_garbage_from_the_guest_is_a_response_not_a_crash: OK")


# -- client -------------------------------------------------------------
def test_call_roundtrip():
    client, made = _client()
    res = client.call("app.launch", {"id": "foot.desktop"})
    assert res.ok and res.result["pid"] == 812
    assert made[0].requests[0]["tool"] == "app.launch"
    print("test_call_roundtrip: OK")


def test_rejection_from_the_daemon_is_reported_not_raised():
    client, _ = _client()
    res = client.call("app.launch", {"id": "/bin/sh"})
    assert not res.ok and "not allowed" in res.error
    print("test_rejection_from_the_daemon_is_reported_not_raised: OK")


def test_source_and_confidence_travel_as_metadata():
    client, made = _client()
    client.call("app.launch", {"id": "foot.desktop"}, source="voice", confidence=0.86)
    sent = made[0].requests[0]
    assert sent["source"] == "voice" and abs(sent["confidence"] - 0.86) < 1e-9
    print("test_source_and_confidence_travel_as_metadata: OK")


def test_connection_is_reused_across_calls():
    client, made = _client()
    client.call("sys.tools")
    client.call("sys.tools")
    assert len(made) == 1, "each utterance must not open a new socket"
    print("test_connection_is_reused_across_calls: OK")


def test_unreachable_guest_degrades_to_a_response():
    def connect():
        raise OSError("No such device")

    res = EffectorClient(connect=connect).call("app.launch", {"id": "foot.desktop"})
    assert not res.ok and "unreachable" in res.error
    print("test_unreachable_guest_degrades_to_a_response: OK")


def test_a_dead_socket_reconnects_exactly_once():
    client, made = _client(die_after=0)
    res = client.call("sys.tools")
    assert not res.ok and "failed" in res.error
    assert len(made) == 2, f"expected one silent retry, got {len(made)} connects"
    print("test_a_dead_socket_reconnects_exactly_once: OK")


def test_tools_returns_the_live_table():
    client, _ = _client()
    names = [t["name"] for t in client.tools()]
    assert names == ["app.launch", "sys.tools"]
    print("test_tools_returns_the_live_table: OK")


def test_tools_returns_empty_when_the_guest_is_down():
    def connect():
        raise OSError("no vsock")

    assert EffectorClient(connect=connect).tools() == []
    print("test_tools_returns_empty_when_the_guest_is_down: OK")


def test_tools_filters_junk_entries():
    client, _ = _client(responder=lambda req: {
        "ok": True, "result": ["nonsense", {"no_name": 1}, {"name": "app.launch"}],
        "error": None})
    assert [t["name"] for t in client.tools()] == ["app.launch"]
    print("test_tools_filters_junk_entries: OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("all tests passed")
