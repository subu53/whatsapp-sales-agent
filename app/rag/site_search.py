"""Keyword search over the small set of business/policy facts (delivery,
tenders, payment, showroom, contact, categories). Small corpus (a handful
of topics) so a lightweight BM25 index is plenty — no vector DB needed."""
import json
import re
from pathlib import Path

from rank_bm25 import BM25Okapi

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class SiteIndex:
    def __init__(self, site_info_path: str):
        self.info: dict = json.loads(Path(site_info_path).read_text())
        self.documents: list[dict] = self._build_documents(self.info)
        corpus = [_tokenize(d["text"]) for d in self.documents]
        self._bm25 = BM25Okapi(corpus)

    @staticmethod
    def _build_documents(info: dict) -> list[dict]:
        docs = []

        docs.append({
            "topic": "positioning",
            "text": f"{info.get('tagline', '')} {info.get('positioning', '')}",
        })

        cats = info.get("categories", {})
        cat_text = " ".join(f"{k}: {', '.join(v)}" for k, v in cats.items())
        docs.append({"topic": "categories", "text": f"product categories {cat_text}"})

        c = info.get("contact", {})
        docs.append({
            "topic": "contact",
            "text": f"contact whatsapp phone email location {c.get('whatsapp','')} "
                    f"{c.get('phone','')} {c.get('email_sales','')} {c.get('email_admin','')} "
                    f"{c.get('location','')}",
        })

        d = info.get("delivery", {})
        docs.append({
            "topic": "delivery",
            "text": f"delivery shipping coverage cost countrywide {d.get('coverage','')} "
                    f"{d.get('policy','')} {d.get('typical_lead_time','')} {d.get('installation','')}",
        })

        t = info.get("commercial_tenders", {})
        docs.append({
            "topic": "commercial_tenders",
            "text": f"commercial tender LPO bulk order hotel school corporate quotation "
                    f"{t.get('audience','')} {' '.join(t.get('offers', []))}",
        })

        docs.append({
            "topic": "payment",
            "text": f"payment methods mpesa m-pesa bank transfer card "
                    f"{' '.join(info.get('payment_methods', []))}",
        })

        s = info.get("showroom", {})
        docs.append({"topic": "showroom", "text": f"showroom visit test equipment parking {s.get('note','')}"})

        docs.append({
            "topic": "policy_pages",
            "text": f"policies returns refunds privacy terms faq "
                    f"{' '.join(info.get('policy_pages', []))}",
        })

        return docs

    def search(self, query: str, k: int = 2) -> list[dict]:
        scores = self._bm25.get_scores(_tokenize(query))
        ranked = sorted(zip(scores, self.documents), key=lambda pair: pair[0], reverse=True)
        results = [d for score, d in ranked if score > 0][:k]
        if not results:
            results = [{"topic": "overview", "text": json.dumps(self.info)}]
        return results
