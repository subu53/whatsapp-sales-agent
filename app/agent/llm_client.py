"""Provider-agnostic LLM client.

brain.py talks to this interface only — swapping providers is a config
change (LLM_PROVIDER=anthropic|deepseek in .env), never a rewrite of the
agent loop. Both providers are normalized to the same response shape
(Anthropic's, since it's the one brain.py was written against):

    response.stop_reason -> "tool_use" | "end_turn" | ...
    response.content     -> list of blocks, each either
        TextBlock(type="text", text=...)
        ToolUseBlock(type="tool_use", id=..., name=..., input={...})

Caveat, stated plainly: the DeepSeekClient below is written correctly
against DeepSeek's documented OpenAI-compatible chat-completions API, but
it has NOT been exercised against a live DeepSeek response — the sandbox
this was built in can reach api.anthropic.com but not api.deepseek.com
(same network allowlist restriction that blocks Twilio and tunnel
services; see README.md). It's covered by unit tests with a mocked HTTP
response, which check the translation logic is correct, not that
DeepSeek's live API behaves exactly as documented. Smoke-test it for real
the first time you run this outside the sandbox, before trusting it with
real customers.
"""
import json
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class NormalizedResponse:
    stop_reason: str
    content: list


class AnthropicClient:
    """Thin pass-through — the Anthropic SDK's response shape already IS
    the normalized shape used throughout this codebase, so there's nothing
    to translate. Also switches on prompt caching for the system prompt:
    it's identical on every single call (only the conversation history and
    tool results change), which is exactly what caching is for — roughly a
    3-4x reduction in the input-token cost of a live conversation. See
    FRAMEWORK.md's cost-modeling section for the actual numbers."""

    def __init__(self, api_key: str, model: str):
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def create(self, messages: list, system: str, tools: list, max_tokens: int) -> Any:
        return self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            tools=tools,
        )


class DeepSeekClient:
    """DeepSeek's API is OpenAI-compatible chat-completions + function
    calling, not Anthropic-shaped — this adapter translates both
    directions so brain.py never has to know which provider it's talking
    to. DeepSeek applies its own context caching automatically server-side
    (no cache_control equivalent needed on this path)."""

    BASE_URL = "https://api.deepseek.com"

    def __init__(self, api_key: str, model: str = "deepseek-chat"):
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key, base_url=self.BASE_URL)
        self.model = model

    def create(self, messages: list, system: str, tools: list, max_tokens: int) -> NormalizedResponse:
        oa_messages = [{"role": "system", "content": system}] + _anthropic_messages_to_openai(messages)
        oa_tools = [_anthropic_tool_to_openai(t) for t in tools] if tools else None

        resp = self._client.chat.completions.create(
            model=self.model,
            messages=oa_messages,
            tools=oa_tools,
            max_tokens=max_tokens,
        )
        return _openai_response_to_normalized(resp)


def build_llm_client(settings) -> Any:
    provider = (settings.llm_provider or "anthropic").lower()
    if provider == "deepseek":
        return DeepSeekClient(api_key=settings.deepseek_api_key, model=settings.deepseek_model)
    if provider == "anthropic":
        return AnthropicClient(api_key=settings.anthropic_api_key, model=settings.anthropic_model)
    raise ValueError(f"Unknown LLM_PROVIDER '{provider}' — expected 'anthropic' or 'deepseek'.")


# --- Anthropic <-> OpenAI message/tool translation (DeepSeek path only) ---

def _block_attr(block, name: str):
    """block is either one of our own dataclasses (attribute access) or a
    plain dict (as built manually in a couple of call sites) — support
    both without the caller having to care which."""
    return getattr(block, name, None) if hasattr(block, name) else block[name]


def _anthropic_messages_to_openai(messages: list) -> list:
    oa: list = []
    for m in messages:
        role, content = m["role"], m["content"]

        if isinstance(content, str):
            oa.append({"role": role, "content": content})
            continue

        if role == "assistant":
            text_parts, tool_calls = [], []
            for block in content:
                btype = _block_attr(block, "type")
                if btype == "text":
                    text_parts.append(_block_attr(block, "text"))
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": _block_attr(block, "id"),
                        "type": "function",
                        "function": {
                            "name": _block_attr(block, "name"),
                            "arguments": json.dumps(_block_attr(block, "input")),
                        },
                    })
            oa_msg: dict = {"role": "assistant", "content": " ".join(text_parts) or None}
            if tool_calls:
                oa_msg["tool_calls"] = tool_calls
            oa.append(oa_msg)
            continue

        if role == "user" and isinstance(content, list) and content and content[0].get("type") == "tool_result":
            for block in content:
                oa.append({
                    "role": "tool",
                    "tool_call_id": block["tool_use_id"],
                    "content": block["content"],
                })
            continue

        oa.append({"role": role, "content": content})
    return oa


def _anthropic_tool_to_openai(t: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        },
    }


def _openai_response_to_normalized(resp) -> NormalizedResponse:
    choice = resp.choices[0]
    msg = choice.message
    blocks: list = []
    if msg.content:
        blocks.append(TextBlock(text=msg.content))
    if msg.tool_calls:
        for tc in msg.tool_calls:
            try:
                parsed_input = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                # A model returning malformed JSON arguments is a real
                # failure mode, not a hypothetical one — surface it as a
                # tool result the model can see and recover from, the same
                # way brain.py already handles any other tool exception,
                # rather than crashing the loop.
                parsed_input = {"_malformed_arguments": tc.function.arguments}
            blocks.append(ToolUseBlock(id=tc.id, name=tc.function.name, input=parsed_input))
    stop_reason = "tool_use" if msg.tool_calls else "end_turn"
    return NormalizedResponse(stop_reason=stop_reason, content=blocks)
