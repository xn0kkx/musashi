"""web.search / web.fetch: the assistant's read-only window on the internet.

These are QUERY tools in the system's binary class sense (Plano Diretor §2.7):
they read the world and change nothing, so — unlike shell.exec or app.launch —
they do not go through the effector at all. The effector owns *effects*; a
search has none, so it runs in this process, next to the LLM that asked for it.

Everything is imported lazily: `ddgs` and `trafilatura` are the `[llm]` extra,
and a bare checkout (or the core test suite) must import this module without
them. A tool whose dependency is missing returns an honest error string that
the LLM can read and route around, exactly as it would a failed search — it
never raises into the voice loop.
"""
from __future__ import annotations

import logging

log = logging.getLogger("musashi-voice.webtools")


class WebTools:
    """Free-to-call web reads for the LLM agent. No effector, no side effects."""

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.max_results = int(cfg.get("max_results", 5))
        self.fetch_max_chars = int(cfg.get("fetch_max_chars", 4000))
        self.timeout_s = float(cfg.get("timeout_s", 10.0))

    # -- web.search -----------------------------------------------------
    def search(self, query: str, max_results: int | None = None) -> str:
        """DuckDuckGo -> a compact title/snippet/url list the LLM can read."""
        n = int(max_results or self.max_results)
        try:
            from ddgs import DDGS
        except ImportError:
            return "web.search unavailable: ddgs not installed (pip install ddgs)"
        try:
            with DDGS() as ddgs:
                hits = list(ddgs.text(query, max_results=n))
        except Exception as exc:                              # noqa: BLE001
            log.warning("web.search failed: %s", exc)
            return f"web.search failed: {exc}"
        if not hits:
            return f"no results for {query!r}"
        lines = []
        for i, h in enumerate(hits, 1):
            title = h.get("title", "").strip()
            body = h.get("body", "").strip()
            url = h.get("href", "") or h.get("url", "")
            lines.append(f"{i}. {title}\n   {body}\n   {url}")
        return "\n".join(lines)

    # -- web.fetch ------------------------------------------------------
    def fetch(self, url: str, max_chars: int | None = None) -> str:
        """Fetch a page and return its main text, extracted and truncated."""
        limit = int(max_chars or self.fetch_max_chars)
        try:
            import requests
        except ImportError:
            return "web.fetch unavailable: requests not installed"
        try:
            resp = requests.get(url, timeout=self.timeout_s,
                                headers={"User-Agent": "musashi-voice/0.1"})
            resp.raise_for_status()
        except Exception as exc:                              # noqa: BLE001
            log.warning("web.fetch failed: %s", exc)
            return f"web.fetch failed: {exc}"
        text = None
        try:
            import trafilatura
            text = trafilatura.extract(resp.text)
        except ImportError:
            pass
        except Exception as exc:                              # noqa: BLE001
            log.debug("trafilatura extract failed, falling back to raw: %s", exc)
        if not text:
            text = resp.text
        text = text.strip()
        if len(text) > limit:
            text = text[:limit] + f"\n... [truncated, {len(text)} chars total]"
        return text

    # -- tool definitions for the LLM -----------------------------------
    def tool_defs(self) -> list[dict]:
        """OpenAI/Ollama-shaped function definitions for web.search/web.fetch.

        Function names are underscored (web_search/web_fetch) because Ollama's
        function-name grammar rejects dots; the assistant maps them back to the
        dotted internal names (handled by handles()/dispatch()) when routing."""
        if not self.enabled:
            return []
        return [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the web and return a list of "
                                   "results (title, snippet, url).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string",
                                      "description": "what to search for"},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "web_fetch",
                    "description": "Fetch a URL and return its main text content.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string",
                                    "description": "the URL to fetch"},
                        },
                        "required": ["url"],
                    },
                },
            },
        ]

    def handles(self, name: str) -> bool:
        return name in ("web.search", "web.fetch")

    def dispatch(self, name: str, args: dict) -> str:
        if name == "web.search":
            return self.search(str(args.get("query", "")))
        if name == "web.fetch":
            return self.fetch(str(args.get("url", "")))
        return f"unknown web tool: {name}"
