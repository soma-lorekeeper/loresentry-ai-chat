"""Run the setting error check through the real services on a planted dataset.

    .venv/bin/python -m evaluation.setting_check_prod \\
        --content http://localhost:28001 --chat http://localhost:28000 \\
        --dataset evaluation/datasets/setting-check-prod.json \\
        --runs 3 --out evaluation/results/setting-check/prod

For every case it seeds a fresh project in content (one episode, the chapters in
order, the setting documents with the core paragraph marked), starts a setting
check for the last chapter through the ai-chat API, waits for it to finish and
grades the findings against the case's `expect`, `avoid` and `may` entries with
the same matching rules as evaluation/setting_check.py. Projects are deleted at
the end unless --keep is given. Tokens are not exposed by the API; pass
--usage <json> (run id -> usage) later with --rescore to add them.
"""

import argparse
import json
import sys
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import httpx

from evaluation.setting_check import classify, norm

HEADERS = {"X-User-Locale": "ko", "X-User-Display-Name": "setting-check-eval"}


class Seeder:
    def __init__(self, base: str, user: str):
        self.client = httpx.Client(base_url=base, headers={"X-User-Id": user}, timeout=60)

    def create(self, project: str, payload: dict) -> str:
        response = self.client.post(f"/projects/{project}/files", json=payload)
        response.raise_for_status()
        return response.json()["id"]

    def save(self, file_id: str, title: str, body: list[str], core: list[int], properties=()):
        current = self.client.get(f"/files/{file_id}/content")
        current.raise_for_status()
        blocks = []
        for index, text in enumerate(body):
            block = {"type": "paragraph", "content": [{"type": "text", "text": text}]}
            if index in core:
                block["attrs"] = {"core": True}
            blocks.append(block)
        payload = {
            "title": title,
            "body": {"schema_version": 1, "doc": {"type": "doc", "content": blocks}},
            "properties": list(properties),
            "relations": [],
        }
        response = self.client.put(
            f"/files/{file_id}/content",
            json=payload,
            headers={"If-Match": f'"{current.json()["revision_no"]}"'},
        )
        response.raise_for_status()

    def seed(self, case: dict) -> dict:
        response = self.client.post(
            "/projects",
            json={"name": f"설정 점검 평가 · {case['id']}", "description": case["title"]},
        )
        response.raise_for_status()
        project = response.json()["id"]
        episode = self.create(project, {"kind": "episode", "title": "Episode 1"})
        ids: dict[str, str] = {}
        for chapter in case["chapters"]:
            ids[chapter["key"]] = self.create(
                project,
                {"kind": "document", "folder_code": "MANUSCRIPT", "title": chapter["title"], "episode_id": episode},
            )
        for setting in case["settings"]:
            ids[setting["key"]] = self.create(
                project, {"kind": "document", "folder_code": setting["folder"], "title": setting["title"]}
            )
        for chapter in case["chapters"]:
            self.save(ids[chapter["key"]], chapter["title"], chapter["body"], [])
        for setting in case["settings"]:
            self.save(ids[setting["key"]], setting["title"], setting["body"], setting.get("core", []), setting["properties"])
        return {"project": project, "ids": ids}

    def delete(self, project: str) -> None:
        self.client.post(f"/projects/{project}/trash")
        self.client.delete(f"/projects/{project}")


