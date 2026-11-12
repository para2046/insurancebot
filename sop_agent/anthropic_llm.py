"""Anthropic provider, using the official `anthropic` SDK.

- json(): structured outputs (`output_config.format` with a JSON schema) guarantee schema-valid JSON.
- text(): plain Messages API call with a system prompt and the conversation history.
- Refusal fallbacks are enabled (server-side `fallbacks="default"`), and `stop_reason` is checked before
  reading content.
"""
import copy
import json

import anthropic

from .llm import KEY_PAGES, LLMError

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000      # thinking counts toward max_tokens; leave room so replies are never cut off
EFFORT = "low"          # short conversational turns and extraction: low effort keeps latency down


def _to_anthropic_schema(schema: dict) -> dict:
    """Express nullable fields as anyOf [..., null] (the app's schema uses type arrays + enum incl. null)."""
    schema = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("type"), list) and "null" in node["type"]:
                base = [t for t in node["type"] if t != "null"][0]
                inner = {"type": base}
                if "enum" in node:
                    inner["enum"] = [v for v in node["enum"] if v is not None]
                rest = {k: v for k, v in node.items() if k not in ("type", "enum")}
                node.clear()
                node.update(rest)
                node["anyOf"] = [inner, {"type": "null"}]
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        return node

    return walk(schema)


class AnthropicLLM:
    provider = "anthropic"

    def __init__(self, key: str, model: str):
        self.model = model
        self.client = anthropic.Anthropic(api_key=key, max_retries=2)

    def _create(self, system: str, messages: list[dict], output_format: dict | None = None) -> str:
        output_config = {"effort": EFFORT}
        if output_format:
            output_config["format"] = output_format
        # The API needs the conversation to start with a user turn (the UI's greeting is an assistant turn).
        while messages and messages[0]["role"] != "user":
            messages = messages[1:]
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=messages,
                output_config=output_config,
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic rejected this API key (it may be mistyped, revoked or deleted). Check the "
                           f"key in the 🔑 field or create a new one at {KEY_PAGES['anthropic']}.") from e
        except anthropic.PermissionDeniedError as e:
            raise LLMError(f"This Anthropic key isn't allowed to use {self.model}. Check it at "
                           f"{KEY_PAGES['anthropic']}.") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"Anthropic doesn't offer the model '{self.model}'. Change the Model field "
                           "(or clear it to use the default).") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Anthropic is rate-limiting this key right now. Wait a moment and send again.") from e
        except anthropic.BadRequestError as e:
            if "credit" in str(e).lower() or "billing" in str(e).lower():
                raise LLMError("This Anthropic account is out of credit. Add credit at "
                               "https://platform.claude.com/settings/billing.") from e
            raise LLMError(f"Anthropic rejected the request: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error ({e.status_code}). Please try again shortly.") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Couldn't reach Anthropic's API. Check the connection and try again.") from e

        if response.stop_reason == "refusal":
            raise LLMError("The model declined to answer this message. Please rephrase, or ask for a human "
                           "representative.")
        return "".join(b.text for b in response.content if b.type == "text").strip()

    def json(self, system: str, user: str, max_tokens: int = MAX_TOKENS, schema: dict | None = None) -> dict:
        fmt = {"type": "json_schema", "schema": _to_anthropic_schema(schema)} if schema else None
        prompt = user if schema else user + "\n\nRespond with a single JSON object only."
        raw = self._create(system, [{"role": "user", "content": prompt}], fmt)
        try:
            start, end = raw.find("{"), raw.rfind("}")
            return json.loads(raw[start:end + 1] if start >= 0 else raw)
        except json.JSONDecodeError as e:
            raise LLMError(f"Model did not return valid JSON: {raw[:200]}") from e

    def text(self, system: str, messages: list[dict], max_tokens: int = MAX_TOKENS) -> str:
        return self._create(system, [dict(m) for m in messages])
