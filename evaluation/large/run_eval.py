"""Ask the 30 test questions to the deployed AI chat as the test user and save raw results.

    python run_eval.py OUT_DIR
"""

import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

OUT = Path(sys.argv[1])
CHAT = "http://localhost:28000"
UNSURE = "확신할 수 없음 (원고와 설정만으로는 정할 수 없다)"
KUBECTL = ["kubectl", "--context", "lore-sentry", "-n", "prod"]

seeded = json.loads((OUT / "eval-seeded.json").read_text())
mcq = json.loads((OUT / "mcq_final.json").read_text())
extra = json.loads((OUT / "q2_checked.json").read_text())


def pick(items, matches):
    found = [next(q for q in items if q["question"].startswith(m)) for m in matches]
    return found


YESNO = pick(extra, ["서이안은 회백등대의", "빈은 갈매기목의", "백연주는 침잠도 생존자", "기억나침반은 사람의", "항로청의 공식 기록은"])
SIMPLE = [mcq[i - 1] for i in (1, 3, 4, 14, 15)]
MULTIHOP = [mcq[i - 1] for i in (5, 6, 7, 10, 11)]
HARD = [q for q in extra if q["type"] == "hard"][:5]
UNKNOWABLE = [mcq[i - 1] for i in (20, 21, 22, 23)] + pick(extra, [
    "파도문항 시장에서", "대조수 당시 조류 조정 장치의 가동과", "유리병 음성의 배경에 남은",
    "같은 시각에 사라진 두 화물선 중", "소명이 기억한 수로의 빛은", "유리병 음성의 원본이 실제로 녹음된",
])
SETS = [("yesno", YESNO), ("simple", SIMPLE), ("multihop", MULTIHOP), ("hard", HARD), ("unknowable", UNKNOWABLE)]


def prompt(category, q):
    if category == "yesno":
        return f"{q['question']}\n\n첫 줄에 '예', '아니요', '확신할 수 없음' 중 하나만 적고, 다음 줄부터 근거를 설명해 줘."
    return (
        f"{q['question']}\n\na. {q['a']}\nb. {q['b']}\nc. {q['c']}\nd. {q['d']}\ne. {UNSURE}\n\n"
        "첫 줄에 고른 선택지의 문자(a~e) 하나만 적고, 다음 줄부터 근거를 설명해 줘."
    )


def key(category, q):
    if category == "yesno":
        return {"yes": "예", "no": "아니요"}[q["answer"]]
    return q["answer"]


def parse(category, answer):
    first = answer.strip().splitlines()[0] if answer.strip() else ""
    head = re.sub(r"[*_`#>\s]", "", first)
    if category == "yesno":
        for word in ("확신할수없음", "아니요", "아니오", "예", "네"):
            if head.startswith(word):
                return {"아니오": "아니요", "네": "예", "확신할수없음": "확신할 수 없음"}.get(word, word)
        return None
    match = re.match(r"^\(?([a-eA-E])[\).:\s]?", head)
    if match:
        return match.group(1).lower()
    if head.startswith("확신할수없음"):
        return "e"
    return None


def exec_python(code):
    pod = subprocess.run(KUBECTL + ["get", "pods", "-o", "name"], capture_output=True, text=True, check=True).stdout.split()
    target = next(p for p in pod if "ai-chat-api" in p)
    script = (
        "import os, json, psycopg\n"
        "c = psycopg.connect(host=os.environ['DB_HOST'], port=os.environ.get('DB_PORT','5432'), dbname=os.environ['DB_NAME'], "
        "user=os.environ['DB_USERNAME'], password=os.environ['DB_PASSWORD'], autocommit=True)\n" + code
    )
    result = subprocess.run(KUBECTL + ["exec", "-i", target, "--", "python", "-"], input=script, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-500:])
    return result.stdout


def reset_usage():
    exec_python(f"c.execute(\"delete from chat_usage where user_id = '{seeded['user']}'\")\n")


