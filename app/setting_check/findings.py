import hashlib
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any

from app.content import Document
from app.model import ModelFailure
from app.navigation import short_ids
from app.text import collapse

QUOTE_MARKS = "\"'`“”‘’«»「」『』 "
MIN_QUOTE = 4
VERDICTS = {"CONFLICT", "CHANGED_OVER_TIME", "CHARACTER_PERSPECTIVE", "INSUFFICIENT"}
CONFIDENCES = {"HIGH", "LOW"}


def edge(char: str) -> bool:
    return char.isspace() or unicodedata.category(char).startswith("P")


def clean(text: str) -> str:
    return collapse(text).strip(QUOTE_MARKS)


def norm(text: str) -> str:
    flat = collapse(text)
    start, end = 0, len(flat)
    while start < end and edge(flat[start]):
        start += 1
    while end > start and edge(flat[end - 1]):
        end -= 1
    return flat[start:end]


def fingerprint(kind: str, source_id: str, source_quote: str, quote: str) -> str:
    raw = "|".join((kind, source_id, norm(source_quote), norm(quote)))
    return hashlib.sha256(raw.encode()).hexdigest()


def locate(quote: str, haystacks: tuple[str, ...]) -> str | None:
    for needle in dict.fromkeys((clean(quote), norm(quote))):
        if len(needle) >= MIN_QUOTE and any(needle in text for text in haystacks):
            return needle
    return None


def overlaps(a: str, b: str) -> bool:
    left, right = norm(a), norm(b)
    return bool(left and right) and (left in right or right in left)


@dataclass(frozen=True)
class Candidate:
    kind: str
    source_id: str
    source_quote: str
    quote: str
    summary: str
    direction: str
    core: bool = False
    confidence: str = "HIGH"


def text_of(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    return value if isinstance(value, str) else ""


def parse_candidates(
    value: dict[str, Any], by_short: dict[str, str], core: bool
) -> list[Candidate]:
    items = value.get("candidates")
    if not isinstance(items, list):
        raise ModelFailure("MODEL_INVALID_JSON")
    found: list[Candidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        source = by_short.get(text_of(item, "source_id").strip())
        if source is None:
            continue
        found.append(
            Candidate(
                kind="SETTING" if core else text_of(item, "kind"),
                source_id=source,
                source_quote=text_of(item, "source_quote"),
                quote=text_of(item, "quote"),
                summary=collapse(text_of(item, "summary")),
                direction=collapse(text_of(item, "direction")),
                core=core,
            )
        )
    return found


def judged(candidate: Candidate, value: dict[str, Any]) -> Candidate | None:
    verdict = value.get("verdict")
    if verdict not in VERDICTS:
        raise ModelFailure("MODEL_INVALID_JSON")
    if verdict != "CONFLICT":
        return None
    confidence = value.get("confidence")
    return replace(
        candidate,
        confidence=confidence if confidence in CONFIDENCES else "LOW",
        summary=collapse(text_of(value, "summary")) or candidate.summary,
        direction=collapse(text_of(value, "direction")) or candidate.direction,
    )


def same(a: Candidate, b: Candidate) -> bool:
    return a.source_id == b.source_id and (
        overlaps(a.quote, b.quote) or overlaps(a.source_quote, b.source_quote)
    )


def dedupe(candidates: list[Candidate]) -> list[Candidate]:
    kept: list[Candidate] = []
    for candidate in candidates:
        if not any(same(candidate, other) for other in kept):
            kept.append(candidate)
    return kept


@dataclass
class Reading:
    target: Document
    earlier: tuple[Document, ...]
    settings: tuple[Document, ...]
    ids: dict[str, str] = field(init=False)
    by_short: dict[str, str] = field(init=False)
    documents: dict[str, Document] = field(init=False)
    body: str = field(init=False)
    texts: dict[str, tuple[str, ...]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        sources = (*self.earlier, *self.settings)
        self.ids = short_ids([self.target.id, *(doc.id for doc in sources)])
        self.by_short = {alias: target for target, alias in self.ids.items()}
        self.documents = {doc.id: doc for doc in sources}
        self.body = collapse(self.target.body_text)

    @property
    def core_facts(self) -> int:
        return sum(len(doc.core_facts) for doc in self.settings)

    def source_texts(self, doc: Document) -> tuple[str, ...]:
        texts = self.texts.get(doc.id)
        if texts is None:
            raw = (doc.body_text, *doc.core_facts, *(p.value for p in doc.properties))
            texts = tuple(collapse(text) for text in raw if text)
            self.texts[doc.id] = texts
        return texts

    def verify(self, candidate: Candidate) -> Candidate | None:
        doc = self.documents.get(candidate.source_id)
        if doc is None:
            return None
        quote = locate(candidate.quote, (self.body,))
        source_quote = locate(candidate.source_quote, self.source_texts(doc))
        if quote is None or source_quote is None:
            return None
        kind = "MANUSCRIPT" if doc.is_manuscript else "SETTING"
        core = kind == "SETTING" and any(
            overlaps(source_quote, fact) for fact in doc.core_facts
        )
        return replace(
            candidate, kind=kind, quote=quote, source_quote=source_quote, core=core
        )

    def order(self, candidates: list[Candidate]) -> list[Candidate]:
        return sorted(candidates, key=lambda candidate: self.body.find(candidate.quote))
