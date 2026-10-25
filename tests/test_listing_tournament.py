"""Best-of-N listing tournament: strict fitting, blind judge, and win margin over the live listing."""

from __future__ import annotations

from unittest.mock import patch

from app.listing_judge import fit_desc_strict, fit_title_strict, judge_listings, weighted_total
from app.meta_writer import write_title_description

HOME = {
    "url": "https://agency.example/",
    "title": "Home | Agency Example",
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


BRAND_FIRST = {
    **HOME,
    "url": "https://sovereignstandard.ca/",
    "title": "Sovereign Standard | Web Design, SEO & Digital Marketing Agency in Calgary",
    "description": "Sovereign Standard helps businesses grow.",
    "h1": "We Build Brands That Grow",
    "headings": "Our Work | Free Consultation | Small Business Owners",
    "text": (
        "Sovereign Standard is a Calgary web design and SEO agency. We build fast websites and run SEO and "
        "Google Ads for Calgary businesses. See our work for clients. Free Consultation for Small Business Owners."
    ),
}
BRAND_FIRST_SITE = {"business": {"brand": "Sovereign Standard Inc.", "offers": ["web design", "SEO", "Google Ads"], "places": ["Calgary"]}, "page": {"role": ""}}
BRAND_FIRST_DRAFTS = (
    "TITLE: Calgary Web Design & SEO Agency | Sovereign Standard\n"
    "DESCRIPTION: Fast websites, SEO and Google Ads for Calgary businesses. Book a free consultation with Sovereign Standard.\n"
    "TITLE: Sovereign Standard | Calgary Web Design & SEO Agency\n"
    "DESCRIPTION: Fast websites, SEO and Google Ads for Calgary businesses. Book a free consultation with Sovereign Standard.\n"
)


def test_brand_first_live_title_with_slogan_h1_still_gets_a_suggestion():
    """Regression: brand-first live title made the offer phrase look like the brand and blocked every draft."""
    seen = []

    def judge(evidence, listings, salt=""):
        seen.extend(row["title"] for row in listings)
        return [{**row, "scores": {}, "score": 40.0 if i == 0 else 85.0, "accurate": True, "note": "n"} for i, row in enumerate(listings)]

    with (
        patch("app.meta_writer.openai_configured", return_value=True),
        patch("app.meta_writer._ask", return_value=(BRAND_FIRST_DRAFTS, "test", "writer")),
        patch("app.meta_writer.scrape_page", return_value=BRAND_FIRST),
        patch("app.dfs_meta.draft_from_research", return_value={}),
        patch("app.meta_writer.judge_listings", side_effect=judge),
    ):
        out = write_title_description(url=BRAND_FIRST["url"], profile={"business": "Sovereign Standard Inc."}, page=BRAND_FIRST, site=BRAND_FIRST_SITE, kind="home")
    # The live title is brand-first, so only the brand-first draft follows the site's format.
    assert "Sovereign Standard | Calgary Web Design & SEO Agency" in seen
    assert "Calgary Web Design & SEO Agency | Sovereign Standard" not in seen
    assert out["recommendKeep"] is False
    assert out["title"] == "Sovereign Standard | Calgary Web Design & SEO Agency"
    assert "Digital Marketing Agency in Calgary" not in out["title"]


def test_long_single_sentence_description_is_trimmed_at_a_clause():
    long = ("Fast websites, technical SEO audits and Google Ads campaigns for Calgary service businesses that want more "
            "calls and booked jobs, with clear monthly reporting on every lead and every dollar.")
    fitted = fit_desc_strict(long)
    assert fitted and len(fitted) <= 160 and fitted.endswith(".")


def test_judge_partial_scores_drop_only_unscored_drafts():
    from app import listing_judge

    reply = '{"items": [{"id": "L1", "relevance": 8, "keyword": 8, "click_appeal": 8, "specificity": 8, "brand_trust": 8, "readability": 8, "accuracy": 9, "note": "ok"}]}'
    rows = [{"title": "Live | Brand", "description": "Live."}, {"title": "Draft | Brand", "description": "Draft."}]
    with patch.object(listing_judge, "_order", return_value=[0, 1]), patch.object(listing_judge, "chat_text", return_value=(reply, "m", "r")):
        out = listing_judge.judge_listings("evidence", rows)
    assert out is not None and out[0]["score"] == 80.5
    assert out[1]["accurate"] is False


def test_title_format_follows_each_site():
    from app.meta_writer import follows_format, title_format

    start = title_format("Acme Plumbing | Drain Cleaning in Austin", "Acme Plumbing")
    end = title_format("Drain Cleaning in Austin – Acme Plumbing", "Acme Plumbing")
    assert start == {"position": "start", "sep": " | ", "brand": "Acme Plumbing"}
    assert end["position"] == "end" and end["sep"] == " – "
    assert follows_format("Acme Plumbing | Same-Day Drain Cleaning", start)
    assert not follows_format("Same-Day Drain Cleaning | Acme Plumbing", start)
    assert follows_format("Same-Day Drain Cleaning – Acme Plumbing", end)


def test_generator_prompt_has_no_preset_examples():
    from app.listing_judge import GENERATOR_SYSTEM

    for preset in ("Get a quote", "Book a call", "See plans", "e.g.", "Calgary", "kitchen"):
        assert preset not in GENERATOR_SYSTEM
    assert "TITLE FORMAT" in GENERATOR_SYSTEM
