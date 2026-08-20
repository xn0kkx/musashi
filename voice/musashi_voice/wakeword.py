"""The wake word gate: transcript -> command | "" | None.

V2's activation is always-on capture (see audio.listen()) instead of a human
holding a button, which means every utterance the VAD hands the STT — a door
closing, a TV in another room, someone talking to someone else — reaches this
module. Its only job is deciding whether the transcript *starts* with
something close enough to "musashi" to be addressed to this machine at all;
everything after that boundary is unchanged, still resolved by
`intent.resolve_intent()` exactly as a PTT transcript would be.

No dedicated wake-word model here, on purpose (see ROADMAP.md/README.md): the
pretrained openWakeWord models are English-only, and training a PT-BR model
is a GPU pipeline out of scope for this change. Instead this reuses the
faster-whisper transcription the always-on loop already produces and treats
"does the start of the sentence sound like 'musashi'?" as a rapidfuzz problem
— the same tool `grammar.py` already uses for the command grammar itself.
"""
from __future__ import annotations

import logging

from rapidfuzz import fuzz

from .grammar import normalize

log = logging.getLogger("musashi-voice.wakeword")

DEFAULT_WAKE_WORD = "musashi"

# fuzz.ratio(candidate, "musashi") measured over a small hand-built corpus
# (tests/test_wake_word.py prints the exact numbers on every run):
#
#   variant                score   note
#   musashi                 100.0   exact
#   mussashi                 93.3   extra vowel
#   musashe                  85.7   final vowel Whisper commonly substitutes
#   musachi                  85.7   dropped "s"
#   musaxi                   76.9   worst real hit measured
#   mustache (en)             66.7   closest false-friend tried
#   musical                   57.1   common PT-BR word
#   mochachi / mudancas       53.3   worst cases tried; NOT reachable — see below
#   moçambique                35.3
#   maquina                   42.9
#
# 75.0 sits in the gap between the worst measured hit (76.9) and the closest
# measured miss (66.7, "mustache") — the same reasoning grammar.py's
# MIN_SCORE documents, applied to a 7-character target instead of a whole
# sentence. Known limitation, documented rather than hidden: a mishearing far
# enough from "musashi" (the "mochachi"/"mudancas" case) will never cross
# this threshold no matter how it is tuned, without also letting ambient
# speech that merely sounds vaguely similar through. This threshold is a
# stand-in for a real acoustic wake-word model, not a replacement for one.
DEFAULT_THRESHOLD = 75.0


def _window_score(tokens: list[str], wake_word: str) -> tuple[float, int]:
    """Best rapidfuzz.fuzz.ratio score for a 1- or 2-token prefix of `tokens`
    against `wake_word`, and how many tokens that window consumed.

    Two windows, not one: faster-whisper sometimes splits "musashi" across a
    word boundary ("mu sashi"). Scored both concatenated ("musashi") and
    space-joined ("mu sashi") since either could plausibly be closer to the
    target depending on exactly where the split fell.
    """
    if not tokens:
        return 0.0, 0

    best_score, best_width = fuzz.ratio(tokens[0], wake_word), 1

    if len(tokens) >= 2:
        joined = tokens[0] + tokens[1]
        spaced = f"{tokens[0]} {tokens[1]}"
        two_score = max(fuzz.ratio(joined, wake_word), fuzz.ratio(spaced, wake_word))
        # A tie goes to the shorter window: crediting the second token to the
        # wake word when the first one already explains the score would eat
        # the verb off the front of the command ("musashi abrir" -> "abrir"
        # lost if "musashi abrir" scored no better than "musashi" alone).
        if two_score > best_score:
            best_score, best_width = two_score, 2

    return best_score, best_width


def strip_wake_word(transcript: str, wake_word: str = DEFAULT_WAKE_WORD,
                    threshold: float = DEFAULT_THRESHOLD) -> str | None:
    """Check only the *start* of `transcript` for the wake word.

    Mid-sentence matches are deliberately not considered: accepting "musashi"
    anywhere would turn any conversation that happens to mention this machine
    into a trigger, which is a much bigger false-accept surface than a missed
    activation at the very front of an utterance.

    Returns:
      None  -- the wake word was not heard; the caller discards this
               utterance silently (DEBUG log only, no TTS, no dispatch).
      ""    -- the wake word was heard alone, with no command after it. Not
               the same as a miss: someone addressed this machine and got no
               command, so the caller should say "não entendi" rather than
               stay silent as if nothing happened.
      other -- the command, normalized, with the wake word removed. Already
               run through `grammar.normalize()` (idempotent), so it is safe
               to hand straight to `intent.resolve_intent()`.
    """
    tokens = normalize(transcript).split()
    if not tokens:
        return None

    score, width = _window_score(tokens, normalize(wake_word))
    if score < threshold:
        log.debug("wake MISS: %r scored %.1f (< %.0f)", transcript, score, threshold)
        return None

    rest = " ".join(tokens[width:])
    log.info("wake HIT: %r scored %.1f, command=%r", transcript, score, rest)
    return rest
