import re
from dataclasses import dataclass
from html import unescape
from typing import Any

from app.prompt import Material
from app.text import collapse

OPEN = "<cite"
ATTRIBUTE = re.compile(r'([a-z_]+)\s*=\s*"([^"]*)"')
TAG = re.compile(r'<cite\b(?:\s+[a-z_]+\s*=\s*"[^"]*")*\s*/?>')
WRAPPER = re.compile(r"</?(?:final|answer|response)\s*>")
WRAPPER_NAMES = ("<final", "</final", "<answer", "</answer", "<response", "</response")
WRAPPER_HOLD = 16
RUNAWAY = 800


@dataclass(frozen=True)
class Delta:
    text: str


@dataclass(frozen=True)
class Cited:
    index: int
    citation: dict[str, Any]


def tag_end(text: str) -> int | None:
    match = TAG.match(text)
    if match:
        return match.end()
    inside = False
    for index, char in enumerate(text):
        if char == '"':
            inside = not inside
        elif char == ">" and not inside:
            return index + 1
    return None


def is_unclosed_wrapper(text: str) -> bool:
    return ">" not in text and any(
        text.rstrip().startswith(name) for name in WRAPPER_NAMES
    )


def could_become_tag(text: str) -> bool:
    if any(name.startswith(text) for name in (OPEN, *WRAPPER_NAMES)):
        return True
    return (
        any(text.startswith(name) for name in WRAPPER_NAMES)
        and ">" not in text
        and len(text) < WRAPPER_HOLD
    )


class CitationStream:
    def __init__(self, materials: dict[str, Material]):
        self.materials = materials
        self.pending = ""
        self.parts: list[str] = []
        self.citations: list[dict[str, Any]] = []
        self.indexes: dict[tuple[str, str | None], int] = {}
        self.normalized: dict[str, tuple[str, ...]] = {}

    @property
    def content(self) -> str:
        return "".join(self.parts).strip()

    def feed(self, text: str) -> list[Delta | Cited]:
        self.pending += text
        return self.drain(final=False)

    def finish(self) -> list[Delta | Cited]:
        return self.drain(final=True)

    def emit(self, text: str, out: list[Delta | Cited]) -> None:
        if text:
            self.parts.append(text)
            out.append(Delta(text))

    def drain(self, final: bool) -> list[Delta | Cited]:
        out: list[Delta | Cited] = []
        while self.pending:
            start = self.pending.find("<")
            if start < 0:
                self.emit(self.pending, out)
                self.pending = ""
                break
            self.emit(self.pending[:start], out)
            self.pending = self.pending[start:]
            wrapper = WRAPPER.match(self.pending)
            if wrapper:
                self.pending = self.pending[wrapper.end() :]
                continue
            if self.pending.startswith(OPEN):
                end = tag_end(self.pending)
                if end is None:
                    if final:
                        self.pending = ""
                    elif len(self.pending) > RUNAWAY:
                        self.emit(self.pending, out)
                        self.pending = ""
                    break
                tag, self.pending = self.pending[:end], self.pending[end:]
                self.cite(tag, out)
                continue
            if not final and could_become_tag(self.pending):
                break
            if final and is_unclosed_wrapper(self.pending):
                self.pending = ""
                break
            self.emit("<", out)
            self.pending = self.pending[1:]
        return out

    def cite(self, tag: str, out: list[Delta | Cited]) -> None:
        attributes = {name: unescape(value) for name, value in ATTRIBUTE.findall(tag)}
        material = self.materials.get(attributes.get("id", "").strip())
        if not material:
            return
        quote = self.verified(material, attributes.get("quote"))
        key = (material.alias, quote)
        index = self.indexes.get(key)
        if index is None:
            self.citations.append(material.citation(quote))
            index = len(self.citations)
            self.indexes[key] = index
            out.append(Cited(index, self.citations[-1]))
        self.emit(f"[[{index}]]", out)

    def verified(self, material: Material, quote: str | None) -> str | None:
        if not quote:
            return None
        needle = collapse(quote)
        if not needle:
            return None
        haystacks = self.normalized.get(material.alias)
        if haystacks is None:
            haystacks = tuple(collapse(text) for text in material.texts if text)
            self.normalized[material.alias] = haystacks
        return needle if any(needle in text for text in haystacks) else None
