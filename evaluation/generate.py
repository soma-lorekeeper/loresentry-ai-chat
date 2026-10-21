"""Write the chapters of each outline with the model and save them as datasets.

    OPENAI_API_KEY=... python -m evaluation.generate [name ...]

Every chapter must contain its facts word for word; a chapter that drops one is
written again. The result is saved in evaluation/datasets/<name>.json and is
meant to be committed, so evaluation runs use the same text.
"""

import asyncio
import json
import math
import os
import sys
from pathlib import Path

from openai import AsyncOpenAI

from evaluation.outlines import DATASETS

HERE = Path(__file__).parent
MODEL = os.getenv("EVAL_WRITER_MODEL", "gpt-6-luna")
ATTEMPTS = 3

INSTRUCTIONS = """You write one chapter of a Korean web novel from an outline.
- Write in Korean, in natural literary prose with dialogue, 2,000 to 3,000 characters.
- Separate paragraphs with a blank line. No title, no headings, no notes.
- Include every sentence listed under "반드시 포함할 문장" exactly as written, character for character, each as its own sentence.
- Only the events of this chapter may happen. Earlier chapters may be remembered only as they are listed in the outline. Never mention or hint at later chapters.
- Do not invent other plot events, revelations, numbers, dates, places or relationships beyond the outline. Atmosphere, feelings and small talk are fine.
- Never write anything listed under "금지"."""


def chapter_prompt(story: dict, number: int) -> str:
    title, facts, beats = story["chapters"][number - 1]
    earlier = "\n".join(
        f"- {n}화 {t}: {' '.join(f)}"
        for n, (t, f, _) in enumerate(story["chapters"][: number - 1], 1)
    )
    required = "\n".join(f"- {fact}" for fact in facts)
    forbidden = "\n".join(f"- {item}" for item in story["forbidden"])
    return (
        f"장르: {story['genre']}\n작품: {story['project']}\n등장인물: {story['cast']}\n\n"
        f"지난 회차:\n{earlier or '- 없음'}\n\n"
        f"이번 회차: {number}화 · {title}\n장면: {beats}\n\n"
        f"반드시 포함할 문장:\n{required}\n\n금지:\n{forbidden}"
    )


async def write(client: AsyncOpenAI, story: dict, number: int, gate) -> str:
    facts = story["chapters"][number - 1][1]
    async with gate:
        for attempt in range(1, ATTEMPTS + 1):
            response = await client.responses.create(
                model=MODEL,
                instructions=INSTRUCTIONS,
                input=chapter_prompt(story, number),
                reasoning={"effort": "low"},
                max_output_tokens=8000,
                store=False,
            )
            text = (response.output_text or "").strip()
            missing = [fact for fact in facts if fact not in text]
            if not missing:
                print(f"{story['name']} {number}화 ok ({len(text)}자)", flush=True)
                return text
            print(f"{story['name']} {number}화 retry {attempt}: {missing}", flush=True)
    raise RuntimeError(f"{story['name']} {number}화 lost a fact")


async def build(client: AsyncOpenAI, story: dict) -> dict:
    gate = asyncio.Semaphore(6)
    count = len(story["chapters"])
    bodies = await asyncio.gather(
        *(write(client, story, n, gate) for n in range(1, count + 1))
    )
    per_episode = math.ceil(count / len(story["episodes"]))
    return {
        "name": story["name"],
        "project": story["project"],
        "description": story["description"],
        "episodes": story["episodes"],
        "chapters": [
            {
                "number": n,
                "episode": (n - 1) // per_episode,
                "title": f"{n}화 · {title}",
                "facts": facts,
                "body": body,
            }
            for n, ((title, facts, _), body) in enumerate(
                zip(story["chapters"], bodies, strict=True), 1
            )
        ],
        "settings": [
            {
                "folder": folder,
                "key": key,
                "title": title,
                "body": body,
                "properties": [{"key": k, "value": v} for k, v in properties],
            }
            for folder, key, title, body, properties in story["settings"]
        ],
        "relations": [
            {"from": a, "to": b, "description": d} for a, b, d in story["relations"]
        ],
        "memos": [
            {"scope": scope, "about": about, "title": title, "body": body}
            for scope, about, title, body in story["memos"]
        ],
        "questions": story["questions"],
    }


async def main(names: list[str]) -> None:
    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=2)
    (HERE / "datasets").mkdir(exist_ok=True)
    for story in DATASETS:
        if names and story["name"] not in names:
            continue
        dataset = await build(client, story)
        path = HERE / "datasets" / f"{story['name']}.json"
        path.write_text(json.dumps(dataset, ensure_ascii=False, indent=1) + "\n")
        size = sum(len(c["body"]) for c in dataset["chapters"])
        print(f"saved {path.name}: {len(dataset['chapters'])} chapters, {size} chars")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
