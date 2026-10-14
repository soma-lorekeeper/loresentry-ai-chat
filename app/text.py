import re
from typing import Any

TITLE_LIMIT = 24
PREVIEW_LIMIT = 80
SNIPPET_BEFORE = 16
SNIPPET_AFTER = 40

MARKER = re.compile(r"\[\[(\d+)\]\]")
SPACES = re.compile(r"\s+")
BULLET = re.compile(r"^- ", re.MULTILINE)

DEFAULT_TITLES = {"ko": "새 대화", "en": "New conversation"}


def default_title(locale: str | None) -> str:
    return DEFAULT_TITLES.get(locale or "ko", DEFAULT_TITLES["ko"])


def collapse(text: str) -> str:
    return SPACES.sub(" ", text).strip()


def chat_title(text: str) -> str:
    flat = collapse(text)
    return f"{flat[: TITLE_LIMIT - 1]}…" if len(flat) > TITLE_LIMIT else flat


def chat_preview(content: str, citations: list[dict[str, Any]]) -> str:
    def replace(match: re.Match[str]) -> str:
        following = content[match.end() : match.end() + 1]
        if not following or following.isspace():
            return ""
        index = int(match.group(1)) - 1
        return citations[index]["title"] if 0 <= index < len(citations) else ""

    flat = MARKER.sub(replace, content).replace("**", "")
    flat = collapse(BULLET.sub("", flat))
    return f"{flat[:PREVIEW_LIMIT]}…" if len(flat) > PREVIEW_LIMIT else flat


def search_text(
    content: str, quote: dict[str, Any] | None, citations: list[dict[str, Any]]
) -> str:
    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1)) - 1
        return f" {citations[index]['title']} " if 0 <= index < len(citations) else ""

    body = MARKER.sub(replace, content)
    quoted = quote["text"] if quote else ""
    return collapse(f"{quoted} {body}".replace("**", ""))


def snippet(text: str, needle: str) -> dict[str, str] | None:
    at = text.lower().find(needle.lower())
    if at < 0:
        return None
    start = max(0, at - SNIPPET_BEFORE)
    end = min(len(text), at + len(needle) + SNIPPET_AFTER)
    return {
        "before": ("…" if start > 0 else "") + text[start:at],
        "match": text[at : at + len(needle)],
        "after": text[at + len(needle) : end] + ("…" if end < len(text) else ""),
    }
