"""Keyword (BM25) search over the product catalogue.

Why BM25 instead of dense embeddings here: product catalogues are short,
structured, and full of exact tokens that matter (model codes, weights,
"multigym", "MJ8") — BM25 nails exact/near-exact term matches without an
embedding model, an extra API key, or added latency. See FRAMEWORK.md for
when to switch to (or add) dense embeddings instead.
"""
import json
import re
from pathlib import Path
from typing import Optional

from rank_bm25 import BM25Okapi

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class CatalogueIndex:
    def __init__(self, products_path: str):
        self.products: list[dict] = json.loads(Path(products_path).read_text())
        self._by_id = {p["id"]: p for p in self.products}
        corpus = [self._doc_text(p) for p in self.products]
        self._tokenized_corpus = [_tokenize(d) for d in corpus]
        self._bm25 = BM25Okapi(self._tokenized_corpus)

    @staticmethod
    def _doc_text(p: dict) -> str:
        parts = [
            p.get("name", ""),
            p.get("category", ""),
            p.get("subcategory", ""),
            p.get("description", ""),
            " ".join(p.get("best_for", [])),
            " ".join(p.get("tags", [])),
            " ".join(str(v) for v in p.get("specs", {}).values()),
        ]
        return " ".join(parts)

    def search(self, query: str, k: int = 5) -> list[dict]:
        scores = self._bm25.get_scores(_tokenize(query))
        ranked = sorted(zip(scores, self.products), key=lambda pair: pair[0], reverse=True)
        results = [p for score, p in ranked if score > 0][:k]
        if not results:
            # Fall back to the top-scoring items even if the score is weak,
            # so the agent still gets *something* plausible to reason about
            # rather than a hard "no results" on an odd phrasing.
            results = [p for _, p in ranked[:k]]
        return results

    def get_by_id(self, product_id: str) -> Optional[dict]:
        return self._by_id.get(product_id)
