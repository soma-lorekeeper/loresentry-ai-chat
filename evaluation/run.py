"""Seed the evaluation datasets, ask their questions and grade the answers.

    OPENAI_API_KEY=... python -m evaluation.run \\
        --content http://localhost:18001 --chat http://localhost:18000 \\
        --db "host=... dbname=ai_chat ..." --out evaluation/results/local

--db (the ai_chat database) is optional. With it the run waits for summaries and
adds per-turn metrics; without it the run waits --settle seconds instead.
Projects and chat data are deleted at the end unless --keep is given.
"""

import argparse
import asyncio
import json
import os
import time
import uuid
from collections import defaultdict
from pathlib import Path

import httpx
import psycopg
from openai import AsyncOpenAI

HERE = Path(__file__).parent
GRADER = os.getenv("EVAL_GRADER_MODEL", "gpt-6-luna")

GRADE_INSTRUCTIONS = """You grade an AI assistant's answer to an author's question about their own web novel.
You get the question, the facts the answer must cover, claims it must not make, the full text of the source documents (the expected sources and every document the answer cited), an outline of the whole story, and the answer with the titles of the documents it cited.
- A point is covered when the answer states it or something equivalent. Wording may differ.
- A false claim is anything the answer asserts about the story that contradicts the source documents or appears in none of them, including any forbidden claim. Details taken from the full source text are supported even if they are not in the outline. Saying that the manuscript does not show something is not a false claim when that is true.
- If the expected result is that the manuscript does not contain the answer, the answer is correct only if it clearly says it could not find it and does not invent one.
verdict: "correct" if every point is covered and there is no false claim; "partial" if at least one point is covered and there is no serious false claim; otherwise "wrong"."""

GRADE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "covered", "false_claims", "reason"],
    "properties": {
        "verdict": {"type": "string", "enum": ["correct", "partial", "wrong"]},
        "covered": {"type": "array", "items": {"type": "boolean"}},
        "false_claims": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
}


class Seeder:
    def __init__(self, base: str, user: str):
        self.client = httpx.Client(
            base_url=base, headers={"X-User-Id": user}, timeout=60
        )

    def create(self, project: str, payload: dict) -> str:
        response = self.client.post(f"/projects/{project}/files", json=payload)
        response.raise_for_status()
        return response.json()["id"]

    def save(self, file_id: str, title: str, body: str, properties=(), relations=()):
        current = self.client.get(f"/files/{file_id}/content")
        current.raise_for_status()
        blocks = [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}
            for text in body.split("\n\n")
            if text.strip()
        ]
        payload = {
            "title": title,
            "body": {"schema_version": 1, "doc": {"type": "doc", "content": blocks}},
            "properties": list(properties),
            "relations": list(relations),
        }
        response = self.client.put(
            f"/files/{file_id}/content",
            json=payload,
            headers={"If-Match": f'"{current.json()["revision_no"]}"'},
        )
        response.raise_for_status()

    def seed(self, dataset: dict) -> dict:
        response = self.client.post(
            "/projects",
            json={"name": dataset["project"], "description": dataset["description"]},
        )
        response.raise_for_status()
        project = response.json()["id"]
        episodes = [
            self.create(project, {"kind": "episode", "title": name})
            for name in dataset["episodes"]
        ]
        ids: dict = {}
        for chapter in dataset["chapters"]:
            ids[chapter["number"]] = self.create(
                project,
                {
                    "kind": "document",
                    "folder_code": "MANUSCRIPT",
                    "title": chapter["title"],
                    "episode_id": episodes[chapter["episode"]],
                },
            )
        for setting in dataset["settings"]:
            ids[setting["key"]] = self.create(
                project,
                {
                    "kind": "document",
                    "folder_code": setting["folder"],
                    "title": setting["title"],
                },
            )
        for chapter in dataset["chapters"]:
            self.save(ids[chapter["number"]], chapter["title"], chapter["body"])
        touching = defaultdict(list)
        for relation in dataset["relations"]:
            for one, other in (
                (relation["from"], relation["to"]),
                (relation["to"], relation["from"]),
            ):
                touching[one].append(
                    {
                        "target_document_id": ids[other],
                        "description": relation["description"],
                    }
                )
        for setting in dataset["settings"]:
            self.save(
                ids[setting["key"]],
                setting["title"],
                setting["body"],
                setting["properties"],
                touching.get(setting["key"], []),
            )
        for memo in dataset["memos"]:
            payload = {"scope": memo["scope"], "body": memo["body"]}
            if memo["title"]:
                payload["title"] = memo["title"]
            if memo["about"]:
                payload["document_id"] = ids[memo["about"]]
            self.client.post(
                f"/projects/{project}/memos", json=payload
            ).raise_for_status()
        return {"project": project, "ids": ids}

    def delete(self, project: str) -> None:
        self.client.post(f"/projects/{project}/trash")
        self.client.delete(f"/projects/{project}")


