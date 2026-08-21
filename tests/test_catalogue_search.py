"""Unit tests for retrieval — run with: pytest tests/ -v
These need no API keys and no network access; they're pure logic checks
you (or CI) can run before ever touching a live key."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag.catalogue_search import CatalogueIndex  # noqa: E402
from app.rag.site_search import SiteIndex  # noqa: E402

PRODUCTS_PATH = str(Path(__file__).resolve().parents[1] / "data" / "catalogue" / "products.json")
SITE_INFO_PATH = str(Path(__file__).resolve().parents[1] / "data" / "site_info.json")


def test_catalogue_loads():
    idx = CatalogueIndex(PRODUCTS_PATH)
    assert len(idx.products) > 10


def test_catalogue_search_finds_treadmill():
    idx = CatalogueIndex(PRODUCTS_PATH)
    results = idx.search("treadmill for a hotel gym", k=3)
    names = [p["name"].lower() for p in results]
    assert any("treadmill" in n for n in names)


def test_catalogue_search_finds_dumbbells_under_budget():
    idx = CatalogueIndex(PRODUCTS_PATH)
    results = idx.search("cheap dumbbells for home", k=5)
    assert any("dumbbell" in p["name"].lower() for p in results)


def test_catalogue_search_never_crashes_on_nonsense():
    idx = CatalogueIndex(PRODUCTS_PATH)
    results = idx.search("asdkjaslkdj random gibberish query", k=3)
    assert isinstance(results, list)
    assert len(results) > 0  # falls back to top-scored items rather than an empty list


def test_get_by_id():
    idx = CatalogueIndex(PRODUCTS_PATH)
    product = idx.get_by_id("commercial-workout-bench")
    assert product is not None
    assert product["price_kes"] == 29000


def test_get_by_id_missing_returns_none():
    idx = CatalogueIndex(PRODUCTS_PATH)
    assert idx.get_by_id("does-not-exist") is None


def test_site_index_finds_delivery_info():
    idx = SiteIndex(SITE_INFO_PATH)
    results = idx.search("how much is delivery to Mombasa", k=2)
    joined = " ".join(r["text"] for r in results).lower()
    assert "deliver" in joined


def test_site_index_finds_tender_info():
    idx = SiteIndex(SITE_INFO_PATH)
    results = idx.search("we need a quotation for a tender at our hotel", k=2)
    joined = " ".join(r["text"] for r in results).lower()
    assert "lpo" in joined or "tender" in joined
