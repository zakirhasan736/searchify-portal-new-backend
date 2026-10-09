"""Regression tests for DataForSEO `40501 Invalid Field: 'web_search_country_iso_code'`.

The old code sent one payload to every engine:
  {"user_prompt", "model_name", "web_search": True, "web_search_country_iso_code": iso, "max_output_tokens": 800}
Gemini has no country field, Perplexity has no web_search field, Claude only accepts some countries,
and reasoning models need max_output_tokens >= 1024.
"""

import pytest

from app import llm_requests, research
from app.llm_requests import RequestError, build, pick_model

OLD_PAYLOAD_KEYS = {"user_prompt", "model_name", "web_search", "web_search_country_iso_code", "max_output_tokens"}

GPT_MINI = {"name": "gpt-4o-mini", "reasoning": False, "webSearch": True}
GPT_NANO = {"name": "gpt-4.1-nano", "reasoning": False, "webSearch": False}
O3_MINI = {"name": "o3-mini", "reasoning": True, "webSearch": False}
GEMINI = {"name": "gemini-2.5-flash", "reasoning": True, "webSearch": True}
SONAR = {"name": "sonar", "reasoning": False, "webSearch": True}
HAIKU = {"name": "claude-haiku-4-5", "reasoning": True, "webSearch": True}


def test_gemini_never_gets_a_country_field():
    payload, applied = build("gemini", prompt="best plumber in Dhaka", model=GEMINI, iso="BD", city="Dhaka")
    assert "web_search_country_iso_code" not in payload
    assert "web_search_city" not in payload
    assert payload["web_search"] is True
    assert applied["country"] == ""
    assert "not country-specific" in applied["note"]


def test_the_exact_failing_payload_is_no_longer_sent_to_any_engine():
    for engine, model in (("chat_gpt", GPT_MINI), ("gemini", GEMINI), ("perplexity", SONAR), ("claude", HAIKU)):
        payload, _ = build(engine, prompt="best seo agency", model=model, iso="BD")
        assert set(payload) <= llm_requests.FIELDS[engine], engine
    gemini, _ = build("gemini", prompt="best seo agency", model=GEMINI, iso="BD")
    assert set(gemini) != OLD_PAYLOAD_KEYS


def test_perplexity_has_no_web_search_field():
    payload, applied = build("perplexity", prompt="roofers near me", model=SONAR, iso="CA")
    assert "web_search" not in payload
    assert payload["web_search_country_iso_code"] == "CA"
    assert applied["webSearch"] is True


def test_claude_drops_unsupported_country():
    payload, applied = build("claude", prompt="dentist in Dhaka", model=HAIKU, iso="BD")
    assert "web_search_country_iso_code" not in payload
    assert "BD" in applied["note"]
    payload, applied = build("claude", prompt="dentist in Calgary", model=HAIKU, iso="CA", city="Calgary")
    assert payload["web_search_country_iso_code"] == "CA"
    assert payload["web_search_city"] == "Calgary"


def test_chatgpt_location_only_with_web_search():
    payload, _ = build("chat_gpt", prompt="x", model=GPT_MINI, iso="CA", city="Calgary")
    assert payload["web_search"] is True and payload["web_search_country_iso_code"] == "CA"
    payload, applied = build("chat_gpt", prompt="x", model=GPT_NANO, iso="CA")
    assert "web_search" not in payload and "web_search_country_iso_code" not in payload
    assert applied["note"]
    payload, _ = build("chat_gpt", prompt="x", model={**O3_MINI, "webSearch": True}, iso="CA")
    assert "web_search_country_iso_code" not in payload


def test_reasoning_models_get_enough_output_tokens():
    assert build("gemini", prompt="x", model=GEMINI)[0]["max_output_tokens"] >= 1024
    assert build("claude", prompt="x", model=HAIKU)[0]["max_output_tokens"] >= 1024
    assert build("chat_gpt", prompt="x", model=GPT_MINI)[0]["max_output_tokens"] <= 4096


@pytest.mark.parametrize("prompt", ["", "   ", "x" * 501])
def test_bad_prompts_fail_before_a_paid_call(prompt):
    with pytest.raises(RequestError):
        build("chat_gpt", prompt=prompt, model=GPT_MINI)


def test_pick_model_prefers_exact_cheap_names():
    perplexity = [{"model_name": "sonar-reasoning-pro", "reasoning": True}, {"model_name": "sonar-pro"}, {"model_name": "sonar"}]
    assert pick_model("perplexity", perplexity)["name"] == "sonar-pro"
    gpt = [
        {"model_name": "o3-mini", "reasoning": True, "web_search_supported": False},
        {"model_name": "gpt-4.1-nano", "web_search_supported": False},
        {"model_name": "gpt-4o-mini", "web_search_supported": True},
        {"model_name": "gpt-4.1-mini", "web_search_supported": True},
    ]
    assert pick_model("chat_gpt", gpt) == {"name": "gpt-4.1-mini", "reasoning": False, "webSearch": True}
    with pytest.raises(RequestError):
        pick_model("claude", [])


def test_chatgpt_and_claude_force_web_search():
    payload, applied = build("chat_gpt", prompt="best plumber", model=GPT_MINI, iso="CA")
    assert payload["web_search"] is True and payload["force_web_search"] is True
    assert applied["webSearch"] is True
    payload, _ = build("claude", prompt="best plumber", model=HAIKU, iso="CA")
    assert payload["web_search"] is True and payload["force_web_search"] is True


class FakeUser:
    id = 1
    role = "ROLE_USER"
    plan = "starter"


