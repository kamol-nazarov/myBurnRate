"""Provider attribution for OpenCode usage, independent from reference pricing.

Only the Z.AI coding-plan provider belongs to the existing Z.AI quota lane.
Every other provider gets a usage-only row; a shared model name does not prove
that two requests drew from the same subscription or allowance.
"""
import colorsys
import hashlib
from urllib.parse import quote, unquote

PROVIDER_PREFIX = "opencode-provider:"
ZAI_PLAN_PROVIDER = "zai-coding-plan"
GO_PROVIDER = "opencode-go"
# Legacy cumulative rows prove the provider, but not which Console profile.
UNSCOPED_GO_TOOL = PROVIDER_PREFIX + GO_PROVIDER


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
        GO_PROVIDER: "Go (profile unknown)",
    }.get(provider.lower(), provider)
    return "OpenCode · " + label


def provider_tool_color(key: str) -> str | None:
    """Stable chart swatches for provider rows, shared by web and native UI."""
    if not key.startswith(PROVIDER_PREFIX):
        return None
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    # Leave the Z.AI lane's green band out of this palette.
    hue = (190 + int.from_bytes(digest[:2], "big") % 300) % 360
    rgb = colorsys.hls_to_rgb(hue / 360, 0.48, 0.65)
    return "#" + "".join(f"{round(channel * 255):02x}" for channel in rgb)
