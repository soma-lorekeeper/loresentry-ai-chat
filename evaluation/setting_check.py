"""Run the setting error check on known cases and measure what it catches.

    OPENAI_API_KEY=... python -m evaluation.setting_check --runs 3 \\
        --out evaluation/results/setting-check/latest

Each case in datasets/setting-check.json is a small project: setting documents,
earlier chapters and the checked chapter (the last one), or a chapter of one of
the chat datasets (`from`). `expect` lists contradictions the check must report,
`avoid` sentences it must not report (story changes, lies, dreams, flashbacks,
rumours, details the settings never mention), and `may` contradictions that are
fine either way. A finding matches an entry when its chapter quote contains one
of the entry's quotes (or the other way round) and its source is one of the
entry's sources.

The run goes through the same steps as CheckRunner.check without a database:
finding passes, quote verification, merging, one judgement per candidate. It
keeps every step so a miss can be traced to the finder or the judge, and runs
each case --runs times to see how stable the result is.
"""

import argparse
import asyncio
import json
import time
import uuid
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from app.content import Document, Property
from app.model import OpenAICheckModel, Usage
from app.setting_check.findings import Candidate, Reading, dedupe, judged, norm
from app.setting_check.prompts import JUDGE_INSTRUCTIONS, JUDGE_SCHEMA, judge_input
from app.setting_check.runner import CheckRunner, Tally
from app.settings import Settings

HERE = Path(__file__).parent
NAMESPACE = uuid.UUID("6f1d2c3a-5b4e-4f60-9a7b-1c2d3e4f5a6b")


