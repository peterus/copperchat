"""Prototype in-memory search index over name / keywords / description (spike-local).

Tokenized inverted index, prefix matching via a sorted vocabulary, additive field weights,
AND-first ranking. Pure Python, no dependencies.
"""

from __future__ import annotations

import bisect
import re
from collections import defaultdict

_TOKEN = re.compile(r"[a-z0-9]+")
W_NAME, W_KEYWORDS, W_DESC, W_LIB = 8.0, 4.0, 1.0, 0.5


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class Index:
    def __init__(self) -> None:
        self.docs: list[tuple[str, str]] = []  # (lib_id, name_lower)
        self.postings: dict[str, dict[int, float]] = defaultdict(dict)
        self.vocab: list[str] = []

    def add(self, lib_id: str, description: str = "", keywords: str = "") -> None:
        doc = len(self.docs)
        lib, _, name = lib_id.partition(":")
        self.docs.append((lib_id, name.lower()))
        for field_text, w in ((name, W_NAME), (keywords, W_KEYWORDS), (description, W_DESC), (lib, W_LIB)):
            for tok in set(tokenize(field_text)):
                cur = self.postings[tok]
                if cur.get(doc, 0) < w:
                    cur[doc] = w

    def finish(self) -> None:
        self.vocab = sorted(self.postings)

    def _expand(self, term: str) -> list[tuple[str, float]]:
        out = []
        if term in self.postings:
            out.append((term, 1.0))
        if len(term) >= 2:
            i = bisect.bisect_left(self.vocab, term)
            n = 0
            while i < len(self.vocab) and self.vocab[i].startswith(term) and n < 300:
                if self.vocab[i] != term:
                    out.append((self.vocab[i], 0.6))
                i += 1
                n += 1
        return out

    def search(self, query: str, limit: int = 20, library: str | None = None) -> list[tuple[str, float]]:
        terms = tokenize(query)
        if not terms:
            return []
        scores: dict[int, float] = defaultdict(float)
        hit_terms: dict[int, int] = defaultdict(int)
        for term in terms:
            best: dict[int, float] = {}
            for tok, factor in self._expand(term):
                for doc, w in self.postings[tok].items():
                    v = w * factor
                    if v > best.get(doc, 0):
                        best[doc] = v
            for doc, v in best.items():
                scores[doc] += v
                hit_terms[doc] += 1
        n = len(terms)
        ranked = []
        for doc, sc in scores.items():
            lib_id, name = self.docs[doc]
            if library and not lib_id.startswith(library + ":"):
                continue
            bonus = 100.0 if hit_terms[doc] == n else 0.0
            if name == query.lower():
                bonus += 50.0
            elif name.startswith(query.lower()):
                bonus += 10.0
            ranked.append((lib_id, sc + bonus - len(name) * 0.01))
        ranked.sort(key=lambda r: -r[1])
        return ranked[:limit]
