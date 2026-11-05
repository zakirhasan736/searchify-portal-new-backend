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


def test_parse_serp_finds_position_features_and_owned():
    from app.rank_tracking import parse_serp

    result = {"items": [
        {"type": "featured_snippet", "domain": "www.voloair.example", "url": "https://www.voloair.example/merv"},
        {"type": "people_also_ask", "items": [{"title": "what is merv 13"}]},
        {"type": "organic", "rank_group": 1, "domain": "rival.example", "url": "https://rival.example/a", "title": "Rival"},
        {"type": "organic", "rank_group": 2, "domain": "www.voloair.example", "url": "https://www.voloair.example/merv", "title": "Us"},
        {"type": "local_pack", "domain": "other.example"},
    ]}
    out = parse_serp(result, "voloair.example")
    assert out["position"] == 2 and out["url"].endswith("/merv")
    assert out["features"] == ["featured_snippet", "people_also_ask", "local_pack"]
    assert out["owned"] == ["featured_snippet"]
    assert [t["domain"] for t in out["top"]] == ["rival.example", "voloair.example"]


def test_question_detection():
    assert keyword_tracking.is_question("how often change merv 13 filter")
    assert keyword_tracking.is_question("merv 13 vs merv 11?")
    assert not keyword_tracking.is_question("merv 13 filter 20x25x1")


def test_competitor_gap_details_survive_dedupe(monkeypatch):
    monkeypatch.setattr(keyword_tracking, "judge_fit", lambda c, p, pool: {r["keyword"]: {"fit": "core", "concept": "", "page": "", "model": "m"} for r in pool})
    rows = [
        {"keyword": "merv 13 filter", "volume": 900, "kind": "site"},
        {"keyword": "merv 13 filter", "volume": 900, "kind": "gap", "gapDomain": "rival.example", "gapPosition": 4},
        {"keyword": "how to change hvac filter", "volume": 500, "kind": "longtail"},
    ]
    out = keyword_tracking.business_suggestions(CONCEPT, [], rows, known=set())
    hit = next(r for r in out["rows"] if r["keyword"] == "merv 13 filter")
    assert hit["gapDomain"] == "rival.example" and hit["gapPosition"] == 4
    assert {r["kind"] for r in out["rows"]} == {"site", "longtail"}


def test_opportunities_keep_weak_ranks_and_search_console_queries():
    ranked = [
        {"keyword": "merv 13 filter", "position": 2, "volume": 2400, "difficulty": 20},
        {"keyword": "hvac filter subscription", "position": 14, "volume": 800, "difficulty": 18, "url": "https://voloair.example/filters"},
    ]
    gsc = [["furnace filter sizes", "4", "220", "1.8%", "18.4"], ["merv 13 filter", "30", "90", "33%", "2.1"]]
    rows = keyword_tracking.opportunity_candidates(ranked, gsc)
    words = {row["keyword"]: row for row in rows}
    assert "merv 13 filter" not in words
    assert words["hvac filter subscription"]["kind"] == "organic"
    assert words["furnace filter sizes"]["kind"] == "console"
    assert words["furnace filter sizes"]["impressions"] == 220
    landings = {"/filters": 86}
    keyword_tracking.stamp_analytics(list(words.values()), landings)
    assert words["hvac filter subscription"]["analyticsSessions"] == 86


def test_google_rows_stay_even_when_the_judge_drops_them():
    ideas = [{"keyword": "air purifier", "kind": "category", "volume": 100, "fit": "core"}]
    candidates = [
        {"keyword": "hvac filter subscription", "kind": "organic", "position": 11, "volume": 400},
        {"keyword": "furnace filter sizes", "kind": "console", "impressions": 220},
        {"keyword": "already tracked", "kind": "organic", "position": 9, "volume": 50},
    ]
    out = keyword_tracking.include_google_rows(ideas, candidates, known={"already tracked"})
    words = [row["keyword"] for row in out]
    assert words[0] == "air purifier"
    assert "hvac filter subscription" in words and "furnace filter sizes" in words
    assert "already tracked" not in words


def test_score_prefers_a_close_rising_search():
    close = {"keyword": "merv 13 filter", "volume": 400, "difficulty": 20, "fit": "core", "position": 8, "trend": [10, 10, 10, 40, 40, 40]}
    far = {"keyword": "air purifier", "volume": 400, "difficulty": 20, "fit": "core", "position": 80, "trend": [40, 40, 40, 10, 10, 10]}
    assert keyword_tracking._score(close) > keyword_tracking._score(far)


def test_organic_summary_uses_dataforseo_metrics():
    from app import research

    task = {"total_count": 1840, "metrics": {"organic": {"pos_1": 5, "pos_2_3": 7, "pos_4_10": 20, "pos_11_20": 30, "etv": 5321.4, "count": 1840}}}
    out = research._organic_summary(task, [{"keyword": "a", "position": 1}])
    assert out == {"keywords": 1840, "loaded": 1, "traffic": 5321, "top3": 12, "top10": 32, "top20": 62}


def test_track_runs_live_check_and_history(monkeypatch):
    from app import rank_tracking, research

    user = make_user("kwlive")
    monkeypatch.setattr(research, "track_term_metrics", lambda *a, **k: None)
    place = research.locations.Location(country="Canada", iso="CA", language="en", city="Calgary", region="Alberta")
    monkeypatch.setattr(rank_tracking, "_place", lambda db, u, host: (place, "en"))
    monkeypatch.setattr(research, "_budget", lambda *a, **k: None)
    seen = []

    def fake_serp(keyword, host, *, location_name, language, device):
        seen.append((location_name, device))
        return {"position": 6 if device == "desktop" else 9, "url": "https://voloair.example/x", "features": ["local_pack"], "owned": [], "top": []}, 0.002

    monkeypatch.setattr(rank_tracking, "serp_check", fake_serp)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        client = TestClient(app)
        body = client.post("/api/v1/research/keywords/track", json={"site": "voloair.example", "keyword": "merv 13 filter", "apply": False}).json()
        assert sorted(seen) == [("Calgary,Alberta,Canada", "desktop"), ("Calgary,Alberta,Canada", "mobile")]
        row = body["tracked"][0]
        assert row["position"] == 6 and row["rankSource"] == "live" and not row["measuring"]
        assert row["live"]["mobile"]["latest"]["position"] == 9
        assert row["live"]["desktop"]["latest"]["features"][0]["label"] == "Local map pack"
        # A manual re-check straight after is held back for 30 minutes.
        again = client.post("/api/v1/research/keywords/check-ranks", json={"site": "voloair.example"})
        assert again.status_code == 424
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        drop_user(user.id)


def test_website_topics_lead_keyword_seeds():
    seeds = keyword_tracking.website_topic_seeds(
        {
            "category": "air filters",
            "offers": ["merv 13 air filter"],
            "pages": [{"role": "product", "target": "home air purifier"}, {"role": "legal", "target": "privacy policy"}],
        },
        ["furnace filter replacement", "running shoes sale"],
        limit=4,
    )
    assert seeds[0] == "air filters"
    assert "merv 13 air filter" in seeds
    assert "home air purifier" in seeds
    assert "privacy policy" not in seeds
    tokens = keyword_tracking.topic_tokens(seeds)
    assert keyword_tracking.on_website_topic("best merv 13 filter", tokens)
    assert keyword_tracking.on_website_topic("plumber near me", tokens) is False
