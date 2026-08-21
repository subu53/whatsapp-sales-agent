"""Tests for the Anthropic<->OpenAI translation used by the DeepSeek
adapter. These test the TRANSLATION LOGIC ONLY, with hand-built fake
objects standing in for real Anthropic/OpenAI SDK response objects — no
live network call to either provider. DeepSeek's API is not reachable
from the sandbox this was built in (see llm_client.py's module
docstring); this is deliberately the honest limit of what could be
verified here."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.llm_client import (  # noqa: E402
    TextBlock,
    ToolUseBlock,
    _anthropic_messages_to_openai,
    _anthropic_tool_to_openai,
    _openai_response_to_normalized,
    build_llm_client,
)


def test_plain_string_messages_pass_through():
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert _anthropic_messages_to_openai(messages) == messages


def test_assistant_tool_use_block_translates_to_openai_tool_call():
    messages = [{
        "role": "assistant",
        "content": [ToolUseBlock(id="tu_1", name="search_catalogue", input={"query": "treadmill"})],
    }]
    result = _anthropic_messages_to_openai(messages)
    assert result[0]["role"] == "assistant"
    assert result[0]["tool_calls"][0]["id"] == "tu_1"
    assert result[0]["tool_calls"][0]["function"]["name"] == "search_catalogue"
    assert '"query": "treadmill"' in result[0]["tool_calls"][0]["function"]["arguments"]


def test_tool_result_turn_expands_to_openai_tool_messages():
    messages = [{
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": '{"results": []}'}],
    }]
    result = _anthropic_messages_to_openai(messages)
    assert result[0]["role"] == "tool"
    assert result[0]["tool_call_id"] == "tu_1"
    assert result[0]["content"] == '{"results": []}'


def test_anthropic_tool_schema_translates_to_openai_function_schema():
    tool = {
        "name": "search_catalogue",
        "description": "Search products.",
        "input_schema": {"type": "object", "properties": {}},
    }
    oa = _anthropic_tool_to_openai(tool)
    assert oa["type"] == "function"
    assert oa["function"]["name"] == "search_catalogue"
    assert oa["function"]["parameters"] == tool["input_schema"]


def _fake_openai_response(content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def test_openai_text_response_normalizes_to_end_turn():
    resp = _fake_openai_response(content="Here's the price.")
    normalized = _openai_response_to_normalized(resp)
    assert normalized.stop_reason == "end_turn"
    assert isinstance(normalized.content[0], TextBlock)
    assert normalized.content[0].text == "Here's the price."


def test_openai_tool_call_response_normalizes_to_tool_use():
    fake_call = SimpleNamespace(
        id="call_1", function=SimpleNamespace(name="search_catalogue", arguments='{"query": "bench"}')
    )
    resp = _fake_openai_response(tool_calls=[fake_call])
    normalized = _openai_response_to_normalized(resp)
    assert normalized.stop_reason == "tool_use"
    assert isinstance(normalized.content[0], ToolUseBlock)
    assert normalized.content[0].name == "search_catalogue"
    assert normalized.content[0].input == {"query": "bench"}


def test_malformed_tool_arguments_do_not_crash_translation():
    fake_call = SimpleNamespace(
        id="call_2", function=SimpleNamespace(name="search_catalogue", arguments="not valid json{{")
    )
    resp = _fake_openai_response(tool_calls=[fake_call])
    normalized = _openai_response_to_normalized(resp)
    assert normalized.content[0].input["_malformed_arguments"] == "not valid json{{"


def test_build_llm_client_selects_anthropic_by_default():
    from app.config import Settings
    client = build_llm_client(Settings(anthropic_api_key="sk-ant-fake", llm_provider="anthropic"))
    assert type(client).__name__ == "AnthropicClient"


def test_build_llm_client_selects_deepseek():
    from app.config import Settings
    client = build_llm_client(Settings(deepseek_api_key="sk-fake", llm_provider="deepseek"))
    assert type(client).__name__ == "DeepSeekClient"


def test_build_llm_client_rejects_unknown_provider():
    from app.config import Settings
    try:
        build_llm_client(Settings(llm_provider="made-up-provider"))
        assert False, "expected ValueError"
    except ValueError:
        pass