def doc_id(case: str, key: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{case}:{key}"))


def from_chat_dataset(case: dict[str, Any]) -> dict[str, Any]:
    data = json.loads((HERE / "datasets" / f"{case['from']}.json").read_text())
    chapters = [
        {"key": f"c{item['number']}", "title": item["title"], "body": [item["body"]]}
        for item in data["chapters"]
        if item["number"] <= case["chapter"]
    ]
    settings = [
        {
            "key": item["key"],
            "title": item["title"],
            "folder": item["folder"],
            "properties": item.get("properties", []),
            "body": [item["body"]],
            "core": [],
        }
        for item in data["settings"]
    ]
    return {**case, "settings": settings, "chapters": chapters}


def reading_of(case: dict[str, Any]) -> tuple[Reading, dict[str, str]]:
    keys: dict[str, str] = {}

    def document(item: dict[str, Any], folder: str, ordinal: int | None) -> Document:
        identifier = doc_id(case["id"], item["key"])
        keys[identifier] = item["key"]
        body = item["body"]
        return Document(
            id=identifier,
            title=item["title"],
            folder_code=folder,
            body_text="\n\n".join(body),
            revision_no=1,
            ordinal=ordinal,
            properties=tuple(
                Property(prop["key"], prop["value"])
                for prop in item.get("properties", [])
            ),
            core_facts=tuple(body[index] for index in item.get("core", [])),
        )

    chapters = [
        document(item, "MANUSCRIPT", number)
        for number, item in enumerate(case["chapters"], start=1)
    ]
    settings = [document(item, item["folder"], None) for item in case["settings"]]
    return Reading(chapters[-1], tuple(chapters[:-1]), tuple(settings)), keys


def near(entry: dict[str, Any], quote: str) -> bool:
    got = norm(quote)
    return any(
        want and got and (want in got or got in want)
        for want in map(norm, entry["quote"])
    )


def matches(entry: dict[str, Any], quote: str, source: str) -> bool:
    return near(entry, quote) and source in entry["sources"]


def classify(case: dict[str, Any], quote: str, source: str) -> tuple[str, str | None]:
    for group in ("expect", "may"):
        for entry in case[group]:
            if matches(entry, quote, source):
                return group, entry["id"]
    for entry in case["avoid"]:
        if near(entry, quote):
            return "avoid", entry["id"]
    return "extra", None


class NoStore:
    async def stage(self, run_id: uuid.UUID, stage: str) -> bool:
        return True


async def check_once(
    runner: CheckRunner,
    model: OpenAICheckModel,
    case: dict[str, Any],
    gate: asyncio.Semaphore,
) -> dict[str, Any]:
    reading, keys = reading_of(case)
    locale = case.get("locale", "ko")
    tally = Tally()
    cache_key = f"setting-check-eval:{case['id']}"
    started = time.monotonic()
    async with gate:
        found = await runner.find(reading, locale, tally, cache_key)
        verified = dedupe([kept for kept in map(reading.verify, found) if kept])
        judge_gate = asyncio.Semaphore(4)

        async def judge(candidate: Candidate) -> dict[str, Any]:
            async with judge_gate:
                value, usage = await model.judge(
                    JUDGE_INSTRUCTIONS,
                    judge_input(reading, candidate, [], locale),
                    "setting_check_judgement",
                    JUDGE_SCHEMA,
                    cache_key,
                )
            tally.add(usage)
            return {
                "candidate": candidate,
                "value": value,
                "kept": judged(candidate, value),
            }

        judgements = await asyncio.gather(*(judge(item) for item in verified))
    seconds = time.monotonic() - started

    def describe(candidate: Candidate) -> dict[str, Any]:
        source = keys.get(candidate.source_id, candidate.source_id)
        group, entry = classify(case, candidate.quote, source)
        return {
            "quote": candidate.quote,
            "source": source,
            "source_quote": candidate.source_quote,
            "core": candidate.core,
            "group": group,
            "entry": entry,
        }

    findings = []
    for item in judgements:
        kept = item["kept"]
        if kept is None:
            continue
        findings.append(
            {
                **describe(kept),
                "confidence": kept.confidence,
                "explanation": kept.explanation,
                "quote_focus": kept.quote_focus,
                "source_focus": kept.source_focus,
            }
        )
    return {
        "found": [describe(item) for item in found],
        "unverified": len(found) - len(verified),
        "judged": [
            {
                **describe(item["candidate"]),
                "verdict": item["value"].get("verdict"),
                "reason": item["value"].get("explanation"),
            }
            for item in judgements
        ],
        "findings": findings,
        "seconds": round(seconds, 1),
        "usage": asdict(tally.usage),
    }


def score(case: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    expected = {entry["id"]: 0 for entry in case["expect"]}
    traps = {entry["id"]: 0 for entry in case["avoid"]}
    candidates = {entry["id"]: 0 for entry in case["expect"]}
    extras: Counter[str] = Counter()
    sets = []
    for run in runs:
        hit = {item["entry"] for item in run["findings"] if item["group"] == "expect"}
        for key in hit:
            expected[key] += 1
        seen = {item["entry"] for item in run["found"] if item["group"] == "expect"}
        for key in seen:
            candidates[key] += 1
        for key in {
            item["entry"] for item in run["findings"] if item["group"] == "avoid"
        }:
            traps[key] += 1
        for item in run["findings"]:
            if item["group"] == "extra":
                extras[norm(item["quote"])] += 1
        sets.append(
            frozenset(item["entry"] or norm(item["quote"]) for item in run["findings"])
        )
    pairs = [(a, b) for i, a in enumerate(sets) for b in sets[i + 1 :]]
    agreement = (
        sum(len(a & b) / len(a | b) if a | b else 1.0 for a, b in pairs) / len(pairs)
        if pairs
        else 1.0
    )
    return {
        "expected": expected,
        "found_by_finder": candidates,
        "traps": traps,
        "extras": dict(extras),
        "agreement": round(agreement, 2),
    }


def summarize(
    cases: list[dict[str, Any]], results: dict[str, Any], runs: int
) -> dict[str, Any]:
    expected = sum(len(case["expect"]) for case in cases)
    traps = sum(len(case["avoid"]) for case in cases)
    hits = sum(sum(results[c["id"]]["score"]["expected"].values()) for c in cases)
    stable = sum(
        sum(1 for n in results[c["id"]]["score"]["expected"].values() if n == runs)
        for c in cases
    )
    trap_hits = sum(sum(results[c["id"]]["score"]["traps"].values()) for c in cases)
    findings = sum(
        len(run["findings"]) for c in cases for run in results[c["id"]]["runs"]
    )
    wanted = sum(
        sum(1 for item in run["findings"] if item["group"] in ("expect", "may"))
        for c in cases
        for run in results[c["id"]]["runs"]
    )
    extras = sum(
        sum(1 for item in run["findings"] if item["group"] == "extra")
        for c in cases
        for run in results[c["id"]]["runs"]
    )
    usage = Usage(0, 0, 0, 0, 0)
    seconds = []
    for c in cases:
        for run in results[c["id"]]["runs"]:
            usage = usage + Usage(**run["usage"])
            seconds.append(run["seconds"])
    return {
        "runs": runs,
        "recall": round(hits / (expected * runs), 3) if expected else None,
        "stable_recall": round(stable / expected, 3) if expected else None,
        "trap_rate": round(trap_hits / (traps * runs), 3) if traps else None,
        "precision": round(wanted / findings, 3) if findings else None,
        "extras_per_run": round(extras / runs, 2),
        "agreement": round(
            sum(results[c["id"]]["score"]["agreement"] for c in cases) / len(cases), 2
        ),
        "seconds_median": sorted(seconds)[len(seconds) // 2] if seconds else None,
        "usage": asdict(usage),
    }


def report(
    cases: list[dict[str, Any]], results: dict[str, Any], summary: dict[str, Any]
) -> str:
    runs = summary["runs"]
    lines = [
        "# Setting check evaluation",
        "",
        f"- Runs per case: {runs}",
        f"- Recall: {summary['recall']} (stable in every run: {summary['stable_recall']})",
        f"- Trap rate (reported something it must not): {summary['trap_rate']}",
        f"- Precision (findings that are expected or acceptable): {summary['precision']}",
        f"- Unlisted findings per run: {summary['extras_per_run']}",
        f"- Run-to-run agreement (Jaccard): {summary['agreement']}",
        f"- Median seconds per check: {summary['seconds_median']}",
        f"- Tokens: input {summary['usage']['input_tokens']}, cached {summary['usage']['cached_tokens']}, output {summary['usage']['output_tokens']}",
        "",
        "| Case | Expected (hits/runs) | Finder saw | Traps hit | Unlisted | Agreement |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for case in cases:
        s = results[case["id"]]["score"]
        exp = ", ".join(f"{k} {v}/{runs}" for k, v in s["expected"].items()) or "—"
        saw = (
            ", ".join(f"{k} {v}/{runs}" for k, v in s["found_by_finder"].items()) or "—"
        )
        trap = ", ".join(f"{k} {v}/{runs}" for k, v in s["traps"].items() if v) or "0"
        lines.append(
            f"| {case['id']} | {exp} | {saw} | {trap} | {sum(s['extras'].values())} | {s['agreement']} |"
        )
    lines += ["", "## Findings", ""]
    for case in cases:
        lines.append(f"### {case['id']} · {case['title']}")
        for number, run in enumerate(results[case["id"]]["runs"], start=1):
            dropped = [
                f"{item['entry'] or item['quote'][:30]}→{item['verdict']}"
                for item in run["judged"]
                if item["verdict"] != "CONFLICT"
            ]
            lines.append(
                f"- run {number}: {len(run['found'])} found, {run['unverified']} unverified, "
                f"{len(run['findings'])} kept, {run['seconds']}s"
                + (f"; judge dropped: {', '.join(dropped)}" if dropped else "")
            )
            for item in run["findings"]:
                tag = item["entry"] or "UNLISTED"
                lines.append(
                    f"  - [{item['group']}:{tag}] ({item['confidence']}) “{item['quote']}” ← {item['source']}: “{item['source_quote']}”"
                )
                lines.append(f"    {item['explanation']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def load_cases(args: argparse.Namespace) -> list[dict[str, Any]]:
    data = json.loads(Path(args.dataset).read_text())
    return [
        from_chat_dataset(case) if "from" in case else case
        for case in data["cases"]
        if not args.only or case["id"] in args.only
    ]


def write(
    out: Path,
    cases: list[dict[str, Any]],
    results: dict[str, Any],
    runs: int,
    extra: dict[str, Any],
) -> None:
    for case in cases:
        results[case["id"]]["score"] = score(case, results[case["id"]]["runs"])
    summary = {**summarize(cases, results, runs), **extra}
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    (out / "report.md").write_text(report(cases, results, summary))
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def rescore(args: argparse.Namespace) -> None:
    out = Path(args.rescore)
    cases = load_cases(args)
    results = json.loads((out / "results.json").read_text())
    extra = json.loads((out / "summary.json").read_text())
    for case in cases:
        for run in results[case["id"]]["runs"]:
            for group in ("found", "judged", "findings"):
                for item in run[group]:
                    item["group"], item["entry"] = classify(
                        case, item["quote"], item["source"]
                    )
    runs = len(results[cases[0]["id"]]["runs"])
    write(
        out,
        cases,
        results,
        runs,
        {k: extra[k] for k in ("model", "judge_model") if k in extra},
    )


async def evaluate(args: argparse.Namespace) -> None:
    settings = Settings.from_environment()
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is not set")
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    model = OpenAICheckModel(
        client,
        settings.openai_model,
        settings.setting_check_reasoning_effort,
        settings.openai_max_output_tokens,
        settings.judge,
        settings.setting_check_judge_reasoning_effort,
        settings.setting_check_judge_max_output_tokens,
        settings.setting_check_timeout_seconds,
    )
    runner = CheckRunner(NoStore(), None, model, settings)  # type: ignore[arg-type]
    cases = load_cases(args)
    gate = asyncio.Semaphore(args.concurrency)
    jobs = [(case, n) for case in cases for n in range(args.runs)]
    outcomes = await asyncio.gather(
        *(check_once(runner, model, case, gate) for case, _ in jobs)
    )
    results: dict[str, Any] = {case["id"]: {"runs": []} for case in cases}
    for (case, _), outcome in zip(jobs, outcomes, strict=True):
        results[case["id"]]["runs"].append(outcome)
    write(
        Path(args.out),
        cases,
        results,
        args.runs,
        {"model": settings.openai_model, "judge_model": settings.judge},
    )
    await client.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset", default=str(HERE / "datasets" / "setting-check.json")
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--only", nargs="*", default=[])
    parser.add_argument("--rescore", help="score an earlier run again with the dataset")
    parser.add_argument(
        "--out", default=str(HERE / "results" / "setting-check" / "latest")
    )
    args = parser.parse_args()
    if args.rescore:
        rescore(args)
    else:
        asyncio.run(evaluate(args))


if __name__ == "__main__":
    main()
