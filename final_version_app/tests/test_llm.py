"""Tests for model-provider HTTP routing and proxy policy."""

from __future__ import annotations

from final_version_app.infra import llm


class _FakeHttpClient:
    def __init__(self, *, trust_env: bool):
        self.trust_env = trust_env


class _FakeChatOpenAI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _build_deepseek(monkeypatch, proxy_value: str | None):
    monkeypatch.setattr(llm, "MODEL", "deepseek-v4-pro")
    monkeypatch.setattr(llm, "ChatOpenAI", _FakeChatOpenAI)
    monkeypatch.setattr(llm.httpx, "Client", _FakeHttpClient)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    if proxy_value is None:
        monkeypatch.delenv("LLM_TRUST_ENV_PROXY", raising=False)
    else:
        monkeypatch.setenv("LLM_TRUST_ENV_PROXY", proxy_value)
    llm._shared_http_client.cache_clear()
    return llm.get_llm(max_tokens=128)


def test_model_http_client_ignores_implicit_proxy_by_default(monkeypatch):
    model = _build_deepseek(monkeypatch, None)
    assert model.kwargs["http_client"].trust_env is False


def test_model_http_client_can_enable_intentional_proxy(monkeypatch):
    model = _build_deepseek(monkeypatch, "true")
    assert model.kwargs["http_client"].trust_env is True
