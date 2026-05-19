"""
shared/llm.py — Anthropic primary, OpenAI fallback.

Single public function:

    from shared.llm import chat

    reply = chat(
        messages=[{"role": "user", "content": "What is a DSCR loan?"}],
        system="You are an expert real estate loan officer.",
        model_tier="standard",   # "standard" | "reasoning"
        max_tokens=1024,
    )

Model routing:
    standard   → claude-sonnet-4-5-20250929   (fast, cost-effective)
    reasoning  → claude-opus-4-7-20251001     (deep analysis, complex underwriting)

OpenAI fallback kicks in when ANTHROPIC_API_KEY is absent or an Anthropic error
is raised. OpenAI models: standard→gpt-4o-mini, reasoning→gpt-4o.
"""

import os
import time
from typing import Any, Literal

_anthropic_client = None
_openai_client = None

ANTHROPIC_STANDARD_MODEL = "claude-sonnet-4-5-20250929"
ANTHROPIC_REASONING_MODEL = "claude-opus-4-7-20251001"
OPENAI_STANDARD_MODEL = "gpt-4o-mini"
OPENAI_REASONING_MODEL = "gpt-4o"

MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"]
        )
    return _anthropic_client


def _get_openai():
    global _openai_client
    if _openai_client is None:
        import openai
        _openai_client = openai.OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY", "")
        )
    return _openai_client


def _anthropic_chat(
    messages: list[dict],
    system: str,
    model: str,
    max_tokens: int,
    temperature: float,
) -> str:
    client = _get_anthropic()
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    # Only add temperature for non-extended-thinking models
    if "opus-4" not in model:
        kwargs["temperature"] = temperature

    resp = client.messages.create(**kwargs)
    return resp.content[0].text


def _openai_chat(
    messages: list[dict],
    system: str,
    model: str,
    max_tokens: int,
    temperature: float,
) -> str:
    client = _get_openai()
    full_messages = [{"role": "system", "content": system}] + messages
    resp = client.chat.completions.create(
        model=model,
        messages=full_messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return resp.choices[0].message.content or ""


def chat_with_tools(
    messages: list[dict],
    system: str,
    tools: list[dict],
    model_tier: Literal["standard", "reasoning"] = "standard",
    max_tokens: int = 2048,
    temperature: float = 0.3,
) -> dict[str, Any]:
    """
    Anthropic tool-use call. Returns the structured response so the caller can
    dispatch tool_use blocks and loop with tool_result back.

    Args:
        messages: full conversation in Anthropic format (each message may have
                  `content` as a string OR a list of content blocks — the caller
                  passes through whatever it built up across iterations).
        tools:    Anthropic tools format — list of {name, description, input_schema}.

    Returns:
        {
          "stop_reason": "end_turn" | "tool_use" | ...,
          "content":     list of content blocks (text, tool_use),
          "model":       string,
        }

    Raises RuntimeError if no Anthropic key is configured — tool calling is
    not implemented on the OpenAI fallback path. The caller can decide to
    degrade to plain `chat()` if it catches this.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "chat_with_tools requires ANTHROPIC_API_KEY. Tool calling is not "
            "wired on the OpenAI fallback."
        )

    model = (
        ANTHROPIC_REASONING_MODEL if model_tier == "reasoning"
        else ANTHROPIC_STANDARD_MODEL
    )
    client = _get_anthropic()

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "tools": tools,
    }
    if "opus-4" not in model:
        kwargs["temperature"] = temperature

    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.messages.create(**kwargs)
            return {
                "stop_reason": resp.stop_reason,
                "content": [block.model_dump() for block in resp.content],
                "model": resp.model,
            }
        except Exception as e:
            last_err = e
            print(f"[llm] chat_with_tools attempt {attempt + 1} failed: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))

    raise RuntimeError(f"chat_with_tools exhausted retries. Last error: {last_err}")


def chat_with_vision(
    prompt: str,
    media: list[dict[str, Any]],
    system: str = "",
    model_tier: Literal["standard", "reasoning"] = "standard",
    max_tokens: int = 1024,
    temperature: float = 0.0,
) -> str:
    """
    Single-turn Claude vision call. Each `media` item is:
        {"data": <bytes>, "media_type": "image/jpeg" | "image/png" | "image/webp"
                                      | "image/gif" | "application/pdf"}

    Anthropic-only. Raises RuntimeError if ANTHROPIC_API_KEY is unset — the
    OpenAI fallback doesn't handle PDF natively, so document-OCR callers should
    treat absence of Claude as a hard failure rather than silently degrade.
    """
    import base64

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "chat_with_vision requires ANTHROPIC_API_KEY (no OpenAI fallback for PDFs)."
        )

    blocks: list[dict[str, Any]] = []
    for item in media:
        media_type = item["media_type"]
        data_b64 = base64.standard_b64encode(item["data"]).decode("ascii")
        if media_type == "application/pdf":
            blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": media_type, "data": data_b64},
            })
        else:
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data_b64},
            })
    blocks.append({"type": "text", "text": prompt})

    model = (
        ANTHROPIC_REASONING_MODEL if model_tier == "reasoning"
        else ANTHROPIC_STANDARD_MODEL
    )
    client = _get_anthropic()
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": blocks}],
    }
    if "opus-4" not in model:
        kwargs["temperature"] = temperature

    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.messages.create(**kwargs)
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    return block.text
            return ""
        except Exception as e:
            last_err = e
            print(f"[llm] chat_with_vision attempt {attempt + 1} failed: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))

    raise RuntimeError(f"chat_with_vision exhausted retries. Last error: {last_err}")


def chat(
    messages: list[dict],
    system: str = "",
    model_tier: Literal["standard", "reasoning"] = "standard",
    max_tokens: int = 1024,
    temperature: float = 0.3,
) -> str:
    """
    Call the LLM with Anthropic primary / OpenAI fallback.

    Returns the assistant's reply as a plain string.
    Raises RuntimeError if both providers fail after retries.
    """
    anthropic_model = (
        ANTHROPIC_REASONING_MODEL if model_tier == "reasoning"
        else ANTHROPIC_STANDARD_MODEL
    )
    openai_model = (
        OPENAI_REASONING_MODEL if model_tier == "reasoning"
        else OPENAI_STANDARD_MODEL
    )

    # Try Anthropic first
    if os.environ.get("ANTHROPIC_API_KEY"):
        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                return _anthropic_chat(messages, system, anthropic_model, max_tokens, temperature)
            except Exception as e:
                last_err = e
                print(f"[llm] Anthropic attempt {attempt + 1} failed: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY * (attempt + 1))
        print(f"[llm] Anthropic exhausted retries, falling back to OpenAI. Last err: {last_err}")

    # Fallback to OpenAI
    if os.environ.get("OPENAI_API_KEY"):
        for attempt in range(MAX_RETRIES):
            try:
                return _openai_chat(messages, system, openai_model, max_tokens, temperature)
            except Exception as e:
                print(f"[llm] OpenAI attempt {attempt + 1} failed: {e}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY * (attempt + 1))

    # Dev mode — no API keys configured
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        print("[llm] WARNING: No API keys configured. Returning stub response.")
        return "[STUB] No LLM API key configured. Set ANTHROPIC_API_KEY in .env to enable AI responses."

    raise RuntimeError("All LLM providers exhausted retries.")