def wait_for_summaries(db: str, projects: dict[str, int], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with psycopg.connect(db) as conn:
            done = 0
            for project, expected in projects.items():
                made, waiting = conn.execute(
                    "select (select count(*) from document_summaries where project_id = %s),"
                    " (select count(*) from summary_queue where project_id = %s)",
                    (project, project),
                ).fetchone()
                done += made >= expected and waiting == 0
        if done == len(projects):
            print("summaries ready", flush=True)
            return
        time.sleep(10)
    print("summaries not ready before timeout; asking anyway", flush=True)


def ask(chat: httpx.Client, project: str, session: str | None, payload: dict) -> dict:
    if session is None:
        response = chat.post(f"/projects/{project}/chat/sessions")
        response.raise_for_status()
        session = response.json()["id"]
    begun = time.monotonic()
    text, citations, done, error, name = [], [], None, None, None
    with chat.stream(
        "POST", f"/chat/sessions/{session}/messages", json=payload
    ) as response:
        if response.status_code != 200:
            response.read()
            return {
                "session": session,
                "error": f"HTTP {response.status_code} {response.text}",
            }
        for line in response.iter_lines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                if name == "delta":
                    text.append(data["text"])
                elif name == "citation":
                    citations.append(data["citation"])
                elif name == "done":
                    done = data
                elif name == "error":
                    error = data
    return {
        "session": session,
        "answer": "".join(text),
        "citations": citations,
        "grounding": done["answer"]["grounding"] if done else None,
        "error": error,
        "seconds": round(time.monotonic() - begun, 1),
    }


def turn_metrics(db: str, session: str) -> dict:
    with psycopg.connect(db) as conn:
        row = conn.execute(
            "select steps, judge_calls, rejections, chars_read, input_tokens, cached_tokens,"
            " output_tokens, reviews from chat_turns where session_id = %s"
            " order by created_at desc limit 1",
            (session,),
        ).fetchone()
    keys = [
        "steps",
        "judge_calls",
        "rejections",
        "chars_read",
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "reviews",
    ]
    return dict(zip(keys, row, strict=True)) if row else {}


def ground_truth(dataset: dict, question: dict, cited: list[str]) -> str:
    titles = []
    for source in question["sources"]:
        if isinstance(source, int):
            titles.append(dataset["chapters"][source - 1]["title"])
        else:
            titles.append(
                next(s for s in dataset["settings"] if s["key"] == source)["title"]
            )
    titles += [title for title in cited if title not in titles]
    documents = {c["title"]: c["body"] for c in dataset["chapters"]}
    documents.update({f"{s['title']}": s["body"] for s in dataset["settings"]})
    parts = [f"[{title}]\n{documents[title]}" for title in titles if title in documents]
    outline = "\n".join(
        f"{c['title']}: {' '.join(c['facts'])}" for c in dataset["chapters"]
    )
    memos = "\n".join(f"메모: {m['body']}" for m in dataset["memos"])
    return "\n".join(parts) + f"\n\n전체 줄거리 사실:\n{outline}\n{memos}"


async def grade(
    client: AsyncOpenAI, dataset: dict, question: dict, result: dict
) -> dict:
    expected = "원고에 답이 없음" if question.get("absent") else "원고에 답이 있음"
    prompt = (
        f"질문: {question['question']}\n"
        + (f"인용: {question['quote']['text']}\n" if question.get("quote") else "")
        + f"기대 결과: {expected}\n"
        + "다뤄야 할 사실:\n"
        + "\n".join(f"- {p}" for p in question["points"])
        + "\n"
        + (
            "해서는 안 되는 주장:\n"
            + "\n".join(f"- {f}" for f in question.get("forbid", []))
            + "\n"
            if question.get("forbid")
            else ""
        )
        + (f"채점 메모: {question['note']}\n" if question.get("note") else "")
        + f"\n원문 근거:\n{ground_truth(dataset, question, [c['title'] for c in result.get('citations', [])])}\n\n"
        + f"답:\n{result.get('answer', '')}\n\n인용한 문서: "
        + ", ".join(c["title"] for c in result.get("citations", []))
    )
    response = await client.responses.create(
        model=GRADER,
        instructions=GRADE_INSTRUCTIONS,
        input=prompt,
        reasoning={"effort": "medium"},
        text={
            "format": {
                "type": "json_schema",
                "name": "grade",
                "strict": True,
                "schema": GRADE_SCHEMA,
            }
        },
        max_output_tokens=4000,
        store=False,
    )
    return json.loads(response.output_text)


def recall(question: dict, result: dict, ids: dict) -> float | None:
    if not question["sources"]:
        return None
    cited = {c.get("file_id") for c in result.get("citations", [])}
    wanted = [ids[s] for s in question["sources"]]
    return sum(target in cited for target in wanted) / len(wanted)


def sessions_of(questions: list[dict]) -> dict[str, str]:
    labels = {q["id"]: q.get("session") for q in questions}
    for q in questions:
        if q.get("after"):
            labels[q["after"]] = labels[q["id"]] = q["session"]
    return labels


def run_dataset(args, dataset: dict, seeded: dict, chat: httpx.Client) -> list[dict]:
    labels = sessions_of(dataset["questions"])
    sessions: dict[str, str] = {}
    results = []
    for question in dataset["questions"]:
        if (
            args.only
            and question["id"] not in args.only
            and not any(
                q.get("after") == question["id"] and q["id"] in args.only
                for q in dataset["questions"]
            )
        ):
            continue
        payload = {
            "content": question["question"],
            "intent": question.get("intent", "ask"),
            "quote": None,
            "focus_file_id": None,
            "replaces": None,
            "client_message_id": str(uuid.uuid4()),
        }
        if question.get("quote"):
            chapter = dataset["chapters"][question["quote"]["chapter"] - 1]
            file_id = seeded["ids"][chapter["number"]]
            payload["quote"] = {
                "file_id": file_id,
                "title": chapter["title"],
                "doc_type": "manuscript",
                "text": question["quote"]["text"],
            }
        label = labels[question["id"]]
        result = ask(
            chat, seeded["project"], sessions.get(label) if label else None, payload
        )
        if label:
            sessions[label] = result["session"]
        if args.db and not result.get("error"):
            result["metrics"] = turn_metrics(args.db, result["session"])
        result["recall"] = recall(question, result, seeded["ids"])
        level = (result.get("grounding") or {}).get("level")
        print(
            f"{question['id']} {question['type']:13} {level} {result['seconds']}s recall={result['recall']}",
            flush=True,
        )
        results.append({"question": question, "result": result})
    return results


def summarize(rows: list[dict]) -> dict:
    total = len(rows)
    verdicts = defaultdict(int)
    for row in rows:
        verdicts[row["grade"]["verdict"]] += 1
    recalls = [
        r["result"]["recall"] for r in rows if r["result"].get("recall") is not None
    ]
    seconds = [r["result"]["seconds"] for r in rows]
    return {
        "questions": total,
        "correct": verdicts["correct"],
        "partial": verdicts["partial"],
        "wrong": verdicts["wrong"],
        "score": round((verdicts["correct"] + 0.5 * verdicts["partial"]) / total, 3)
        if total
        else 0,
        "recall": round(sum(recalls) / len(recalls), 3) if recalls else None,
        "seconds": round(sum(seconds) / len(seconds), 1) if seconds else None,
        "false_claims": sum(len(r["grade"]["false_claims"]) for r in rows),
        "not_enough": sum(
            ((r["result"].get("grounding") or {}).get("level") == "NOT_ENOUGH")
            for r in rows
        ),
        "false_not_enough": sum(
            ((r["result"].get("grounding") or {}).get("level") == "NOT_ENOUGH")
            and not r["question"].get("absent")
            for r in rows
        ),
    }


def report(rows: list[dict], out: Path) -> str:
    lines = ["# Chat evaluation", ""]
    overall = summarize(rows)
    lines += [
        "| | 질문 | 정답 | 부분 | 오답 | 점수 | 근거 재현율 | 평균 초 | 잘못된 주장 | NOT_ENOUGH | 잘못된 NOT_ENOUGH |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    def line(name, s):
        return f"| {name} | {s['questions']} | {s['correct']} | {s['partial']} | {s['wrong']} | {s['score']} | {s['recall']} | {s['seconds']} | {s['false_claims']} | {s['not_enough']} | {s['false_not_enough']} |"

    lines.append(line("전체", overall))
    by_type = defaultdict(list)
    by_set = defaultdict(list)
    for row in rows:
        by_type[row["question"]["type"]].append(row)
        by_set[row["dataset"]].append(row)
    for name, group in sorted(by_set.items()):
        lines.append(line(name, summarize(group)))
    for name, group in sorted(by_type.items()):
        lines.append(line(name, summarize(group)))
    lines += [
        "",
        "| id | 유형 | 판정 | 수준 | 재현율 | 단계 | 거절 | 읽은 글자 | 초 | 이유 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        q, r, g = row["question"], row["result"], row["grade"]
        m = r.get("metrics", {})
        level = (r.get("grounding") or {}).get("level")
        reason = g["reason"].replace("|", "/").replace("\n", " ")[:160]
        lines.append(
            f"| {q['id']} | {q['type']} | {g['verdict']} | {level} | {r.get('recall')} | {m.get('steps')} | {m.get('rejections')} | {m.get('chars_read')} | {r['seconds']} | {reason} |"
        )
    text = "\n".join(lines) + "\n"
    (out / "report.md").write_text(text)
    (out / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    (out / "summary.json").write_text(json.dumps(overall, ensure_ascii=False, indent=1))
    return text


VOTES = 3
RANK = {"correct": 0, "partial": 1, "wrong": 2}


def majority(grades: list[dict]) -> dict:
    """The verdict most graders gave; a tie goes to the worse verdict."""
    counts = {verdict: 0 for verdict in RANK}
    for item in grades:
        counts[item["verdict"]] += 1
    verdict = max(RANK, key=lambda v: (counts[v], RANK[v]))
    chosen = next(item for item in grades if item["verdict"] == verdict)
    return {**chosen, "votes": [item["verdict"] for item in grades]}


async def grade_all(rows: list[dict], datasets: dict) -> None:
    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=2)
    gate = asyncio.Semaphore(6)

    async def one(row):
        async with gate:
            if row["result"].get("error"):
                row["grade"] = {
                    "verdict": "wrong",
                    "covered": [],
                    "false_claims": [],
                    "reason": f"error: {row['result']['error']}",
                }
                return
            grades = await asyncio.gather(
                *(
                    grade(
                        client, datasets[row["dataset"]], row["question"], row["result"]
                    )
                    for _ in range(VOTES)
                )
            )
            row["grade"] = majority(grades)

    await asyncio.gather(*(one(row) for row in rows))
    await client.close()


def load_datasets(names: list[str] | None = None) -> dict:
    datasets = {}
    for path in sorted((HERE / "datasets").glob("*.json")):
        dataset = json.loads(path.read_text())
        if not names or dataset["name"] in names:
            datasets[dataset["name"]] = dataset
    return datasets


def regrade(out: Path) -> None:
    rows = json.loads((out / "results.json").read_text())
    datasets = load_datasets()
    for row in rows:
        row["question"] = next(
            q
            for q in datasets[row["dataset"]]["questions"]
            if q["id"] == row["question"]["id"]
        )
    asyncio.run(grade_all(rows, datasets))
    print(report(rows, out))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--content", required=True)
    parser.add_argument("--chat", required=True)
    parser.add_argument("--db")
    parser.add_argument("--user", default=str(uuid.uuid4()))
    parser.add_argument("--datasets", nargs="*", default=[])
    parser.add_argument("--only", nargs="*", default=[], help="question ids to ask")
    parser.add_argument("--settle", type=float, default=900)
    parser.add_argument("--summary-timeout", type=float, default=1800)
    parser.add_argument("--out", default=str(HERE / "results" / "latest"))
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--regrade", help="grade results.json in this directory again")
    args = parser.parse_args()

    if args.regrade:
        regrade(Path(args.regrade))
        return

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    datasets = load_datasets(args.datasets)

    seeder = Seeder(args.content, args.user)
    seeded = {name: seeder.seed(dataset) for name, dataset in datasets.items()}
    print("seeded", {name: s["project"] for name, s in seeded.items()}, flush=True)
    if args.db:
        expected = {
            s["project"]: sum(bool(c["body"].strip()) for c in datasets[n]["chapters"])
            + sum(bool(t["body"].strip()) for t in datasets[n]["settings"])
            for n, s in seeded.items()
        }
        wait_for_summaries(args.db, expected, args.summary_timeout)
    else:
        time.sleep(args.settle)

    chat = httpx.Client(
        base_url=args.chat,
        headers={"X-User-Id": args.user, "X-User-Locale": "ko"},
        timeout=240,
    )
    rows = []
    try:
        for name, dataset in datasets.items():
            for row in run_dataset(args, dataset, seeded[name], chat):
                row["dataset"] = name
                rows.append(row)
        asyncio.run(grade_all(rows, datasets))
        print(report(rows, out))
    finally:
        if not args.keep:
            for s in seeded.values():
                chat.delete(f"/projects/{s['project']}/chat/data")
                seeder.delete(s["project"])
            print("cleaned up", flush=True)


if __name__ == "__main__":
    main()
