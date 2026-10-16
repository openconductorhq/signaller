"""claude-router: route Claude Code requests to Anthropic or a local LiteLLM."""
import json
import re

ROUTED_PATHS = ("/v1/messages", "/v1/messages/count_tokens")
TAG_RE = re.compile(r"(?<!\w)#(plan|local)(?!\w)", re.I)


def latest_human_text(messages: list) -> str:
    """Text of the most recent user message that has non-system-reminder text."""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        texts = [
            b["text"] for b in content
            if b.get("type") == "text" and not b["text"].strip().startswith("<system-reminder>")
        ]
        if texts:
            return "\n".join(texts)
    return ""


def decide(method: str, path: str, body: bytes) -> tuple[str, str]:
    """Return (route, reason): route is "anthropic" or "local"."""
    if method != "POST" or path.split("?", 1)[0] not in ROUTED_PATHS:
        return "anthropic", "passthrough"
    try:
        req = json.loads(body)
        model = req["model"]
        tags = {t.lower() for t in TAG_RE.findall(latest_human_text(req["messages"]))}
    except (ValueError, KeyError, TypeError, AttributeError):
        return "anthropic", "passthrough"
    if "plan" in tags:
        return "anthropic", "tag:plan"
    if "local" in tags:
        return "local", "tag:local"
    return ("local" if "haiku" in model or "sonnet" in model else "anthropic"), "tier"


def local_model(model: str, sonnet: str, haiku: str) -> str:
    return haiku if "haiku" in model else sonnet