def run_check(chat: httpx.Client, project: str, document: str, timeout: float) -> dict:
    started = time.monotonic()
    response = chat.post(f"/projects/{project}/setting-checks", json={"document_id": document})
    if response.status_code != 202:
        return {"error": f"HTTP {response.status_code} {response.text}", "seconds": 0.0}
    run_id = response.json()["id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(5)
        current = chat.get(f"/setting-checks/{run_id}")
        current.raise_for_status()
        run = current.json()
        if run["status"] not in ("RUNNING",):
            return {**run, "seconds": round(time.monotonic() - started, 1)}
    return {"id": run_id, "status": "TIMEOUT", "seconds": round(time.monotonic() - started, 1), "findings": []}


def describe(case: dict, by_id: dict[str, str], finding: dict) -> dict:
    source = by_id.get(finding["source"]["document_id"], finding["source"]["document_id"])
    group, entry = classify(case, finding["quote"], source)
    return {
        "id": finding["id"],
        "quote": finding["quote"],
        "source": source,
        "source_quote": finding["source"]["quote"],
        "kind": finding["kind"],
        "core": finding["core"],
        "confidence": finding["confidence"],
        "explanation": finding["explanation"],
        "group": group,
        "entry": entry,
    }


def score(case: dict, runs: list[dict]) -> dict:
    expected = {entry["id"]: 0 for entry in case["expect"]}
    traps = {entry["id"]: 0 for entry in case["avoid"]}
    extras: Counter = Counter()
    sets = []
    for run in runs:
        for key in {f["entry"] for f in run["findings"] if f["group"] == "expect"}:
            expected[key] += 1
        for key in {f["entry"] for f in run["findings"] if f["group"] == "avoid"}:
            traps[key] += 1
        for f in run["findings"]:
            if f["group"] == "extra":
                extras[norm(f["quote"])] += 1
        sets.append(frozenset(f["entry"] or norm(f["quote"]) for f in run["findings"]))
    pairs = [(a, b) for i, a in enumerate(sets) for b in sets[i + 1 :]]
    agreement = (
        sum(len(a & b) / len(a | b) if a | b else 1.0 for a, b in pairs) / len(pairs) if pairs else 1.0
    )
    return {"expected": expected, "traps": traps, "extras": dict(extras), "agreement": round(agreement, 2)}


def summarize(cases: list[dict], results: dict, runs: int) -> dict:
    def block(subset: list[dict]) -> dict:
        expected = sum(len(c["expect"]) for c in subset)
        traps = sum(len(c["avoid"]) for c in subset)
        hits = sum(sum(results[c["id"]]["score"]["expected"].values()) for c in subset)
        stable = sum(sum(1 for n in results[c["id"]]["score"]["expected"].values() if n == runs) for c in subset)
        trap_hits = sum(sum(results[c["id"]]["score"]["traps"].values()) for c in subset)
        all_runs = [run for c in subset for run in results[c["id"]]["runs"]]
        findings = sum(len(run["findings"]) for run in all_runs)
        wanted = sum(sum(1 for f in run["findings"] if f["group"] in ("expect", "may")) for run in all_runs)
        extras = sum(sum(1 for f in run["findings"] if f["group"] == "extra") for run in all_runs)
        seconds = sorted(run["seconds"] for run in all_runs if not run.get("error"))
        failed = sum(1 for run in all_runs if run.get("error") or run.get("status") != "DONE")
        usage = defaultdict(int)
        for run in all_runs:
            for key, value in (run.get("usage") or {}).items():
                usage[key] += value
        return {
            "cases": len(subset),
            "checks": len(all_runs),
            "failed_checks": failed,
            "expected": expected,
            "recall": round(hits / (expected * runs), 3) if expected else None,
            "stable_recall": round(stable / expected, 3) if expected else None,
            "trap_rate": round(trap_hits / (traps * runs), 3) if traps else None,
            "precision": round(wanted / findings, 3) if findings else None,
            "extras_per_run": round(extras / len(all_runs), 2) if all_runs else None,
            "agreement": round(sum(results[c["id"]]["score"]["agreement"] for c in subset) / len(subset), 2) if subset else None,
            "seconds_mean": round(sum(seconds) / len(seconds), 1) if seconds else None,
            "seconds_median": seconds[len(seconds) // 2] if seconds else None,
            "seconds_max": seconds[-1] if seconds else None,
            "usage": dict(usage) or None,
        }

    by_work = defaultdict(list)
    for case in cases:
        by_work[case["work"]].append(case)
    mid = [c for c in cases if c["work"] != "mumyeong-suro"]
    long = [c for c in cases if c["work"] == "mumyeong-suro"]
    return {
        "runs": runs,
        "all": block(cases),
        "mid_length": block(mid),
        "long": block(long),
        "by_work": {work: block(subset) for work, subset in by_work.items()},
    }


def report(cases: list[dict], results: dict, summary: dict) -> str:
    runs = summary["runs"]
    lines = ["# Setting check evaluation (real services)", ""]
    for name, key in (("전체", "all"), ("중편 3편", "mid_length"), ("장편 1편", "long")):
        s = summary[key]
        lines.append(
            f"- {name}: 케이스 {s['cases']}, 점검 {s['checks']}(실패 {s['failed_checks']}), 재현율 {s['recall']} (3회 모두 {s['stable_recall']}), "
            f"함정 오탐률 {s['trap_rate']}, 정밀도 {s['precision']}, 정답표 밖 보고/회 {s['extras_per_run']}, 일치도 {s['agreement']}, "
            f"시간 평균/중앙/최대 {s['seconds_mean']}/{s['seconds_median']}/{s['seconds_max']}초"
        )
    lines += ["", "| Case | Expected (hits/runs) | Traps hit | Unlisted | Agreement | Seconds |", "| --- | --- | --- | --- | --- | --- |"]
    for case in cases:
        s = results[case["id"]]["score"]
        expected = ", ".join(f"{k} {v}/{runs}" for k, v in s["expected"].items()) or "—"
        traps = ", ".join(f"{k} {v}/{runs}" for k, v in s["traps"].items() if v) or "0"
        unlisted = sum(s["extras"].values())
        secs = ", ".join(str(run["seconds"]) for run in results[case["id"]]["runs"])
        lines.append(f"| {case['id']} | {expected} | {traps} | {unlisted} | {s['agreement']} | {secs} |")
    lines += ["", "## Findings", ""]
    for case in cases:
        lines.append(f"### {case['id']} · {case['title']}")
        for n, run in enumerate(results[case["id"]]["runs"], start=1):
            status = run.get("status") or run.get("error")
            lines.append(f"- run {n}: {status}, {len(run['findings'])} findings, {run['seconds']}s, run id {run.get('run_id')}")
            for item in run["findings"]:
                tag = item["entry"] or "UNLISTED"
                lines.append(
                    f"  - [{item['group']}:{tag}] ({item['confidence']}{', core' if item['core'] else ''}) “{item['quote']}” ← {item['source']}: “{item['source_quote']}”"
                )
                lines.append(f"    {item['explanation']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def write(out: Path, cases: list[dict], results: dict, runs: int) -> dict:
    for case in cases:
        results[case["id"]]["score"] = score(case, results[case["id"]]["runs"])
    summary = summarize(cases, results, runs)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    (out / "report.md").write_text(report(cases, results, summary))
    print(json.dumps({k: summary[k] for k in ("all", "mid_length", "long")}, ensure_ascii=False, indent=1))
    return summary


def load_cases(args) -> list[dict]:
    data = json.loads(Path(args.dataset).read_text())
    return [case for case in data["cases"] if not args.only or case["id"] in args.only]


def rescore(args) -> None:
    out = Path(args.rescore)
    cases = load_cases(args)
    results = json.loads((out / "results.json").read_text())
    usage = json.loads(Path(args.usage).read_text()) if args.usage else {}
    for case in cases:
        for run in results[case["id"]]["runs"]:
            for item in run["findings"]:
                item["group"], item["entry"] = classify(case, item["quote"], item["source"])
            if run.get("run_id") in usage:
                run["usage"] = usage[run["run_id"]]
    runs = max(len(results[c["id"]]["runs"]) for c in cases)
    write(out, cases, results, runs)


def evaluate(args) -> None:
    cases = load_cases(args)
    user = args.user or str(uuid.uuid4())
    print(f"user {user}", flush=True)
    seeder = Seeder(args.content, user)
    chat = httpx.Client(base_url=args.chat, headers={"X-User-Id": user, **HEADERS}, timeout=60)
    results: dict[str, Any] = {case["id"]: {"runs": [], "project": None} for case in cases}
    out = Path(args.out)
    seeded: list[tuple[dict, dict]] = []
    try:
        for case in cases:
            seed = seeder.seed(case)
            results[case["id"]]["project"] = seed["project"]
            seeded.append((case, seed))
            print(f"seeded {case['id']} -> {seed['project']}", flush=True)
        for case, seed in seeded:
            by_id = {doc_id: key for key, doc_id in seed["ids"].items()}
            document = seed["ids"][case["chapters"][-1]["key"]]
            for n in range(args.runs):
                run = run_check(chat, seed["project"], document, args.timeout)
                findings = [describe(case, by_id, f) for f in run.get("findings", [])]
                results[case["id"]]["runs"].append(
                    {
                        "run_id": run.get("id"),
                        "status": run.get("status"),
                        "error": run.get("error") or run.get("failure_code"),
                        "seconds": run["seconds"],
                        "created_at": run.get("created_at"),
                        "finished_at": run.get("finished_at"),
                        "read": run.get("read"),
                        "usage": run.get("usage"),
                        "findings": findings,
                    }
                )
                hit = sum(1 for f in findings if f["group"] == "expect")
                print(
                    f"{case['id']} run {n + 1}: {run.get('status')} {run['seconds']}s findings={len(findings)} "
                    f"expect={hit}/{len(case['expect'])} avoid={sum(1 for f in findings if f['group'] == 'avoid')} "
                    f"extra={sum(1 for f in findings if f['group'] == 'extra')}",
                    flush=True,
                )
                write(out, cases, results, args.runs)
    finally:
        if not args.keep:
            for case, seed in seeded:
                try:
                    seeder.delete(seed["project"])
                except Exception as error:  # noqa: BLE001
                    print(f"delete failed for {seed['project']}: {error}", file=sys.stderr)
            print(f"deleted {len(seeded)} projects", flush=True)
    write(out, cases, results, args.runs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--content", default="http://localhost:28001")
    parser.add_argument("--chat", default="http://localhost:28000")
    parser.add_argument("--dataset", default="evaluation/datasets/setting-check-prod.json")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--user")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--out", default="evaluation/results/setting-check/prod")
    parser.add_argument("--rescore")
    parser.add_argument("--usage")
    args = parser.parse_args()
    if args.rescore:
        rescore(args)
    else:
        evaluate(args)


if __name__ == "__main__":
    main()
