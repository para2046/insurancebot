"""Thin OpenRouter client (OpenAI-compatible API)."""
import json
import re
import time

from openai import OpenAI

from . import config


class LLMError(RuntimeError):
    pass


KEY_HELP = ("Please paste your OpenRouter API key (it starts with sk-or-) into the 🔑 field at the top of the "
            "page — get one at https://openrouter.ai/keys — then send your message again.")


def clean_key(raw: str | None) -> str:
    """Accept common paste mistakes: quotes, an `OPENROUTER_API_KEY=` prefix, a `Bearer ` prefix, whitespace."""
    key = (raw or "").strip().strip("'\"").strip()
    key = re.sub(r"^(export\s+)?OPENROUTER_API_KEY\s*=\s*", "", key, flags=re.I).strip("'\"").strip()
    key = re.sub(r"^Bearer\s+", "", key, flags=re.I)
    return re.sub(r"\s+", "", key)


class LLM:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        key = clean_key(api_key) or config.api_key()
        if not key:
            raise LLMError(KEY_HELP)
        if not key.startswith("sk-or-"):
            raise LLMError("That doesn't look like an OpenRouter API key (your browser may have auto-filled a "
                           "saved password). Clear the 🔑 field and paste only the key — it starts with "
                           "sk-or- (get one at https://openrouter.ai/keys).")
        self.model = model or config.MODEL
        self.client = OpenAI(
            base_url=config.OPENROUTER_BASE_URL,
            api_key=key,
            default_headers={"X-Title": "Insurance SOP Agent Demo"},
        )

    def _call(self, messages: list[dict], max_tokens: int, json_mode: bool, schema: dict | None = None) -> str:
        kwargs = {}
        if schema:
            kwargs["response_format"] = {"type": "json_schema",
                                         "json_schema": {"name": "output", "strict": True, "schema": schema}}
        elif json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        for attempt in range(3):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model, messages=messages, max_tokens=max_tokens, **kwargs
                )
                return (resp.choices[0].message.content or "").strip()
            except Exception as e:  # surface API problems (bad key, no credit) to the UI
                status = getattr(e, "status_code", None)
                if attempt < 2 and (status is None or status == 429 or status >= 500):
                    time.sleep(2 * (attempt + 1))
                    continue
                if status == 401:
                    raise LLMError("OpenRouter rejected this API key (it may be mistyped, revoked or deleted). "
                                   "Check the key in the 🔑 field — it starts with sk-or- — or create a new one "
                                   "at https://openrouter.ai/keys.") from e
                if status == 402:
                    raise LLMError("This OpenRouter key is out of credit or over its spending limit. Add credit "
                                   "or raise the key's limit at https://openrouter.ai/keys.") from e
                raise LLMError(f"Model call failed: {e}") from e

    def json(self, system: str, user: str, max_tokens: int = config.MAX_TOKENS, schema: dict | None = None) -> dict:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for _ in range(3):
            raw = self._call(messages, max_tokens, json_mode=True, schema=schema)
            match = re.search(r"\{.*\}", raw, re.S)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
        raise LLMError(f"Model did not return valid JSON: {raw[:200]}")

    def text(self, system: str, messages: list[dict], max_tokens: int = config.MAX_TOKENS) -> str:
        return self._call([{"role": "system", "content": system}, *messages], max_tokens, json_mode=False)
