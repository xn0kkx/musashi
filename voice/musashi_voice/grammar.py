"""Stage (a) of the intent cascade: fixed grammar + deterministic fuzzy match.

The plan's rationale: an 8B LLM as the *only* path costs 120-400 ms of TTFT on
every sentence, including "abrir terminal". So the ~25 frequent commands go
through sentence templates plus tolerant matching (the `speech-to-phrase`
pattern from OHF-Voice): < 5 ms, zero hallucination, zero GPU. Only a miss is
worth an LLM, and that LLM is *not* in this file — see intent.resolve_intent.

Everything here is derived from the **live** tool table fetched from the guest
via `sys.tools`. Nothing about app.launch or the app allowlist is hardcoded in
this module: add a tool to the effector, restart the voice loop, and it is
speakable. What *is* hardcoded, deliberately and documented, is two tables:

  VERBS  — how a tool name is said out loud in pt/en. A tool with no entry is
           still reachable by its own dotted name ("sys tools"), just not
           idiomatically. This is the "frases-padrão simples por tool" the
           plan asks for, and it is a table rather than a doc-string parser
           because guessing verbs from English doc strings is exactly the kind
           of cleverness that fails silently in PT-BR.

  ALIASES — how an allowlisted *value* is said out loud ("foot.desktop" ->
           "terminal"). Unknown values are not a dead end: `_derived_aliases`
           extracts a spoken form from the id itself, so a newly allowlisted
           org.gnome.Weather.desktop answers to "weather" on day one, and only
           needs an entry here to also answer to "tempo".

An arg whose schema has no `choices` is not voice-addressable in V1: ui.tap
takes two floats, and "tap at zero point three seven" is a dictation problem,
not a grammar problem. Such tools are skipped, visibly, with a debug log.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz, process

log = logging.getLogger("musashi-voice.grammar")

# Default acceptance threshold. Sits in the measured gap between the worst
# real command (85.0) and the best unrelated sentence (62.1) — see match().
MIN_SCORE = 75.0

# How each tool is said. Order matters only for readability.
VERBS: dict[str, dict[str, tuple[str, ...]]] = {
    "app.launch": {
        "pt": ("abrir", "abre", "abra", "iniciar", "inicia", "executar", "lancar"),
        "en": ("open", "launch", "start", "run"),
    },
    "app.close": {
        "pt": ("fechar", "fecha", "feche", "encerrar", "encerra", "sair de"),
        "en": ("close", "quit", "exit", "stop"),
    },
    "shell.swipe": {
        "pt": ("deslizar", "deslize", "desliza", "arrastar", "arrasta", "swipe"),
        "en": ("swipe", "slide"),
    },
    "sys.tools": {
        "pt": ("listar comandos", "quais comandos", "o que voce sabe fazer",
               "listar ferramentas"),
        "en": ("list commands", "list tools", "what can you do"),
    },
}

# How each allowlisted value is said. Keyed by the literal value in the tool
# table's `choices`. Accents are stripped at match time, so write them plainly.
ALIASES: dict[str, tuple[str, ...]] = {
    # app.launch / app.close ids from effector.toml's [apps].allow
    "foot.desktop": ("terminal", "foot", "console", "shell"),
    "org.gnome.Calculator.desktop": ("calculadora", "calculator", "calc"),
    # shell.swipe directions
    "up": ("cima", "para cima", "up", "acima"),
    "down": ("baixo", "para baixo", "down", "abaixo"),
    "left": ("esquerda", "para a esquerda", "left"),
    "right": ("direita", "para a direita", "right"),
}

# Dropped before matching: they carry no intent and only dilute the score.
# Note what is *not* here: "up", "down", "left", "right", "cima", "baixo" —
# those are real shell.swipe values, and dropping them would delete the whole
# meaning of the sentence.
# Also not here: "do". It is a Portuguese article *and* an English verb, and
# dropping it turned "what can you do" into "what can you", which then scored
# 85 against "what is the capital of mongolia".
STOPWORDS = frozenset({
    "o", "a", "os", "as", "um", "uma", "de", "da", "no", "na", "em",
    "para", "pra", "pro", "por", "favor", "me", "ai", "entao", "ja",
    "the", "an", "to", "please", "now",
})

_WORD_RE = re.compile(r"[^a-z0-9 ]+")


def normalize(text: str) -> str:
    """Lowercase, strip accents and punctuation, drop stopwords, collapse space.

    Accent stripping is what makes the match survive Whisper writing "lançar"
    where the template says "lancar", and PT-BR speakers who drop accents in
    the template file."""
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = _WORD_RE.sub(" ", folded.lower())
    words = [w for w in folded.split() if w and w not in STOPWORDS]
    return " ".join(words)


def _derived_aliases(value: str) -> tuple[str, ...]:
    """A spoken form guessed from the value itself, for values not in ALIASES.

    "org.gnome.Calculator.desktop" -> "calculator"; "foot.desktop" -> "foot".
    Never a substitute for a curated alias, but it means a tool table that
    grows on the guest is never mute on the host."""
    stem = value[:-len(".desktop")] if value.endswith(".desktop") else value
    last = stem.rsplit(".", 1)[-1]
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", last).replace("-", " ").replace("_", " ")
    return tuple(dict.fromkeys(a for a in (normalize(spaced), normalize(stem)) if a))


def aliases_for(value: str) -> tuple[str, ...]:
    """Every spoken form of one allowlisted value, curated first."""
    curated = tuple(normalize(a) for a in ALIASES.get(value, ()))
    return tuple(dict.fromkeys(curated + _derived_aliases(value)))


def _tool_verbs(name: str, locale: str) -> tuple[str, ...]:
    verbs = VERBS.get(name, {}).get(locale)
    if verbs:
        return tuple(normalize(v) for v in verbs)
    # Fallback so an unknown tool is odd to say, but not unreachable.
    return (normalize(name.replace(".", " ")),)


@dataclass(frozen=True)
class Phrase:
    """One thing a person can say, and exactly what it means."""
    text: str                    # normalized
    tool: str
    args: tuple[tuple[str, object], ...]   # hashable; dict() at match time
    locale: str

    @property
    def arg_dict(self) -> dict:
        return dict(self.args)


@dataclass(frozen=True)
class Match:
    tool: str
    args: dict
    score: float
    phrase: str
    locale: str


class Grammar:
    """The phrase table, built from a live `sys.tools` response."""

    def __init__(self, phrases: list[Phrase], value_labels: dict[str, str]):
        self._phrases = tuple(phrases)
        self._labels = dict(value_labels)
        self._index = [p.text for p in self._phrases]

    # -- construction ----------------------------------------------------
    @classmethod
    def from_tool_table(cls, tools: list[dict],
                        locales: tuple[str, ...] = ("pt", "en")) -> "Grammar":
        phrases: list[Phrase] = []
        labels: dict[str, str] = {}

        for tool in tools:
            name = tool.get("name")
            if not isinstance(name, str):
                continue
            args = tool.get("args") or {}

            # Which args must be spoken? Only required ones without a default.
            required = {k: v for k, v in args.items()
                        if isinstance(v, dict) and v.get("required")}
            unchoosable = [k for k, v in required.items() if not v.get("choices")]
            if unchoosable:
                log.debug("skipping %s: args %s have no choices to speak",
                          name, sorted(unchoosable))
                continue
            if len(required) > 1:
                # No V1 tool needs two spoken slots; a template language for
                # that is a whole design, and guessing word order in two
                # languages is how you get "abrir terminal calculadora".
                log.debug("skipping %s: %d required args, V1 grammar takes 1",
                          name, len(required))
                continue

            for locale in locales:
                verbs = _tool_verbs(name, locale)
                if not required:
                    for verb in verbs:
                        phrases.append(Phrase(verb, name, (), locale))
                    continue
                (arg_name, arg_spec), = required.items()
                for value in arg_spec["choices"]:
                    for alias in aliases_for(value):
                        labels.setdefault(value, alias)
                        for verb in verbs:
                            phrases.append(Phrase(
                                normalize(f"{verb} {alias}"), name,
                                ((arg_name, value),), locale))

        # Identical normalized text can arise twice (e.g. "swipe up" is the
        # same in both locales); keep the first, so the score is not diluted
        # and the reported locale is stable.
        seen: set[str] = set()
        unique = [p for p in phrases if not (p.text in seen or seen.add(p.text))]
        log.info("grammar: %d phrases from %d tools", len(unique), len(tools))
        return cls(unique, labels)

    # -- introspection ---------------------------------------------------
    @property
    def phrases(self) -> tuple[Phrase, ...]:
        return self._phrases

    def tools(self) -> list[str]:
        return sorted({p.tool for p in self._phrases})

    def label_for(self, value: object) -> str:
        """The friendly spoken name of a value, for the TTS confirmation:
        "foot.desktop" -> "terminal"."""
        return self._labels.get(value, str(value))

    # -- matching --------------------------------------------------------
    def match(self, transcript: str, min_score: float = MIN_SCORE) -> Match | None:
        """Best phrase above `min_score`, or None (a MISS).

        Scorer choice, measured against this tool table (see
        tests/test_grammar_match.py for the corpus):

          scorer            worst hit   best miss
          fuzz.ratio            85.0        62.1   <- chosen
          token_sort_ratio      85.0        61.5
          WRatio                92.9        85.5

        WRatio and token_set_ratio both fall back to a *partial* match, which
        scores a short phrase against any long sentence containing something
        like it — "what is the capital of mongolia" hit "what can you do" at
        85.5. For a fixed command grammar the whole-string comparison is the
        right one: a padded command still scores in the high 80s, and an
        unrelated sentence has nowhere to hide."""
        query = normalize(transcript)
        if not query or not self._index:
            return None
        best = process.extractOne(query, self._index, scorer=fuzz.ratio,
                                  score_cutoff=min_score)
        if best is None:
            log.info("MISS: %r matched nothing above %.0f", transcript, min_score)
            return None
        _text, score, idx = best
        p = self._phrases[idx]
        return Match(tool=p.tool, args=p.arg_dict, score=float(score),
                     phrase=p.text, locale=p.locale)
