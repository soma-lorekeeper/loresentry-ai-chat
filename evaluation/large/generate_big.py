"""Generate a large Korean web novel project (about 100 chapters) for agent-loop testing.

    OPENAI_API_KEY=... python generate_big.py OUT_DIR
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from openai import AsyncOpenAI

MODEL = "gpt-6-luna"
OUT = Path(sys.argv[1])
PARTS = 5
PER_PART = 20
FOLDERS = ["CHARACTER", "LOCATION", "ORGANIZATION", "ITEM", "EVENT", "WORLDVIEW"]


def strict(schema):
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
        schema["required"] = list(schema["properties"])
        for value in schema["properties"].values():
            strict(value)
    if schema.get("type") == "array":
        strict(schema["items"])
    return schema


STR = {"type": "string"}
PROPS = {"type": "array", "items": {"type": "object", "properties": {"key": STR, "value": STR}}}

BIBLE_SCHEMA = strict({
    "type": "object",
    "properties": {
        "project": STR,
        "description": STR,
        "genre": STR,
        "cast": STR,
        "parts": {"type": "array", "items": {"type": "object", "properties": {"name": STR, "synopsis": STR}}},
        "settings": {"type": "array", "items": {"type": "object", "properties": {
            "key": STR, "folder": {"type": "string", "enum": FOLDERS}, "title": STR,
            "properties": PROPS, "truth": STR}}},
        "relations": {"type": "array", "items": {"type": "object", "properties": {"from": STR, "to": STR, "description": STR}}},
        "memos": {"type": "array", "items": {"type": "object", "properties": {
            "scope": {"type": "string", "enum": ["project", "file"]}, "about": STR, "title": STR, "body": STR}}},
        "traps": {"type": "array", "items": {"type": "object", "properties": {
            "setting_key": STR, "setting_says": STR, "manuscript_shows": STR, "chapter": {"type": "integer"}}}},
    },
})

PLAN_SCHEMA = strict({
    "type": "object",
    "properties": {"chapters": {"type": "array", "items": {"type": "object", "properties": {
        "number": {"type": "integer"}, "title": STR, "beats": STR, "facts": {"type": "array", "items": STR}, "keys": {"type": "array", "items": STR}}}}},
})

QUESTION_SCHEMA = strict({
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {"type": "object", "properties": {
        "type": STR, "question": STR, "expected": STR, "chapters": {"type": "array", "items": {"type": "integer"}}}}}},
})

BIBLE_PROMPT = f"""한국 웹소설 장편 한 편의 작품 설정집을 만든다. 이 작품은 집필 도구의 AI 챗을 시험하는 데 쓴다.
- 장르: 판타지 미스터리. 섬과 항로, 등불, 기억을 다루는 세계. 진지하고 일관된 이야기.
- 원고는 {PARTS}부 × {PER_PART}화 = {PARTS * PER_PART}화. parts는 {PARTS}개이고 각 synopsis는 그 부의 사건을 시간 순서대로 8~12문장으로 구체적으로 적는다(누가, 어디서, 무엇을, 왜).
- settings는 정확히 40개: CHARACTER 15, LOCATION 9, ORGANIZATION 5, ITEM 6, EVENT 3, WORLDVIEW 2. key는 영문 소문자 slug. title은 한국어 이름. properties에는 description(한 줄 소개)을 꼭 넣고, 별명이 있는 인물은 alias를 넣는다(별명 있는 인물 3명 이상). truth는 작가만 아는 진실과 이야기 속 역할을 2~4문장으로.
- 1부에 잠깐 나왔다가 4~5부에서 결정적으로 돌아오는 인물, 여러 부에 흩어진 단서로 풀리는 수수께끼를 반드시 넣는다.
- relations는 30개 이상. from/to는 settings의 key. description은 짧은 관계 설명.
- memos는 6개. 작가의 구상 메모로, 일부는 실제 원고와 다르게 흘러간 계획(예: 범인 후보를 바꿀지 고민)이다. scope가 file이면 about에 settings key, project면 about은 빈 문자열.
- traps는 4개. 설정 문서에 적힌 내용(setting_says)과 원고가 보여 주는 사실(manuscript_shows)이 어긋나는 지점. chapter는 원고에서 그 사실이 드러나는 회차 번호(1~{PARTS * PER_PART}).
- cast는 주요 인물 10명의 한 줄 소개를 이어 쓴 문단.
- 등장인물과 지명은 서로 헷갈리지 않게 뚜렷하게 짓는다."""


def plan_prompt(bible, part):
    first = part * PER_PART + 1
    last = first + PER_PART - 1
    settings = "\n".join(f"- {s['key']} ({s['folder']}) {s['title']}: {s['truth']}" for s in bible["settings"])
    parts = "\n".join(f"{i + 1}부 {p['name']}: {p['synopsis']}" for i, p in enumerate(bible["parts"]))
    traps = "\n".join(
        f"- {t['chapter']}화: 원고에서 '{t['manuscript_shows']}' (설정 문서는 '{t['setting_says']}'라고 적혀 있음)"
        for t in bible["traps"] if first <= t["chapter"] <= last
    )
    return f"""작품: {bible['project']} ({bible['genre']})
