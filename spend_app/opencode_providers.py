"""Provider attribution for OpenCode usage, independent from reference pricing.

Only the Z.AI coding-plan provider belongs to the existing Z.AI quota lane.
Every other provider gets a usage-only row; a shared model name does not prove
that two requests drew from the same subscription or allowance.
"""
from urllib.parse import quote, unquote

PROVIDER_PREFIX = "opencode-provider:"
ZAI_PLAN_PROVIDER = "zai-coding-plan"
GO_PROVIDER = "opencode-go"


def tool_key_for_provider(provider: str) -> str:
    if provider == ZAI_PLAN_PROVIDER:
        return "opencode"
    if provider == GO_PROVIDER:
        return GO_PROVIDER
    return PROVIDER_PREFIX + quote(provider or "unknown", safe="")


def provider_tool_name(key: str) -> str | None:
    if not key.startswith(PROVIDER_PREFIX):
        return None
    provider = unquote(key[len(PROVIDER_PREFIX):])
    label = {
        "isoquant": "Isoquant", "openai": "OpenAI", "anthropic": "Anthropic",
        "openrouter": "OpenRouter", "deepseek": "DeepSeek", "unknown": "Unknown provider",
    }.get(provider.lower(), provider)
    return "OpenCode · " + label
