import hashlib
import re
from collections import Counter

SENTENCE = re.compile(r"[^.!?。…\n]+[.!?。…]*")


def fingerprint(text: str) -> list[list[str | int]]:
    sentences = (match.strip() for match in SENTENCE.findall(text))
    return [
        [hashlib.sha1(sentence.encode("utf-8")).hexdigest()[:16], len(sentence)]
        for sentence in sentences
        if sentence
    ]


def changed(old: list[list[str | int]], new: list[list[str | int]]) -> int:
    before = Counter(str(digest) for digest, _ in old)
    after = Counter(str(digest) for digest, _ in new)
    size = {str(digest): int(length) for digest, length in (*old, *new)}
    difference = (before - after) + (after - before)
    return sum(size[digest] * count for digest, count in difference.items())


def threshold(length: int, chars: int, ratio: float) -> int:
    return min(chars, int(length * ratio))
