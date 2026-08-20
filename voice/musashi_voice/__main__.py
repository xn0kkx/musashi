"""musashi-voice: the host-side voice loop, plus its push-to-talk test harness.

    python -m musashi_voice                          # PTT loop against the VM
    python -m musashi_voice --unix /tmp/eff.sock     # ... against a host effector
    python -m musashi_voice --text "abrir terminal"  # no mic, no GPU, one shot
    python -m musashi_voice --list-tools             # what the guest exposes

**Push-to-talk harness.** Enter starts recording, Enter stops it. A held key
(pynput/keyboard) was rejected for V1: both need an X/Wayland grab or root to
see key-up reliably, and this harness is scaffolding — the real PTT is the
guest's hand gesture, which will drive the very same `threading.Event` that
the second Enter sets here. Trading a dependency and a permissions problem for
a harness that gets deleted is a bad trade.

Everything after "the user stopped talking" is timed per stage, which the plan
requires from day one (Plano Diretor §8/S4: p50 <= 900 ms).
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading

from .config import expand_path, load_config
from .grammar import MIN_SCORE, Grammar
from .intent import resolve_intent
from .latency import LatencyTrace
from .tts import Speaker, confirmation_text
from .vsock import EffectorClient

log = logging.getLogger("musashi-voice")


class VoiceLoop:
    """One utterance in, one action and one spoken reply out.

    `fallback` is the LLM seam (see intent.resolve_intent). It is None for the
    whole of V1 and is threaded through here so wiring llama.cpp later touches
    this constructor and nothing else."""

    def __init__(self, cfg: dict, client: EffectorClient, grammar: Grammar,
                 transcriber=None, speaker: Speaker | None = None, fallback=None):
        self.cfg = cfg
        self.client = client
        self.grammar = grammar
        self.transcriber = transcriber
        self.speaker = speaker or Speaker(cfg.get("tts"))
        self.fallback = fallback
        self.min_score = float(cfg.get("intent", {}).get("min_score", MIN_SCORE))
        self.locale = str(cfg.get("tts", {}).get("locale", "pt"))

    # -- the half of the pipeline that needs no hardware -------------------
    def handle_transcript(self, transcript: str, trace: LatencyTrace) -> None:
        with trace.stage("intent"):
            intent = resolve_intent(transcript, self.grammar,
                                    min_score=self.min_score, fallback=self.fallback)

        if intent is None:
            reply = confirmation_text(None, locale=self.locale)
        else:
            with trace.stage("vsock"):
                res = self.client.call(intent.name, dict(intent.args),
                                       source=intent.source,
                                       confidence=intent.confidence)
            label = self.grammar.label_for(next(iter(intent.args.values()), ""))
            reply = confirmation_text(intent.name, label=label, ok=res.ok,
                                      error=res.error, locale=self.locale)
            if not res.ok:
                log.warning("effector refused %s: %s", intent.name, res.error)

        with trace.stage("tts"):
            self.speaker.say(reply)
        print(f"  reply : {reply}")

    # -- the full pipeline -------------------------------------------------
    def handle_audio(self, pcm, trace: LatencyTrace) -> None:
        from . import audio

        with trace.stage("vad"):
            pcm = audio.trim_silence(pcm, self.cfg.get("vad"),
                                     samplerate=int(self.cfg["audio"]["samplerate"]))
        with trace.stage("stt"):
            transcript = self.transcriber.transcribe(pcm)
        print(f"  heard : {transcript.text!r} [{transcript.language}]")
        if not transcript.text.strip():
            with trace.stage("tts"):
                self.speaker.say(confirmation_text(None, locale=self.locale))
            return
        self.handle_transcript(transcript.text, trace)

    def record_once(self):
        """Enter -> record -> Enter. Returns float32 PCM (possibly empty)."""
        from . import audio

        input("\n[PTT] Enter to start recording... ")
        stop = threading.Event()
        box: dict = {}

        def _record():
            try:
                box["pcm"] = audio.record_utterance(stop, self.cfg.get("audio"))
            except Exception as exc:                      # noqa: BLE001
                box["error"] = exc

        worker = threading.Thread(target=_record, daemon=True)
        worker.start()
        input("[PTT] recording — Enter to stop... ")
        stop.set()
        worker.join(timeout=5.0)
        if "error" in box:
            raise box["error"]
        return box.get("pcm")


# -- wiring ---------------------------------------------------------------
def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _connect(args, cfg: dict) -> EffectorClient:
    """--unix > --cid/--port > config. Explicit flags always win.

    The config may itself select a unix socket (the guest sets
    `[vsock].unix_path`), so --cid/--port have to *force* vsock rather than
    patch fields on a client that would still dial a unix path."""
    timeout_s = float(cfg["vsock"].get("timeout_s", 5.0))
    if args.unix:
        return EffectorClient.over_unix(args.unix, timeout_s)
    if args.cid is not None or args.port is not None:
        v = cfg.get("vsock", {})
        return EffectorClient(cid=args.cid if args.cid is not None else int(v.get("cid", 3)),
                              port=args.port if args.port is not None else int(v.get("port", 5000)),
                              timeout_s=timeout_s)
    return EffectorClient.from_config(cfg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="musashi-voice")
    parser.add_argument("--cid", type=int, default=None, help="guest CID (default 3)")
    parser.add_argument("--port", type=int, default=None, help="effector vsock port")
    parser.add_argument("--unix", metavar="PATH",
                        help="talk to musashi-effector --unix PATH instead of vsock")
    parser.add_argument("--text", metavar="PHRASE",
                        help="skip mic and STT: resolve PHRASE and act on it")
    parser.add_argument("--list-tools", action="store_true",
                        help="print the guest's live tool table and the grammar built from it")
    parser.add_argument("--no-tts", action="store_true", help="print replies instead of speaking")
    parser.add_argument("--devices", action="store_true", help="list audio devices and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    cfg = load_config()
    cfg.setdefault("tts", {})["model"] = expand_path(cfg.get("tts", {}).get("model", ""))
    if args.no_tts:
        cfg["tts"]["enabled"] = False

    if args.devices:
        from . import audio
        print(audio.list_devices())
        return 0

    client = _connect(args, cfg)

    # The grammar is derived from the guest's live table, never hardcoded: a
    # tool added to the effector is speakable after a restart of this process.
    tools = client.tools()
    if not tools:
        print("could not read the tool table from the effector.\n"
              "  - is the VM running (./run.sh) and musashi-effector up?\n"
              f"  - target: {client.describe()}\n"
              "  - no VM? run the daemon on the host with\n"
              "      python -m musashi_effector --unix /tmp/eff.sock\n"
              "    and pass --unix /tmp/eff.sock here.", file=sys.stderr)
        return 1

    locales = tuple(cfg.get("intent", {}).get("locales", ("pt", "en")))
    grammar = Grammar.from_tool_table(tools, locales=locales)

    if args.list_tools:
        for tool in tools:
            print(f"{tool['name']:<14} {tool.get('class','?'):<7} {tool.get('doc','')}")
        print(f"\n{len(grammar.phrases)} spoken phrases over {grammar.tools()}:")
        for p in grammar.phrases:
            print(f"  [{p.locale}] {p.text:<32} -> {p.tool} {p.arg_dict}")
        return 0

    loop = VoiceLoop(cfg, client, grammar)

    # Text mode: the whole intent -> vsock -> TTS path, no microphone, no GPU.
    if args.text:
        trace = LatencyTrace(label="text")
        loop.handle_transcript(args.text, trace)
        print(trace.format_summary())
        return 0

    from .stt import Transcriber
    loop.transcriber = Transcriber(cfg.get("stt"))
    print("loading the Whisper model (first run downloads it)...")
    loop.transcriber.load()

    print(f"ready. tools: {grammar.tools()}   Ctrl-C to quit.")
    try:
        while True:
            pcm = loop.record_once()
            trace = LatencyTrace(label="utterance")   # clock starts at end of speech
            if pcm is None or pcm.size == 0:
                print("  (no audio captured)")
                continue
            loop.handle_audio(pcm, trace)
            print(trace.format_summary())
    except KeyboardInterrupt:
        print()
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
