"""OpenAI calls use a model the project actually has, and say plainly when it has none."""

import httpx
import pytest

from app import config, openai_client


class FakeClient:
    """Stands in for httpx.Client. `allowed` is the set of models this project may call."""

    allowed: set[str] = set()
    listed: set[str] | None = None
    calls: list[str] = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        if self.listed is None:
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"data": [{"id": m} for m in self.listed]})

    def post(self, url, headers=None, json=None):
        model = json["model"]
        FakeClient.calls.append(model)
        if model not in self.allowed:
            return httpx.Response(403, text=f'{{"error": {{"message": "Project `proj_x` does not have access to model `{model}`"}}}}')
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok from " + model}}]})


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(openai_client.httpx, "Client", FakeClient)
    openai_client._models.update(at=0.0, ids=None)
    openai_client._denied.clear()
    FakeClient.calls = []
    yield
    openai_client._models.update(at=0.0, ids=None)
    openai_client._denied.clear()


def test_uses_a_model_the_project_has_when_none_of_the_chain_is_allowed():
    FakeClient.allowed = {"gpt-4o", "gpt-4o-mini"}
    FakeClient.listed = {"gpt-4o", "gpt-4o-mini", "dall-e-3", "text-embedding-3-small"}
    text, model, _ = openai_client.chat_text(system="s", user="u", kind="analyst", force_astra=True)
    assert model == "gpt-4o" and text == "ok from gpt-4o"
    assert FakeClient.calls == ["gpt-4o"]  # no wasted calls on models the project can't use


def test_names_every_refused_model_when_listing_is_unavailable():
    FakeClient.allowed = set()
    FakeClient.listed = None
    with pytest.raises(RuntimeError) as err:
        openai_client.chat_text(system="s", user="u", kind="analyst", force_astra=True)
    message = str(err.value)
    assert "no access to" in message and "gpt-4.1" in message and "gpt-6-astra" in message
    assert "platform.openai.com" in message
    # Refused models are skipped on the next call instead of being retried every time.
    FakeClient.calls = []
    with pytest.raises(RuntimeError):
        openai_client.chat_text(system="s", user="u", kind="analyst", force_astra=True)
    assert FakeClient.calls == []


def test_project_with_no_chat_models_gets_a_clear_message():
    FakeClient.allowed = set()
    FakeClient.listed = {"text-embedding-3-small", "dall-e-3"}
    with pytest.raises(RuntimeError) as err:
        openai_client.chat_text(system="s", user="u", kind="fast")
    assert "no chat model" in str(err.value)
