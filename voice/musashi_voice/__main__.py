"""musashi-voice: the host-side voice loop, its PTT test harness, and the
always-on wake-word loop the systemd unit runs.

    python -m musashi_voice                          # PTT loop against the VM
    python -m musashi_voice --wake                    # always-on, "musashi" wakes it
    python -m musashi_voice --unix /tmp/eff.sock     # ... against a host effector
    python -m musashi_voice --text "abrir terminal"  # no mic, no GPU, one shot
    python -m musashi_voice --list-tools             # what the guest exposes

**Push-to-talk harness (default mode, unchanged).** Enter starts recording,
Enter stops it. A held key (pynput/keyboard) was rejected for V1: both need an
X/Wayland grab or root to see key-up reliably, and this harness is scaffolding.
It remains the default because it is still what a human at a terminal wants
for manual testing — `--wake` does not replace it, it adds a second mode.

**`--wake` (always-on).** `musashi-voice.service` has no console to press
Enter at, so V2 replaces the human's second Enter with a wake word: capture
never stops (see audio.listen()), and only an utterance whose transcript
starts with something close to "musashi" (wakeword.strip_wake_word()) is
acted on. This is the trade-off explained at length in the systemd unit: it
trades away PTT's "second, non-audio factor" property (Plano Diretor §2.7)
for the ability to run headless. Deliberate, not an oversight.

Everything after "the user stopped talking" is timed per stage, which the plan
requires from day one (Plano Diretor §8/S4: p50 <= 900 ms). In --wake mode
"stopped talking" is the VAD's silence boundary rather than a keypress, so
`[wake].silence_ms` is baked into every WALL measurement — see voice/README.md.
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time

from .config import expand_path, load_config
from .grammar import MIN_SCORE, Grammar
from .intent import resolve_intent
from .latency import LatencyTrace
from .tts import Speaker, confirmation_text
from .vsock import EffectorClient
from .wakeword import DEFAULT_THRESHOLD as WAKE_DEFAULT_THRESHOLD
from .wakeword import DEFAULT_WAKE_WORD, strip_wake_word

log = logging.getLogger("musashi-voice")


class VoiceLoop:
    """One utterance in, one action and one spoken reply out.

    `fallback` is the single-shot LLM seam (see intent.resolve_intent): it
    proposes ONE intent on a grammar miss. `assistant` is the separate,
    multi-turn agent seam (see assistant.LlmAssistant): on a grammar miss with
    no fallback proposal, it runs a tool-call loop and speaks free prose. Both
    are None for the plain V1 loop; either can be wired without touching the
    grammar or intent modules."""

    def __init__(self, cfg: dict, client: EffectorClient, grammar: Grammar,
                 transcriber=None, speaker: Speaker | None = None, fallback=None,
                 assistant=None):
        self.cfg = cfg
        self.client = client
        self.grammar = grammar
        self.transcriber = transcriber
        self.speaker = speaker or Speaker(cfg.get("tts"))
        self.fallback = fallback
        self.assistant = assistant
        # Spoken immediately when the assistant takes over a grammar miss, so a
        # slow LLM turn (tens of seconds on CPU) is not dead silence — the user
        # hears that Musashi caught the request and is working on it. Empty
        # disables it.
        self.llm_ack = str(cfg.get("llm", {}).get("ack", "")).strip()
        self.min_score = float(cfg.get("intent", {}).get("min_score", MIN_SCORE))
        self.locale = str(cfg.get("tts", {}).get("locale", "pt"))
        wake_cfg = cfg.get("wake", {})
        self.wake_word = str(wake_cfg.get("word", DEFAULT_WAKE_WORD))
        self.wake_threshold = float(wake_cfg.get("threshold", WAKE_DEFAULT_THRESHOLD))

    # -- the half of the pipeline that needs no hardware -------------------
    def handle_transcript(self, transcript: str, trace: LatencyTrace) -> None:
        with trace.stage("intent"):
            intent = resolve_intent(transcript, self.grammar,
                                    min_score=self.min_score, fallback=self.fallback)

        if intent is None:
            # Grammar miss, no single-shot fallback proposal: hand the raw
            # transcript to the agent if one is wired, which speaks its own
            # reply. Only when there is no assistant do we fall back to the
            # canned "não entendi".
            if self.assistant is not None:
                print("  → Musashi está pensando...")
                if self.llm_ack:
                    self.speaker.say(self.llm_ack)
                self.assistant.handle(transcript, trace)
                return
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

    # -- the always-on pipeline ---------------------------------------------
    def handle_wake_audio(self, pcm, trace: LatencyTrace) -> bool:
        """One utterance from `audio.listen()` in; True if it became a real
        interaction (worth printing a latency summary for), False if it was
        silently discarded for lacking the wake word.

        No `trim_silence()` here: `audio.listen()`'s VadSegmenter already
        delivered a silence-delimited utterance, and re-running the endpoint
        VAD would only add latency to the S4 budget for no benefit."""
        with trace.stage("stt"):
            transcript = self.transcriber.transcribe(pcm)
        print(f"  heard : {transcript.text!r} [{transcript.language}]")

        with trace.stage("wake"):
            command = strip_wake_word(transcript.text, self.wake_word, self.wake_threshold)
        if command is None:
            log.debug("no wake word in %r; discarding", transcript.text)
            return False

        if not command:
            # The wake word alone, no command after it: the person addressed
            # this machine and got nothing back, which is worse than staying
            # silent -- say so, the same way an unresolved MISS does.
            with trace.stage("tts"):
                self.speaker.say(confirmation_text(None, locale=self.locale))
            print("  reply : (wake word only, no command)")
            return True

        self.handle_transcript(command, trace)
        return True

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
def _make_speaker(cfg: dict):
    """Piper (default) or Kokoro, selected by [tts].engine. Kokoro is the
    natural-voice, sentence-streaming engine the LLM assistant wants; Piper
    stays the default and the guest's canned-confirmation engine."""
    engine = str(cfg.get("tts", {}).get("engine", "piper")).lower()
    if engine == "kokoro":
        from .tts_kokoro import KokoroSpeaker
        return KokoroSpeaker(cfg.get("tts"))
    return Speaker(cfg.get("tts"))


