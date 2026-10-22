import asyncio
import json
import os
import sys
from pathlib import Path

from openai import AsyncOpenAI

OUT = Path(sys.argv[1])
sys.path.insert(0, str(Path(__file__).parent))
from mcq import MATERIAL, UNSURE, obj, texts  # noqa: E402

S = {"type": "string"}

GEN_SCHEMA = obj({"questions": {"type": "array", "items": obj({
    "type": {"type": "string", "enum": ["yesno", "hard", "unknowable"]},
    "question": S,
    "a": S, "b": S, "c": S, "d": S,
    "answer": {"type": "string", "enum": ["a", "b", "c", "d", "e", "yes", "no"]},
    "chapters": {"type": "array", "items": {"type": "integer"}},
    "evidence": {"type": "array", "items": obj({"source": S, "quote": S})},
    "why": S,
})}})

SOLVE_SCHEMA = obj({"answers": {"type": "array", "items": obj({
    "id": {"type": "integer"},
    "choice": {"type": "string", "enum": ["a", "b", "c", "d", "e", "yes", "no", "unsure"]},
    "reason": S,
})}})

GEN_PROMPT = f"""위 문서들은 한 장편 웹소설 프로젝트의 원고 100화, 설정 문서, 작가 메모 전부다.
이 작품으로 AI 챗을 채점할 문제를 만든다. 정답은 원고와 설정 문서만으로 한 가지로 정해져야 한다.

1. yesno 8개: 예/아니요로 답하는 단순 사실 질문. a~d는 빈 문자열, answer는 "yes" 또는 "no". 예와 아니요가 절반씩. 아니요가 정답인 질문은 원고에 비슷한 사실이 있어 헷갈릴 만한 것(인물·숫자·장소를 바꾼 것)으로 만든다.
2. hard 6개: 객관식. 세 부 이상에 걸친 근거를 모아야 풀리는 문제(여러 회차에 흩어진 사실의 개수 세기, 세 사건 이상의 인과 연결, 한 인물의 입장 변화 추적 등). 한두 회차만 읽으면 오답 선택지가 그럴듯해야 한다.
3. unknowable 10개: 객관식, 정답은 e("{UNSURE}"). 원고가 일부러 밝히지 않거나 아직 확인되지 않았다고 적은 것(누가, 왜, 정확히 언제, 어떤 원리로)을 묻는다. 원고 어디에도 답이 없음을 전체를 보고 확인한 것만 낸다. 아래는 이미 있는 문제라 다시 내지 않는다: 5화 장부 페이지를 자른 사람, 실종 화물선 두 척이 사라진 물리적 원인, 유리병 원본 음성을 녹음한 사람, 새벽 2시에 두 번 적힌 조류 수치 중 정확한 쪽.

객관식 선택지는 a~d 네 개를 쓰고 e는 쓰지 않는다. 오답은 이 작품의 실제 이름·장소·숫자·사건으로 만들고, 정답만 길거나 구체적이지 않게 한다.
chapters는 근거 회차. evidence는 원문을 그대로 옮긴 문장 2~4개(source는 "12화", "설정: 이름", "메모: 제목"). why는 정답과 주요 오답의 이유 2~3문장. 한국어로 쓴다."""

SOLVE_PROMPT = """위 문서들은 한 장편 웹소설 프로젝트의 원고 100화, 설정 문서, 작가 메모 전부다.
아래 문제를 원고와 설정 문서만 근거로 푼다. 작가 메모는 계획일 뿐 이야기 속 사실이 아니다. 설정 문서와 원고가 다르면 원고가 이야기 속 사실이다.
예/아니요 문제는 yes, no, unsure 중 하나를 고른다. 객관식은 a~e 중 하나를 고르고, 문서로 정답을 한 가지로 정할 수 없으면 e를 고른다.
각 문제의 id와 고른 값, 근거 회차를 포함한 짧은 이유를 낸다.

"""


async def call(client, prompt, schema, name, effort):
    for attempt in range(3):
        try:
            response = await client.responses.create(
                model="gpt-6-luna", input=MATERIAL + "\n\n" + prompt, reasoning={"effort": effort},
                max_output_tokens=60000, store=False, prompt_cache_key="mcq-material",
                text={"format": {"type": "json_schema", "name": name, "strict": True, "schema": schema}},
            )
            return json.loads(response.output_text)
        except Exception as error:
            print(f"{name} retry {attempt + 1}: {error}", flush=True)
    raise RuntimeError(name)


def render(questions):
    out = []
    for i, q in enumerate(questions, 1):
        if q["type"] == "yesno":
            out.append(f"[{i}] (예/아니요) {q['question']}")
        else:
            out.append(f"[{i}] {q['question']}\na. {q['a']}\nb. {q['b']}\nc. {q['c']}\nd. {q['d']}\ne. {UNSURE}")
    return "\n\n".join(out)


async def main():
    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=3, timeout=1200)
    path = OUT / "q2_generated.json"
    generated = json.loads(path.read_text()) if path.exists() else None
    if generated is None:
        generated = (await call(client, GEN_PROMPT, GEN_SCHEMA, "q2", "high"))["questions"]
        path.write_text(json.dumps(generated, ensure_ascii=False, indent=1))
    print("generated", len(generated), flush=True)
    for q in generated:
        q["verbatim"] = all(e["quote"] in texts.get(e["source"], "") for e in q["evidence"])
    solved = await asyncio.gather(*(call(client, SOLVE_PROMPT + render(generated), SOLVE_SCHEMA, f"s{n}", "high") for n in range(2)))
    runs = [{a["id"]: a["choice"] for a in s["answers"]} for s in solved]
    for i, q in enumerate(generated, 1):
        q["solver"] = [run.get(i) for run in runs]
        q["agree"] = all(c == q["answer"] for c in q["solver"])
    (OUT / "q2_checked.json").write_text(json.dumps(generated, ensure_ascii=False, indent=1))
    for q in generated:
        print(q["type"], q["answer"], q["solver"], "verbatim" if q["verbatim"] else "QUOTE?", q["question"][:70], flush=True)
    await client.close()


asyncio.run(main())
