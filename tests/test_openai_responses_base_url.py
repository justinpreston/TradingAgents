"""The Responses API only exists on native OpenAI; a custom base_url on the
openai provider must fall back to Chat Completions (#1024)."""

from __future__ import annotations

import pytest

from tradingagents.llm_clients.openai_client import (
    OpenAIClient,
    _is_native_openai_base_url,
)


@pytest.mark.unit
class NativeBaseUrlTests:
    def test_unset_is_native(self):
        assert _is_native_openai_base_url(None) is True
        assert _is_native_openai_base_url("") is True

    def test_openai_hosts_are_native(self):
        assert _is_native_openai_base_url("https://api.openai.com/v1") is True
        assert _is_native_openai_base_url("api.openai.com/v1") is True

    def test_custom_endpoints_are_not_native(self):
        assert _is_native_openai_base_url("http://localhost:1234/v1") is False
        assert _is_native_openai_base_url("https://my-gateway.example.com/v1") is False
        assert _is_native_openai_base_url("https://api.openai.com.evil.com/v1") is False


@pytest.mark.unit
class ResponsesApiSelectionTests:
    def test_native_openai_enables_responses_api(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        llm = OpenAIClient("gpt-5.5", provider="openai").get_llm()
        assert getattr(llm, "use_responses_api", False) is True

    def test_custom_base_url_disables_responses_api(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        llm = OpenAIClient(
            "gpt-5.5", base_url="http://localhost:1234/v1", provider="openai"
        ).get_llm()
        # use_responses_api should be absent/False so the client speaks Chat Completions.
        assert getattr(llm, "use_responses_api", False) is False


@pytest.mark.unit
class TestCopilotResponsesApi:
    @pytest.mark.parametrize("model", ["gpt-5.5", "gpt-6-sol", "gpt-6.1-sol"])
    def test_gpt5_and_later_use_responses_api(self, monkeypatch, model):
        monkeypatch.setenv("GITHUB_TOKEN", "gh-test")
        llm = OpenAIClient(model, provider="copilot").get_llm()
        assert getattr(llm, "use_responses_api", False) is True

    @pytest.mark.parametrize("model", ["gpt-4.1", "gpt-4o", "claude-sonnet-5.5"])
    def test_older_and_claude_models_use_chat_completions(self, monkeypatch, model):
        monkeypatch.setenv("GITHUB_TOKEN", "gh-test")
        llm = OpenAIClient(model, provider="copilot").get_llm()
        assert not getattr(llm, "use_responses_api", False)
