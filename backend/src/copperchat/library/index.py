"""In-memory search index over library items (port of spikes/2b-libraries/index.py).

Tokenized inverted index over name / keywords / description / library nickname with
prefix matching through a sorted vocabulary, additive field weights and AND-first
ranking. Pure Python; a query over all ~23 000 stock symbols takes a few ms (spike 2B).
"""

from __future__ import annotations

import bisect
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

Scope = Literal["project", "global"]

_TOKEN = re.compile(r"[a-z0-9]+")
W_NAME, W_KEYWORDS, W_DESC, W_LIB = 8.0, 4.0, 1.0, 0.5
_PREFIX_FACTOR = 0.6
_MAX_SHORT_EXPANSIONS = 300  # 2-character prefixes only; longer prefixes expand fully
MAX_TERMS = 16


MAX_TEXT_CHARS = 500  # free-text library fields (project libraries come from the repo)


def clip(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    """Library text as shown to the agent: at most `limit` characters."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


@dataclass
class SymbolRow:
    """One searchable library symbol. `pin_count` is None for derived (`extends`)
    symbols until `LibraryService.get_symbol` probed them (spike 2B, finding 3)."""

    lib_id: str
    description: str = ""
    keywords: str = ""
    unit_count: int = 1
    pin_count: int | None = None
    footprint: str = ""
    footprint_filters: list[str] = field(default_factory=list)
    scope: Scope = "global"

    def to_dict(self) -> dict[str, Any]:
        return {
            "lib_id": self.lib_id,
            "description": self.description,
            "keywords": self.keywords,
            "unit_count": self.unit_count,
            "pin_count": self.pin_count,
            "footprint": self.footprint or None,
            "footprint_filters": self.footprint_filters[:6],
            "scope": self.scope,
        }


class Index:
    """Add items with `add`, call `finish` once, then `search`."""

    def __init__(self) -> None:
        self._docs: list[tuple[str, str, bool]] = []  # (lib_id, name_lower, is_project)
        self._postings: dict[str, dict[int, float]] = defaultdict(dict)
        self._vocab: list[str] = []

    def __len__(self) -> int:
        return len(self._docs)

    def add(
        self, lib_id: str, description: str = "", keywords: str = "", scope: Scope = "global"
    ) -> None:
        doc = len(self._docs)
        lib, _, name = lib_id.partition(":")
        self._docs.append((lib_id, name.lower(), scope == "project"))
        # The name is always indexed, so an item without description/keywords (common
        # in project libraries) is still found by its name.
        for text, weight in ((name, W_NAME), (keywords, W_KEYWORDS), (description, W_DESC)):
            for tok in set(tokenize(text)):
                cur = self._postings[tok]
                if cur.get(doc, 0.0) < weight:
                    cur[doc] = weight
        for tok in set(tokenize(lib)):
            cur = self._postings[tok]
            if doc not in cur:
                cur[doc] = W_LIB

    def finish(self) -> None:
        self._vocab = sorted(self._postings)

    def _expand(self, term: str) -> list[tuple[str, float]]:
        out: list[tuple[str, float]] = []
        if term in self._postings:
            out.append((term, 1.0))
        if len(term) >= 2:
            i = bisect.bisect_left(self._vocab, term)
            n = 0
            cap = _MAX_SHORT_EXPANSIONS if len(term) == 2 else len(self._vocab)
            while i < len(self._vocab) and self._vocab[i].startswith(term) and n < cap:
                if self._vocab[i] != term:
                    out.append((self._vocab[i], _PREFIX_FACTOR))
                i += 1
                n += 1
        return out

    def search(
        self, query: str, limit: int = 20, library: str | None = None
    ) -> list[tuple[str, float]]:
        """(lib_id, score), best first. Items matching every query term rank above
        partial matches; equal scores put project libraries first."""
        terms = list(dict.fromkeys(tokenize(query)))[:MAX_TERMS]
        if not terms or limit <= 0:
            return []
        scores: dict[int, float] = defaultdict(float)
        hit_terms: dict[int, int] = defaultdict(int)
        for term in terms:
            best: dict[int, float] = {}
            for tok, factor in self._expand(term):
                for doc, weight in self._postings[tok].items():
                    v = weight * factor
                    if v > best.get(doc, 0.0):
                        best[doc] = v
            for doc, v in best.items():
                scores[doc] += v
                hit_terms[doc] += 1
        whole = query.strip().lower()
        prefix = f"{library}:" if library else None
        ranked: list[tuple[float, bool, str]] = []
        for doc, score in scores.items():
            lib_id, name, is_project = self._docs[doc]
            if prefix is not None and not lib_id.startswith(prefix):
                continue
            bonus = 100.0 if hit_terms[doc] == len(terms) else 0.0
            if name == whole:
                bonus += 50.0
            elif name.startswith(whole):
                bonus += 10.0
            ranked.append((score + bonus - len(name) * 0.01, is_project, lib_id))
        ranked.sort(key=lambda r: (-r[0], not r[1], r[2]))
        return [(lib_id, score) for score, _, lib_id in ranked[:limit]]
