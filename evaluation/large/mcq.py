import asyncio
import json
import os
import sys
from pathlib import Path

from openai import AsyncOpenAI

OUT = Path(sys.argv[1])
dataset = json.loads((OUT / "dataset.json").read_text())
UNSURE = "확신할 수 없음 (원고와 설정만으로는 정할 수 없다)"

texts = {}
parts = []
for c in dataset["chapters"]:
    texts[f"{c['number']}화"] = c["body"]
    parts.append(f'<document source="{c["number"]}화" title="{c["title"]}">\n{c["body"]}\n</document>')
for s in dataset["settings"]:
    texts[f"설정: {s['title']}"] = s["body"]
    props = "; ".join(f"{p['key']}={p['value']}" for p in s["properties"])
    parts.append(f'<document source="설정: {s["title"]}" kind="{s["folder"]}" properties="{props}">\n{s["body"]}\n</document>')
for m in dataset["memos"]:
    texts[f"메모: {m['title']}"] = m["body"]
    parts.append(f'<document source="메모: {m["title"]}" kind="author memo">\n{m["body"]}\n</document>')
MATERIAL = "\n\n".join(parts)

S = {"type": "string"}


def obj(props):
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


GEN_SCHEMA = obj({"questions": {"type": "array", "items": obj({
    "type": {"type": "string", "enum": ["single", "multihop", "contradiction", "memo", "unknowable", "order"]},
    "question": S,
    "a": S, "b": S, "c": S, "d": S,
    "answer": {"type": "string", "enum": ["a", "b", "c", "d", "e"]},
    "chapters": {"type": "array", "items": {"type": "integer"}},
    "evidence": {"type": "array", "items": obj({"source": S, "quote": S})},
    "why": S,
})}})

SOLVE_SCHEMA = obj({"answers": {"type": "array", "items": obj({
    "id": {"type": "integer"}, "choice": {"type": "string", "enum": ["a", "b", "c", "d", "e"]}, "reason": S,
})}})

GEN_PROMPT = f"""위 문서들은 한 장편 웹소설 프로젝트의 원고 100화, 설정 문서, 작가 메모 전부다.
이 작품을 두고 AI 챗을 채점할 객관식 문제 24개를 만든다. 정답은 원고와 설정 문서만으로 한 가지로 정해져야 한다.

형식
- 선택지는 a, b, c, d 네 개를 직접 쓴다. 다섯 번째 선택지 e는 언제나 "{UNSURE}"이고 쓰지 않는다.
- answer는 정답 문자. e가 정답인 문제도 있다.
- 오답 선택지는 이 작품에 실제로 나오는 이름·장소·숫자·사건으로 그럴듯하게 만든다. 정답과 헷갈릴 만큼 가깝되, 원고에 비추면 분명히 틀려야 한다.
- 선택지끼리 길이와 문체를 비슷하게 맞춘다. 정답만 길거나 구체적이지 않게 한다.

유형과 개수
- single 4: 한 회차의 구체적인 사실(이름, 숫자, 시각, 장소, 물건).
- multihop 7: 서로 15화 이상 떨어진 두세 회차를 이어야 정답이 나온다. 한 회차만 읽으면 오답 선택지가 그럴듯해야 한다.
- order 2: 여러 회차에 걸친 사건의 순서나 개수.
- contradiction 4: 설정 문서와 원고가 어긋나는 지점. 질문은 원고(이야기) 안에서의 사실을 묻고, 설정 문서의 주장은 오답 선택지로 넣는다.
- memo 3: 작가 메모의 계획과 실제 원고가 다른 지점. 메모의 계획은 오답 선택지로 넣는다.
- unknowable 4: 정답이 e. 원고가 일부러 밝히지 않거나 아직 확인되지 않았다고 적은 것을 묻는다(누가 했는지, 왜 그랬는지, 정확한 원인 등). 원고 어디에도 답이 없다는 것을 전체를 보고 확인한 것만 낸다. 그럴듯한 오답 네 개를 쓴다.

근거
- chapters: 근거가 되는 회차 번호.
- evidence: 정답을 뒷받침하는 문장을 원문 그대로 2~4개, source는 "12화" 또는 "설정: 이름" 또는 "메모: 제목" 형식. unknowable은 원고가 그것을 밝히지 않거나 모른다고 적은 문장을 넣는다.
- why: 정답인 이유와 주요 오답이 틀린 이유를 2~3문장으로.

문제와 선택지는 한국어로 쓴다."""

SOLVE_PROMPT = """위 문서들은 한 장편 웹소설 프로젝트의 원고 100화, 설정 문서, 작가 메모 전부다.
아래 객관식 문제를 원고와 설정 문서만 근거로 푼다. 작가 메모는 계획일 뿐 이야기 속 사실이 아니다. 설정 문서와 원고가 다르면 원고가 이야기 속 사실이다.
문서로 정답을 한 가지로 정할 수 없으면 e를 고른다.
각 문제의 id와 고른 문자, 근거 회차를 포함한 짧은 이유를 낸다.

"""


async def call(client, prompt, schema, name, effort):
    for attempt in range(3):
        try:
            response = await client.responses.create(
                model="gpt-6-luna",
                input=MATERIAL + "\n\n" + prompt,
                reasoning={"effort": effort},
                max_output_tokens=60000,
                store=False,
                prompt_cache_key="mcq-material",
                text={"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}},
            )
            return json.loads(response.output_text)
        except Exception as error:
            print(f"{name} retry {attempt + 1}: {error}", flush=True)
    raise RuntimeError(name)


def render(questions):
    lines = []
    for i, q in enumerate(questions, 1):
        lines.append(f"[{i}] {q['question']}\na. {q['a']}\nb. {q['b']}\nc. {q['c']}\nd. {q['d']}\ne. {UNSURE}")
    return "\n\n".join(lines)


async def main():
    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=3, timeout=1200)
    generated = json.loads((OUT / "mcq_generated.json").read_text()) if (OUT / "mcq_generated.json").exists() else None
    if generated is None:
        generated = (await call(client, GEN_PROMPT, GEN_SCHEMA, "mcq", "high"))["questions"]
        (OUT / "mcq_generated.json").write_text(json.dumps(generated, ensure_ascii=False, indent=1))
    print(f"generated {len(generated)}", flush=True)
    for q in generated:
        q["verbatim"] = all(e["quote"] in texts.get(e["source"], "") for e in q["evidence"])
    solved = await asyncio.gather(*(
        call(client, SOLVE_PROMPT + render(generated), SOLVE_SCHEMA, f"solve{n}", "high") for n in range(2)
    ))
    runs = [{a["id"]: a for a in s["answers"]} for s in solved]
    for i, q in enumerate(generated, 1):
        q["solver"] = [run.get(i, {}).get("choice") for run in runs]
        q["solver_reasons"] = [run.get(i, {}).get("reason") for run in runs]
        q["kept"] = q["verbatim"] and all(choice == q["answer"] for choice in q["solver"])
    (OUT / "mcq_checked.json").write_text(json.dumps(generated, ensure_ascii=False, indent=1))
    kept = [q for q in generated if q["kept"]]
    print(f"kept {len(kept)} of {len(generated)}; verbatim fails {sum(not q['verbatim'] for q in generated)}", flush=True)
    for q in generated:
        if not q["kept"]:
            print(f"dropped [{q['type']}] {q['question'][:60]} key={q['answer']} solver={q['solver']} verbatim={q['verbatim']}", flush=True)
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