def _make_assistant(cfg: dict, client: EffectorClient, tools: list[dict], speaker):
    """Build the LLM agent from config, or None if [llm].enabled is false."""
    llm_cfg = cfg.get("llm", {})
    if not llm_cfg.get("enabled", False):
        return None
    from .assistant import LlmAssistant, OllamaClient
    from .webtools import WebTools
    web_cfg = cfg.get("web", {})
    web = WebTools(web_cfg) if web_cfg.get("enabled", True) else None
    return LlmAssistant(
        OllamaClient(llm_cfg), client, tools, speaker,
        web_tools=web,
        system_prompt=llm_cfg.get("system_prompt"),
        max_turns=int(llm_cfg.get("max_turns", 6)),
        locale=str(cfg.get("tts", {}).get("locale", "pt")),
    )


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
    parser.add_argument("--no-llm", action="store_true",
                        help="disable the LLM assistant even if [llm].enabled "
                             "is set (grammar-only, canned replies)")
    parser.add_argument("--devices", action="store_true", help="list audio devices and exit")
    parser.add_argument("--wake", action="store_true",
                        help="always-on capture, activated by the wake word "
                             "instead of push-to-talk (what musashi-voice.service runs)")
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
    #
    # --wake only: retry a few times before giving up. Requires=musashi-effector
    # in the unit only orders the two units' *starts*; it does not wait for the
    # socket to actually be listening, so on a fresh boot this process can win
    # the race and ask before the effector has bound /run/musashi/effector.sock.
    tools = client.tools()
    if not tools and args.wake:
        for attempt in range(1, 5):
            log.warning("effector not answering yet (attempt %d/5); retrying in 2s", attempt)
            time.sleep(2.0)
            tools = client.tools()
            if tools:
                break
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

    speaker = _make_speaker(cfg)
    assistant = None if args.no_llm else _make_assistant(cfg, client, tools, speaker)
    if assistant is not None:
        log.info("LLM assistant enabled (model %s)", assistant.ollama.model)
        # Warm the model NOW, off-thread, so it loads while Whisper loads below
        # and the first spoken query hits a resident model instead of eating
        # the cold-start cost (~24s for the 30b MoE) live.
        print("pré-aquecendo o modelo LLM em segundo plano...")
        threading.Thread(target=assistant.warmup, name="llm-warmup",
                         daemon=True).start()
    loop = VoiceLoop(cfg, client, grammar, speaker=speaker, assistant=assistant)

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

    if args.wake:
        return _run_wake_loop(loop, cfg, client)

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


def _run_wake_loop(loop: VoiceLoop, cfg: dict, client: EffectorClient) -> int:
    """Always-on: audio.listen() segments speech by silence forever; each
    utterance is checked for the wake word and only acted on if it starts
    with one. See __main__'s module docstring for the security trade-off."""
    from . import audio

    print(f"ready (wake word {loop.wake_word!r}). tools: {loop.grammar.tools()}   "
          "Ctrl-C to quit.")
    try:
        for pcm in audio.listen(cfg.get("audio"), cfg.get("wake")):
            trace = LatencyTrace(label="utterance")   # clock starts at end of speech
            became_interaction = loop.handle_wake_audio(pcm, trace)
            if became_interaction:
                print(trace.format_summary())
    except audio.AudioUnavailable as exc:
        print(f"audio capture unavailable: {exc}\n"
              "  - did the VM boot with ./run.sh (not --no-audio)?\n"
              "  - is silero-vad installed (pip install 'voice/[audio]')?",
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
