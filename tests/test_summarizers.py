"""Tests for the model registries in summarizer classes.

These tests don't make network calls — they just check that the model IDs
configured in MODELS dicts haven't drifted from what the providers actually
accept. If a provider deprecates a model, you'd update both the constant
here and the source.
"""
from meeting_notes.ai_summarizer import (
    AnthropicSummarizer,
    OpenAISummarizer,
    OpenRouterSummarizer,
)


def test_anthropic_haiku_is_current_4_5():
    """eduspano/jmalobicky bumped Haiku from 3.5 to 4.5."""
    haiku = AnthropicSummarizer.MODELS["haiku"]
    assert haiku["id"] == "claude-haiku-4-5-20251001"
    assert "4.5" in haiku["name"]


def test_anthropic_sonnet_is_current_4_6():
    """eduspano bumped Sonnet to 4.6."""
    sonnet = AnthropicSummarizer.MODELS["sonnet"]
    assert sonnet["id"] == "claude-sonnet-4-6"
    assert "4.6" in sonnet["name"]


def test_anthropic_no_deprecated_3_5_ids_remain():
    """Guard against accidental revert to deprecated 3.5 model IDs."""
    for tier, info in AnthropicSummarizer.MODELS.items():
        assert "3-5" not in info["id"], f"{tier} still references deprecated 3.5 model"
        assert "3.5" not in info["name"], f"{tier} still labelled as 3.5"


def test_anthropic_models_have_required_fields():
    for tier, info in AnthropicSummarizer.MODELS.items():
        for field in ("id", "name", "cost_per_1k_input", "cost_per_1k_output"):
            assert field in info, f"Anthropic {tier} missing {field}"
        assert isinstance(info["cost_per_1k_input"], (int, float))


def test_openai_models_have_required_fields():
    for tier, info in OpenAISummarizer.MODELS.items():
        for field in ("id", "name", "cost_per_1k_input", "cost_per_1k_output"):
            assert field in info, f"OpenAI {tier} missing {field}"


def test_openrouter_models_have_required_fields():
    for tier, info in OpenRouterSummarizer.MODELS.items():
        for field in ("id", "name"):
            assert field in info, f"OpenRouter {tier} missing {field}"


def test_anthropic_summarizer_requires_api_key(monkeypatch):
    """Without an API key (and no env var), construction should fail clearly."""
    import pytest
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key"):
        AnthropicSummarizer(api_key=None, model="haiku")


def test_anthropic_summarizer_rejects_unknown_model():
    """Unknown model tier should not be silently accepted."""
    import pytest
    with pytest.raises((KeyError, ValueError)):
        AnthropicSummarizer(api_key="sk-ant-test", model="not-a-tier")


def test_openai_tiers_are_current_gpt5x():
    """OpenAI tier map should point at the current GPT-5.x lineup
    (GA July 2026), not the long-deprecated 4o family."""
    assert OpenAISummarizer.MODELS["mini"]["id"] == "gpt-5.4-mini"
    assert OpenAISummarizer.MODELS["standard"]["id"] == "gpt-5.6-terra"
    assert OpenAISummarizer.MODELS["best"]["id"] == "gpt-5.6-sol"


def test_openai_no_deprecated_4o_ids_remain():
    for tier, info in OpenAISummarizer.MODELS.items():
        assert "4o" not in info["id"], f"OpenAI {tier} still on 4o-era model {info['id']}"


def test_openai_summarize_omits_temperature(monkeypatch):
    """GPT-5.x reasoning models reject non-default temperature on
    chat.completions — the call must not pin one."""
    s = OpenAISummarizer(api_key="sk-test", model="best")

    calls = []

    class _Usage:
        prompt_tokens = 10
        completion_tokens = 5

    class _Msg:
        content = "OVERVIEW:\nshort test overview"

    class _Choice:
        message = _Msg()

    class _Resp:
        usage = _Usage()
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    s.client = _Client()
    s.summarize("a short transcript")

    assert calls, "summarize() should call chat.completions.create"
    assert "temperature" not in calls[0]
    assert calls[0]["model"] == "gpt-5.6-sol"