def ask(client, text):
    session = client.post(f"/projects/{seeded['project']}/chat/sessions").raise_for_status().json()["id"]
    payload = {"content": text, "intent": "ask", "quote": None, "replaces": None, "client_message_id": str(uuid.uuid4())}
    begun = time.monotonic()
    first_activity = None
    parts, citations, activities, done, error, name = [], [], [], None, None, None
    with client.stream("POST", f"/chat/sessions/{session}/messages", json=payload) as response:
        if response.status_code != 200:
            response.read()
            return {"session": session, "error": f"HTTP {response.status_code} {response.text}", "seconds": 0}
        for line in response.iter_lines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                if name == "delta":
                    parts.append(data["text"])
                elif name == "citation":
                    citations.append(data["citation"])
                elif name == "activity":
                    activities.append(data)
                    first_activity = first_activity or round(time.monotonic() - begun, 1)
                elif name == "done":
                    done = data
                elif name == "error":
                    error = data
    return {
        "session": session,
        "answer": "".join(parts),
        "citations": citations,
        "activities": activities,
        "grounding": done["answer"]["grounding"] if done else None,
        "usage": done.get("usage") if done else None,
        "error": error,
        "seconds": round(time.monotonic() - begun, 1),
    }


def main():
    raw_path = OUT / "eval-raw.json"
    results = json.loads(raw_path.read_text()) if raw_path.exists() else []
    done_ids = {r["id"] for r in results}
    client = httpx.Client(base_url=CHAT, headers={"X-User-Id": seeded["user"], "X-User-Locale": "ko"}, timeout=240)
    number = 0
    for category, items in SETS:
        for q in items:
            number += 1
            qid = f"{category}-{number:02d}"
            if qid in done_ids:
                continue
            reset_usage()
            text = prompt(category, q)
            result = ask(client, text)
            result.update({
                "id": qid, "category": category, "question": q["question"], "prompt": text,
                "options": None if category == "yesno" else {k: q[k] for k in "abcd"} | {"e": UNSURE},
                "expected": key(category, q), "expected_chapters": q["chapters"], "why": q["why"],
                "chosen": parse(category, result.get("answer", "")),
            })
            result["correct"] = result["chosen"] == result["expected"]
            results.append(result)
            raw_path.write_text(json.dumps(results, ensure_ascii=False, indent=1))
            print(f"{qid} expected={result['expected']} chosen={result['chosen']} {'O' if result['correct'] else 'X'} "
                  f"{result['seconds']}s level={(result.get('grounding') or {}).get('level')} err={result.get('error')}", flush=True)
    sessions = [r["session"] for r in results]
    metrics = exec_python(
        f"sessions = {json.dumps(sessions)}\n"
        "out = {}\n"
        "for s in sessions:\n"
        "    t = c.execute('select steps, judge_calls, rejections, chars_read, input_tokens, cached_tokens, cache_write_tokens, output_tokens, total_ms, first_token_ms, reviews, grounding, status, failure_code from chat_turns where session_id = %s order by created_at desc limit 1', (s,)).fetchone()\n"
        "    held = [str(r[0]) for r in c.execute('select target_id from chat_held_documents where session_id = %s order by first_read_at', (s,)).fetchall()]\n"
        "    names = ['steps','judge_calls','rejections','chars_read','input_tokens','cached_tokens','cache_write_tokens','output_tokens','total_ms','first_token_ms','reviews','grounding','status','failure_code']\n"
        "    out[s] = {'turn': dict(zip(names, t)) if t else None, 'held': held}\n"
        "print(json.dumps(out, default=str))\n"
    )
    by_session = json.loads(metrics.strip().splitlines()[-1])
    titles = {}
    dataset = json.loads((OUT / "dataset.json").read_text())
    chapters = {c["number"]: c["title"] for c in dataset["chapters"]}
    settings = {s["key"]: s["title"] for s in dataset["settings"]}
    for k, file_id in seeded["ids"].items():
        titles[file_id] = chapters.get(int(k)) if str(k).isdigit() else settings.get(k, k)
    for r in results:
        m = by_session.get(r["session"], {})
        r["turn"] = m.get("turn")
        r["read"] = [titles.get(t, "메모") for t in m.get("held", [])]
    raw_path.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print("saved", raw_path, flush=True)


if __name__ == "__main__":
    main()
