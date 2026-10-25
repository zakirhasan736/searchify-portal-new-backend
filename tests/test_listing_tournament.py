"""Best-of-N listing tournament: strict fitting, blind judge, and win margin over the live listing."""

from __future__ import annotations

from unittest.mock import patch

from app.listing_judge import fit_desc_strict, fit_title_strict, judge_listings, weighted_total
from app.meta_writer import write_title_description

HOME = {
    "url": "https://agency.example/",
    "title": "Agency Example | Home",
    "description": "Welcome to Agency Example. We do websites and marketing.",
    "h1": "Calgary web design and SEO",
    "headings": "Web Design | SEO | Google Ads",
    "text": (
        "Agency Example is a Calgary web design and SEO agency. We build fast WordPress websites, "
        "run technical SEO audits and manage Google Ads for local service businesses in Calgary."
    ),
    "words": 60,
    "ogTitle": "", "ogDescription": "", "schemaName": "Agency Example", "schemaDescription": "",
    "schemaTypes": ["WebPage"], "nav": [], "faqs": [], "videos": [], "images": [],
    "canonical": "https://agency.example/", "ogImage": "",
}
SITE = {"business": {"brand": "Agency Example", "offers": ["web design", "SEO", "Google Ads"], "places": ["Calgary"]}, "page": {"role": ""}}
PROFILE = {"business": "Agency Example", "services": "web design, SEO, Google Ads", "areas": "Calgary"}

FIVE = (
    "TITLE: Calgary Web Design & SEO Agency | Agency Example\n"
    "DESCRIPTION: Fast WordPress websites, technical SEO audits and Google Ads for Calgary service businesses. Get a quote from Agency Example.\n"
    "TITLE: Web Design & SEO for Calgary Businesses | Agency Example\n"
    "DESCRIPTION: Agency Example builds fast WordPress sites and runs technical SEO audits so Calgary service businesses get found and get calls.\n"
    "TARGET: web design calgary\n"
    "REASON: Live title is just the brand plus Home.\n"
)


def _fake_judge(scores_by_title: dict[str, float]):
    def judge(_evidence, listings, salt=""):
        out = []
        for row in listings:
            score = scores_by_title.get(row["title"], 50.0)
            out.append({**row, "scores": {}, "score": score, "accurate": True, "note": "n"})
        return out
    return judge


def _run(judge, ask_text=FIVE):
    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", return_value=(ask_text, "test", "writer")),
        patch("app.meta_writer.scrape_page", return_value=HOME),
        patch("app.dfs_meta.draft_from_research", return_value={}),
        patch("app.meta_writer.judge_listings", side_effect=judge),
    ):
        return write_title_description(
            url=HOME["url"], profile=PROFILE, page=HOME, site=SITE, kind="home", queries=["web design calgary"],
        )


def test_fit_title_never_chops_mid_phrase():
    long = "Calgary Web Design, SEO, Google Ads & Branding Agency | Agency Example"
    fitted = fit_title_strict(long, "Agency Example")
    assert fitted and len(fitted) <= 60
    assert fitted.endswith("| Agency Example")
    assert not fitted.rstrip().endswith(("&", "and", ",", "in"))


def test_fit_title_rejects_when_it_cannot_fit_cleanly():
    assert fit_title_strict("Supercalifragilisticexpialidocious Extraordinarily Unbelievable Services Here", "") == ""


def test_fit_desc_ends_on_sentence_or_rejects():
    text = ("Fast WordPress websites and technical SEO audits for Calgary service businesses that want more calls. "
            "We also run Google Ads campaigns and report every month on leads and spend.")
    fitted = fit_desc_strict(text)
    assert fitted.endswith(".") and len(fitted) <= 160
    assert fit_desc_strict("word " * 60) == ""


def test_weighted_total_is_0_to_100():
    assert weighted_total({k: 10 for k in ("relevance", "keyword", "click_appeal", "specificity", "brand_trust", "readability", "accuracy")}) == 100.0
    assert weighted_total({}) == 0.0


def test_judge_returns_none_without_model():
    # No API key in tests: the judge must fail soft so the heuristic fallback runs.
    assert judge_listings("evidence", [{"title": "a", "description": "b"}]) is None


def test_judge_winner_replaces_weak_live_listing():
    out = _run(_fake_judge({
        HOME["title"]: 41.0,
        "Calgary Web Design & SEO Agency | Agency Example": 86.0,
        "Web Design & SEO for Calgary Businesses | Agency Example": 80.0,
    }))
    assert out["recommendKeep"] is False
    assert out["beatsLive"] is True
    assert out["title"] == "Calgary Web Design & SEO Agency | Agency Example"
    assert out["judgeScore"] == 86.0 and out["liveScore"] == 41.0
    assert out["alternatives"] and out["alternatives"][0]["judgeScore"] == 80.0
    assert out["analysis"]["judge"]["used"] is True


def test_judge_keeps_live_when_no_candidate_clears_margin():
    calls = {"n": 0}
    scores = {HOME["title"]: 80.0, "Calgary Web Design & SEO Agency | Agency Example": 82.0}

    def judge(evidence, listings, salt=""):
        calls["n"] += 1
        return _fake_judge(scores)(evidence, listings, salt)

    out = _run(judge)
    assert out["recommendKeep"] is True
    assert out["beatsLive"] is False
    assert out["title"] == HOME["title"]
    assert "blind review" in out["reason"]


def test_refine_round_can_produce_the_winner():
    refined = (
        "TITLE: Calgary Web Design That Brings Calls | Agency Example\n"
        "DESCRIPTION: Fast WordPress websites with technical SEO built in, for Calgary service businesses. Get a quote from Agency Example.\n"
        "TARGET: web design calgary\nREASON: Adds the outcome.\n"
    )
    replies = iter([(FIVE, "test", "writer"), (refined, "test", "writer")])
    scores = {
        HOME["title"]: 70.0,
        "Calgary Web Design & SEO Agency | Agency Example": 72.0,
        "Web Design & SEO for Calgary Businesses | Agency Example": 71.0,
        "Calgary Web Design That Brings Calls | Agency Example": 88.0,
    }
    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", side_effect=lambda *_a, **_k: next(replies)),
        patch("app.meta_writer.scrape_page", return_value=HOME),
        patch("app.dfs_meta.draft_from_research", return_value={}),
        patch("app.meta_writer.judge_listings", side_effect=_fake_judge(scores)),
    ):
        out = write_title_description(url=HOME["url"], profile=PROFILE, page=HOME, site=SITE, kind="home", queries=["web design calgary"])
    assert out["title"] == "Calgary Web Design That Brings Calls | Agency Example"
    assert out["recommendKeep"] is False


def test_candidates_dropping_brand_or_inventing_facts_never_reach_judge():
    bad = (
        "TITLE: Award-Winning Calgary Web Design Agency\n"
        "DESCRIPTION: The #1 top-rated Calgary web design agency with 5 core services and 300 happy clients. Call today.\n"
        "TITLE: Calgary Web Design & SEO Agency | Agency Example\n"
        "DESCRIPTION: Fast WordPress websites, technical SEO audits and Google Ads for Calgary service businesses. Get a quote from Agency Example.\n"
    )
    seen = []

    def judge(evidence, listings, salt=""):
        seen.extend(row["title"] for row in listings)
        return _fake_judge({})(evidence, listings, salt)

    _run(judge, ask_text=bad)
    assert all("Award" not in t for t in seen)