전체 줄거리:
{parts}

설정(작가만 아는 진실 포함):
{settings}

이번에 계획할 범위: {part + 1}부 {bible['parts'][part]['name']}, {first}화부터 {last}화까지 정확히 {PER_PART}개.
chapters 배열에는 다음 번호가 하나도 빠짐없이 순서대로 하나씩 있어야 한다: {', '.join(str(n) for n in range(first, last + 1))}. 각 항목의 number에 그 번호를 적는다.
이 범위에서 원고가 반드시 보여 줄 함정:
{traps or '- 없음'}

각 회차:
- title: 회차 제목(번호 없이 짧게).
- beats: 그 회차에서 일어나는 일 3~4문장. 이전 회차와 이어지고, 이 부의 줄거리를 {PER_PART}화에 고르게 나눈다.
- facts: 원고에 그대로 넣을 사실 문장 정확히 2개. 이름·장소·숫자·시각 등 확인할 수 있는 구체적인 내용. 각각 '다.'로 끝나는 한 문장. 다른 회차의 사실과 겹치지 않는다.
- keys: 그 회차에 등장하거나 언급되는 settings key 목록."""


def setting_prompt(bible, setting, mentions):
    traps = [t for t in bible["traps"] if t["setting_key"] == setting["key"]]
    trap_text = "\n".join(f"- 반드시 이렇게 적는다: {t['setting_says']}" for t in traps)
    return f"""작품: {bible['project']} ({bible['genre']})
작가가 집필 도구에 정리해 둔 설정 문서 하나를 쓴다. 문서: {setting['title']} ({setting['folder']})
작가만 아는 진실: {setting['truth']}
원고에서 이 대상이 나오는 장면(회차: 사실):
{mentions or '- 없음'}
{trap_text}

