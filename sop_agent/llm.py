"""Model client. The visitor's key decides the provider:

    sk-or-...   -> OpenRouter (OpenAI-compatible API)
    sk-ant-...  -> Anthropic (official SDK, see anthropic_llm.py)
    sk-...      -> OpenAI (direct)

Every provider exposes the same two calls used by the agent: json() (schema-constrained) and text().
"""
import json
import re
import time

from openai import OpenAI

from . import config


class LLMError(RuntimeError):
    pass


KEY_HELP = ("Please paste your API key into the 🔑 field at the top of the page — an OpenRouter (sk-or-…), "
            "OpenAI (sk-…) or Anthropic (sk-ant-…) key — then send your message again.")

PROVIDER_NAMES = {"openrouter": "OpenRouter", "openai": "OpenAI", "anthropic": "Anthropic"}
KEY_PAGES = {"openrouter": "https://openrouter.ai/keys", "openai": "https://platform.openai.com/api-keys",
             "anthropic": "https://platform.claude.com/settings/keys"}


def clean_key(raw: str | None) -> str:
    """Accept common paste mistakes: quotes, an `XXX_API_KEY=` prefix, a `Bearer ` prefix, whitespace."""
    key = (raw or "").strip().strip("'\"").strip()
    key = re.sub(r"^(export\s+)?[A-Z_]*API_KEY\s*=\s*", "", key, flags=re.I).strip("'\"").strip()
    key = re.sub(r"^Bearer\s+", "", key, flags=re.I)
    return re.sub(r"\s+", "", key)


def detect_provider(key: str) -> str | None:
    if key.startswith("sk-or-"):
        return "openrouter"
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("sk-"):
        return "openai"
    return None


def resolve_key(api_key: str | None) -> tuple[str, str]:
    """The visitor's key wins; otherwise fall back to a server-side key if one is configured."""
    key = clean_key(api_key)
    if not key:
        key = config.api_key() or config.openai_key() or config.anthropic_key() or ""
    if not key:
        raise LLMError(KEY_HELP)
    provider = detect_provider(key)
    if not provider:
        raise LLMError("That doesn't look like an API key (your browser may have auto-filled a saved password). "
                       "Clear the 🔑 field and paste only the key: OpenRouter keys start with sk-or-, OpenAI keys "
                       "with sk-, Anthropic keys with sk-ant-.")
    return key, provider


def model_for(provider: str, requested: str | None) -> str:
    """Pick a model id valid for the provider. An id meant for another provider falls back to the default."""
    requested = (requested or "").strip()
    if provider == "anthropic":
        return requested if requested.startswith("claude") else config.ANTHROPIC_MODEL
    if provider == "openai":
        if requested.startswith("openai/"):
            return requested.split("/", 1)[1]
        return requested if requested and "/" not in requested and not requested.startswith("claude") \
            else config.OPENAI_MODEL
    return requested if "/" in requested else config.MODEL       # OpenRouter ids look like vendor/model


class LLM:
    def __new__(cls, api_key: str | None = None, model: str | None = None):
        key, provider = resolve_key(api_key)
        if provider == "anthropic":
            from .anthropic_llm import AnthropicLLM
            return AnthropicLLM(key, model_for(provider, model))
        self = super().__new__(cls)
        self._key, self.provider = key, provider
        return self

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.model = model_for(self.provider, model)
        if self.provider == "openrouter":
            self.client = OpenAI(base_url=config.OPENROUTER_BASE_URL, api_key=self._key,
                                 default_headers={"X-Title": "Insurance SOP Agent Demo"})
        else:
            self.client = OpenAI(base_url=config.OPENAI_BASE_URL, api_key=self._key)

    def _call(self, messages: list[dict], max_tokens: int, json_mode: bool, schema: dict | None = None) -> str:
        kwargs = {}
        if schema:
            kwargs["response_format"] = {"type": "json_schema",
                                         "json_schema": {"name": "output", "strict": True, "schema": schema}}
        elif json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        # OpenAI's current models take max_completion_tokens; OpenRouter accepts max_tokens.
        kwargs["max_completion_tokens" if self.provider == "openai" else "max_tokens"] = max_tokens
        name, page = PROVIDER_NAMES[self.provider], KEY_PAGES[self.provider]
        for attempt in range(3):
            try:
                resp = self.client.chat.completions.create(model=self.model, messages=messages, **kwargs)
                return (resp.choices[0].message.content or "").strip()
            except Exception as e:  # surface API problems (bad key, no credit) to the UI
                status = getattr(e, "status_code", None)
                if attempt < 2 and (status is None or status == 429 or status >= 500):
                    time.sleep(2 * (attempt + 1))
                    continue
                if status == 401:
                    raise LLMError(f"{name} rejected this API key (it may be mistyped, revoked or deleted). Check "
                                   f"the key in the 🔑 field or create a new one at {page}.") from e
                if status in (402, 403) or (status == 429 and "quota" in str(e).lower()):
                    raise LLMError(f"This {name} key is out of credit, over its spending limit, or not allowed "
                                   f"to use model {self.model}. Check it at {page}.") from e
                if status == 404:
                    raise LLMError(f"{name} doesn't offer the model '{self.model}' to this key. Change the Model "
                                   "field (or clear it to use the default).") from e
                raise LLMError(f"Model call failed: {e}") from e

    def json(self, system: str, user: str, max_tokens: int = config.MAX_TOKENS, schema: dict | None = None) -> dict:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        raw = ""
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
