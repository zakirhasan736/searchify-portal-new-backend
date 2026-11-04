"""Tracked keywords in the database, current keywords, business-fit suggestions, track → page update."""

from fastapi.testclient import TestClient

from app import keyword_tracking
from app.database import SessionLocal
from app.main import app
from app.models import FeatureRecord, SiteChange, TrackedKeyword
from app.security import get_current_user
from tests.db_fixtures import drop_user, make_user

CONCEPT = {
    "summary": "Volo Air sells home air filters and purifiers.",
    "category": "air filters",
    "offers": ["merv 13 air filter", "hvac filter subscription", "air purifier"],
    "pages": [
        {"url": "https://voloair.example/products/merv-13", "role": "product", "target": "merv 13 air filter"},
        {"url": "https://voloair.example/products/purifier", "role": "product", "target": "home air purifier"},
    ],
}
CANDIDATES = [
    {"keyword": "MERV 13 air filter 20x25x1", "volume": 8100, "difficulty": 31},
    {"keyword": "air cleaner for home", "volume": 3600, "difficulty": 39},
    {"keyword": "plumber near me", "volume": 90000, "difficulty": 60},
    {"keyword": "already tracked", "volume": 10, "difficulty": 1},
]


def test_ai_judge_keeps_business_searches_and_synonyms(monkeypatch):
    def judge(concept, places, pool):
        assert all(row["keyword"] != "already tracked" for row in pool)
        return {
            "merv 13 air filter 20x25x1": {"fit": "core", "concept": "merv 13 air filter", "page": CONCEPT["pages"][0]["url"], "model": "m"},
            "air cleaner for home": {"fit": "related", "concept": "air purifier", "page": CONCEPT["pages"][1]["url"], "model": "m"},
            "plumber near me": {"fit": "off", "concept": "", "page": "", "model": "m"},
        }

    monkeypatch.setattr(keyword_tracking, "judge_fit", judge)
    out = keyword_tracking.business_suggestions(CONCEPT, [], CANDIDATES, known={"already tracked"})
    words = [row["keyword"] for row in out["rows"]]
    assert words == ["merv 13 air filter 20x25x1", "air cleaner for home"]
    assert out["rows"][1]["page"].endswith("/purifier")  # a synonym word-matching would have dropped
    assert out["judged"] == "ai"


def test_without_ai_falls_back_to_offer_words(monkeypatch):
    monkeypatch.setattr(keyword_tracking, "judge_fit", lambda *a: None)
    out = keyword_tracking.business_suggestions(CONCEPT, [], CANDIDATES, known=set())
    words = [row["keyword"] for row in out["rows"]]
    assert "plumber near me" not in words and "merv 13 air filter 20x25x1" in words
    assert out["judged"] == "words"


def test_current_keywords_put_dataforseo_first_and_add_search_console():
    user = make_user("kwcur")
    try:
        with SessionLocal() as db:
            db.add(FeatureRecord(customer_id=user.id, kind="organic-search", title="Organic Search (GSC) · voloair.example", status="stored",
                                 payload={"siteHost": "voloair.example", "rows": [["merv 13 filter", "12", "400", "3.0%", "6.2"], ["volo air", "40", "90", "44%", "1.0"]]}))
            db.commit()
            out = keyword_tracking.current_keywords(db, user.id, "voloair.example", [{"keyword": "merv 13 filter", "position": 7, "volume": 2400}])
        rows = out["rows"]
        assert rows[0]["keyword"] == "merv 13 filter" and rows[0]["source"] == "DataForSEO"
        assert rows[0]["clicks"] == 12 and rows[0]["position"] == 7
        assert rows[1]["keyword"] == "volo air" and rows[1]["source"] == "Search Console" and rows[1]["position"] == 1
        assert out["searchConsoleConnected"]
    finally:
        drop_user(user.id)


def test_track_saves_in_database_and_starts_page_update(monkeypatch):
    from app import research
    from app.routers import operator

    user = make_user("kwtrack")
    monkeypatch.setattr(research, "track_term_metrics", lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "tracked_keyword_limit", lambda *a: 10)

    def fake_generate(change_id, body, db, u):
        row = db.get(SiteChange, change_id)
        row.proposed = {**(row.proposed or {}), "title": "MERV 13 Air Filter 20x25x1 | Volo Air", "metaDescription": "Hospital-grade filters.", "target": body.primaryKeyword}
        db.commit()
        return {"ok": True, "change": {"id": row.id, "status": row.status}}

    monkeypatch.setattr(operator, "generate_meta", fake_generate)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        client = TestClient(app)
        res = client.post("/api/v1/research/keywords/track", json={
            "site": "https://voloair.example", "keyword": "MERV 13 Air Filter 20x25x1",
            "page": "https://voloair.example/products/merv-13", "source": "suggestion", "apply": True,
        })
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["created"] and body["change"]["id"]
        assert body["tracked"][0]["keyword"] == "merv 13 air filter 20x25x1"
        assert body["tracked"][0]["change"]["status"] == "awaiting_approval"
        with SessionLocal() as db:
            row = db.query(TrackedKeyword).filter(TrackedKeyword.customer_id == user.id).one()
            assert row.change_id == body["change"]["id"] and row.source == "suggestion"
            change = db.get(SiteChange, row.change_id)
            assert change.status == "awaiting_approval" and change.proposed["primaryKeyword"] == row.keyword

        listed = client.get("/api/v1/research/keywords/tracked", params={"site": "voloair.example"}).json()
        assert [r["keyword"] for r in listed["tracked"]] == ["merv 13 air filter 20x25x1"]

        again = client.post("/api/v1/research/keywords/track", json={"site": "voloair.example", "keyword": "merv 13 air filter 20x25x1", "apply": False}).json()
        assert again["created"] is False

        gone = client.post("/api/v1/research/keywords/untrack", json={"site": "voloair.example", "keyword": "MERV 13 air filter 20x25x1"}).json()
        assert gone["removed"] and gone["tracked"] == []
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        drop_user(user.id)


def test_track_without_matching_page_only_tracks(monkeypatch):
    from app import research

    user = make_user("kwnopage")
    monkeypatch.setattr(research, "track_term_metrics", lambda *a, **k: None)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        body = TestClient(app).post("/api/v1/research/keywords/track", json={"site": "nopage.example", "keyword": "duct cleaning"}).json()
        assert body["ok"] and body["change"] is None
        assert "No page" in body["note"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        drop_user(user.id)
