"""Thin OpenRouter client (OpenAI-compatible API)."""
import json
import re
import time

from openai import OpenAI

from . import config


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        key = api_key or config.api_key()
        if not key:
            raise LLMError("Please enter your LLM API key (OpenRouter) in the 🔑 field at the top of the page "
                           "(get one at https://openrouter.ai/keys), then send your message again.")
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