@pytest.fixture
def offline(monkeypatch):
    sent, saved = [], {}
    monkeypatch.setattr(research, "cached", lambda *a, **k: None)
    monkeypatch.setattr(research, "_record", lambda db, uid, kind, title: type("R", (), {"payload": saved.get(title)})() if title in saved else None)
    monkeypatch.setattr(research, "_save", lambda db, uid, kind, title, payload: saved.__setitem__(title, payload))
    monkeypatch.setattr(research, "_budget", lambda *a, **k: None)
    monkeypatch.setattr(research, "_spend", lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "plan_for", lambda *a, **k: None)
    monkeypatch.setattr(research.quotas, "consume", lambda *a, **k: None)
    models = {"chat_gpt": GPT_MINI, "gemini": GEMINI, "perplexity": SONAR, "claude": HAIKU}
    monkeypatch.setattr(research, "_model_for", lambda db, user, engine: models[engine])
    return sent, saved


def _answer(text: str, url: str = "") -> list:
    notes = [{"url": url, "title": "src"}] if url else []
    return [{"model_name": "m", "items": [{"sections": [{"text": text, "annotations": notes}]}]}]


def test_visibility_sends_engine_specific_payloads(offline, monkeypatch):
    sent, _ = offline

    def fake_call(method, path, payload=None, timeout=60):
        sent.append((path, payload[0]))
        return _answer("Acme Plumbing is a good pick.", "https://acme.ca/x"), 0.001

    monkeypatch.setattr(research, "_call", fake_call)
    out = research.visibility(
        None, FakeUser(), site="https://acme.ca", brand="Acme Plumbing", country="Calgary, Alberta",
        prompts=[{"id": i, "text": "best plumber in calgary", "engine": e} for i, e in enumerate(llm_requests.ENGINES)],
    )
    assert out["location"]["iso"] == "CA"
    by_engine = {path.split("/")[2]: payload for path, payload in sent}
    assert "web_search_country_iso_code" not in by_engine["gemini"]
    assert "web_search" not in by_engine["perplexity"]
    assert all(r["status"] == "ok" and r["mention"] is True and r["citation"] for r in out["results"])


def test_visibility_keeps_last_good_answer_when_a_call_fails(offline, monkeypatch):
    sent, saved = offline
    monkeypatch.setattr(research, "_call", lambda *a, **k: (_answer("Acme is listed."), 0.0))
    first = research.visibility(None, FakeUser(), site="acme.ca", brand="Acme", country="Canada",
                                prompts=[{"id": 1, "text": "plumber", "engine": "Gemini"}])
    good_time = first["results"][0]["fetchedAt"]

    def boom(*a, **k):
        raise research.ResearchError("DataForSEO: Invalid Field: 'web_search_country_iso_code'.")

    monkeypatch.setattr(research, "_call", boom)
    second = research.visibility(None, FakeUser(), site="acme.ca", brand="Acme", country="Canada",
                                 prompts=[{"id": 1, "text": "plumber", "engine": "Gemini"}], force=True)
    row = second["results"][0]
    assert row["status"] == "error" and row["stale"] is True
    assert row["fetchedAt"] == good_time and row["snippet"] == "Acme is listed."
    assert "Invalid Field" in row["error"]
    assert next(iter(saved.values()))["status"] == "ok"


def test_visibility_empty_answer_is_not_a_mention(offline, monkeypatch):
    monkeypatch.setattr(research, "_call", lambda *a, **k: ([{"items": []}], 0.0))
    out = research.visibility(None, FakeUser(), site="acme.ca", brand="Acme", country="Canada",
                              prompts=[{"id": 1, "text": "plumber", "engine": "ChatGPT"}])
    assert out["results"][0]["status"] == "empty"
    assert out["results"][0]["mention"] is None


def test_visibility_requires_a_market(offline):
    from app.locations import LocationError

    with pytest.raises(LocationError):
        research.visibility(None, FakeUser(), site="acme.ca", brand="Acme", country="",
                            prompts=[{"id": 1, "text": "plumber", "engine": "ChatGPT"}])


def test_normalize_engine_aliases_claude_ai():
    assert llm_requests.normalize_engine("Claude AI") == "Claude"
    assert llm_requests.normalize_engine("claude") == "Claude"
    assert llm_requests.normalize_engine("Claude") == "Claude"
    assert llm_requests.normalize_engine("Google AI Overview") == "Google AI Overviews"


def test_visibility_accepts_claude_and_rejects_soon_engines(offline, monkeypatch):
    sent = []

    def fake_call(method, path, payload=None, timeout=60):
        sent.append(path)
        return _answer("Acme Plumbing in Calgary.", "https://acme.ca/"), 0.001

    monkeypatch.setattr(research, "_call", fake_call)
    out = research.visibility(
        None, FakeUser(), site="https://acme.ca", brand="Acme", country="Canada",
        prompts=[
            {"id": 1, "text": "best plumber calgary", "engine": "Claude AI"},
            {"id": 2, "text": "best plumber calgary", "engine": "Copilot"},
            {"id": 3, "text": "best plumber calgary", "engine": "Grok"},
        ],
    )
    by_id = {r["id"]: r for r in out["results"]}
    assert by_id[1]["engine"] == "Claude" and by_id[1]["status"] == "ok"
    assert any("/claude/" in path for path in sent)
    assert by_id[2]["status"] == "error" and by_id[2]["code"] == "engine_unavailable"
    assert by_id[3]["status"] == "error" and by_id[3]["code"] == "engine_unavailable"