- 한국어로 600~1,200자. 제목·머리말 없이 본문만. 소제목 대신 짧은 문단으로 외형/성격/배경/역할/메모 같은 내용을 담는다.
- 원고에 나온 사실과 어긋나지 않게 쓴다. 단 위의 '반드시 이렇게 적는다'는 원고와 다르더라도 그대로 적는다(작가가 처음 세운 계획이다).
- 아직 원고에 나오지 않은 반전은 쓰지 않는다."""


CHAPTER_INSTRUCTIONS = """You write one chapter of a Korean web novel from an outline.
- Write in Korean, in natural literary prose with dialogue, 3,000 to 4,000 characters.
- Separate paragraphs with a blank line. No title, no headings, no notes.
- Include every sentence listed under "반드시 포함할 문장" exactly as written, character for character, each as its own sentence.
- Only the events of this chapter may happen. Earlier chapters may be remembered only as they are listed in the outline. Never mention or hint at later chapters.
- Do not invent other plot events, revelations, numbers, dates, places or relationships beyond the outline. Atmosphere, feelings and small talk are fine."""


def chapter_prompt(bible, plans, number):
    plan = plans[number - 1]
    part = (number - 1) // PER_PART
    earlier_parts = "\n".join(f"- {i + 1}부: {bible['parts'][i]['synopsis']}" for i in range(part))
    start = part * PER_PART
    earlier = "\n".join(
        f"- {n + 1}화 {plans[n]['title']}: {' '.join(plans[n]['facts'])}" for n in range(start, number - 1)
    )
    recent = "\n".join(f"- {n + 1}화: {plans[n]['beats']}" for n in range(max(0, number - 3), number - 1))
    names = {s["key"]: f"{s['title']}({s['properties'][0]['value'] if s['properties'] else ''})" for s in bible["settings"]}
    present = ", ".join(names.get(k, k) for k in plan["keys"])
    required = "\n".join(f"- {fact}" for fact in plan["facts"])
    return (
        f"장르: {bible['genre']}\n작품: {bible['project']}\n주요 인물: {bible['cast']}\n\n"
        f"앞선 부:\n{earlier_parts or '- 없음'}\n\n이번 부의 앞선 회차:\n{earlier or '- 없음'}\n\n"
        f"직전 회차 장면:\n{recent or '- 없음'}\n\n"
        f"이번 회차: {number}화 · {plan['title']}\n등장: {present}\n장면: {plan['beats']}\n\n"
        f"반드시 포함할 문장:\n{required}"
    )


async def structured(client, prompt, schema, name, effort="medium", max_tokens=32000):
    for attempt in range(3):
        try:
            response = await client.responses.create(
                model=MODEL, input=prompt, reasoning={"effort": effort}, max_output_tokens=max_tokens,
                store=False, text={"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}},
            )
            return json.loads(response.output_text)
        except Exception as error:
            print(f"{name} retry {attempt + 1}: {error}", flush=True)
    raise RuntimeError(name)


async def text(client, instructions, prompt, effort="low", max_tokens=12000):
    response = await client.responses.create(
        model=MODEL, instructions=instructions, input=prompt, reasoning={"effort": effort},
        max_output_tokens=max_tokens, store=False,
    )
    return (response.output_text or "").strip()


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=1))


def load(name):
    path = OUT / name
    return json.loads(path.read_text()) if path.exists() else None


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=3, timeout=600)
    gate = asyncio.Semaphore(10)

    bible = load("bible.json")
    if bible is None:
        bible = await structured(client, BIBLE_PROMPT, BIBLE_SCHEMA, "bible", "high")
        save("bible.json", bible)
    print(f"bible: {bible['project']} settings={len(bible['settings'])} relations={len(bible['relations'])}", flush=True)

    plans = load("plans.json")
    if plans is None:
        async def plan(part):
            cached = load(f"plan{part}.json")
            if cached:
                return cached
            async with gate:
                for attempt in range(4):
                    result = await structured(client, plan_prompt(bible, part), PLAN_SCHEMA, f"plan{part}")
                    chapters = result["chapters"][:PER_PART]
                    if len(chapters) == PER_PART and all(len(c["facts"]) >= 2 for c in chapters):
                        break
                    print(f"plan part {part + 1} retry {attempt + 1}: {len(chapters)} chapters", flush=True)
                else:
                    raise RuntimeError(f"part {part} has {len(chapters)} chapters")
                for c in chapters:
                    c["facts"] = c["facts"][:2]
                save(f"plan{part}.json", chapters)
                print(f"plan part {part + 1} ok", flush=True)
                return chapters
        plans = [c for part in await asyncio.gather(*(plan(p) for p in range(PARTS))) for c in part]
        save("plans.json", plans)

    bodies = load("settings.json") or {}
    async def setting(s):
        if s["key"] in bodies:
            return
        mentions = "\n".join(
            f"- {n + 1}화: {' '.join(p['facts'])}" for n, p in enumerate(plans) if s["key"] in p["keys"]
        )[:6000]
        async with gate:
            bodies[s["key"]] = await text(client, "Write in Korean.", setting_prompt(bible, s, mentions))
        print(f"setting {s['title']} {len(bodies[s['key']])}자", flush=True)
    await asyncio.gather(*(setting(s) for s in bible["settings"]))
    save("settings.json", bodies)

    chapters = load("chapters.json") or {}
    async def chapter(number):
        if str(number) in chapters:
            return
        facts = plans[number - 1]["facts"]
        async with gate:
            body = ""
            for attempt in range(3):
                body = await text(client, CHAPTER_INSTRUCTIONS, chapter_prompt(bible, plans, number))
                missing = [f for f in facts if f not in body]
                if not missing and len(body) >= 2500:
                    break
                print(f"{number}화 retry {attempt + 1}: missing={len(missing)} len={len(body)}", flush=True)
        chapters[str(number)] = body
        if number % 10 == 0:
            save("chapters.json", chapters)
        print(f"{number}화 {len(body)}자", flush=True)
    await asyncio.gather(*(chapter(n) for n in range(1, PARTS * PER_PART + 1)))
    save("chapters.json", chapters)

    questions = load("questions.json")
    if questions is None:
        outline = "\n".join(f"{n + 1}화 {p['title']}: {p['beats']} 사실: {' '.join(p['facts'])}" for n, p in enumerate(plans))
        traps = "\n".join(f"- 설정 '{t['setting_says']}' vs {t['chapter']}화 '{t['manuscript_shows']}'" for t in bible["traps"])
        memos = "\n".join(f"- {m['title']}: {m['body']}" for m in bible["memos"])
        prompt = f"""아래 장편의 회차별 줄거리로 AI 챗 시험 질문 16개를 만든다.
- 유형과 개수: simple 3, multihop 4(서로 멀리 떨어진 회차 2~3곳을 이어야 답이 나오는 것), complex 3(여러 회차에 걸친 시간순 정리·원인과 결과), classification 2(인물·단서 등을 기준에 따라 분류), contradiction 2(아래 함정), memo 1(메모를 사실로 착각하지 않는지), negative 1(원고에 없는 일).
- expected는 정답의 핵심을 2~4문장으로, 근거 회차 번호를 포함한다. chapters는 근거 회차 번호 목록.
함정:
{traps}
작가 메모:
{memos}
회차별 줄거리:
{outline}"""
        questions = await structured(client, prompt, QUESTION_SCHEMA, "questions", "medium")
        save("questions.json", questions)

    dataset = {
        "project": bible["project"],
        "description": bible["description"],
        "episodes": [f"{i + 1}부 · {p['name']}" for i, p in enumerate(bible["parts"])],
        "chapters": [
            {"number": n, "episode": (n - 1) // PER_PART, "title": f"{n}화 · {plans[n - 1]['title']}", "body": chapters[str(n)]}
            for n in range(1, PARTS * PER_PART + 1)
        ],
        "settings": [
            {"folder": s["folder"], "key": s["key"], "title": s["title"], "body": bodies[s["key"]], "properties": s["properties"]}
            for s in bible["settings"]
        ],
        "relations": [r for r in bible["relations"] if r["from"] in bodies and r["to"] in bodies and r["from"] != r["to"]],
        "memos": [
            {"scope": m["scope"], "about": m["about"] if m["scope"] == "file" and m["about"] in bodies else None,
             "title": m["title"], "body": m["body"]}
            for m in bible["memos"]
        ],
    }
    for m in dataset["memos"]:
        if m["about"] is None:
            m["scope"] = "project"
    save("dataset.json", dataset)
    manuscript = sum(len(c["body"]) for c in dataset["chapters"])
    settings_size = sum(len(s["body"]) for s in dataset["settings"])
    print(f"done: chapters={len(dataset['chapters'])} manuscript={manuscript}자 settings={len(dataset['settings'])} {settings_size}자 relations={len(dataset['relations'])} memos={len(dataset['memos'])}", flush=True)
    await client.close()


asyncio.run(main())
