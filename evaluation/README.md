# Chat evaluation

End-to-end checks of the AI chat against three generated stories with known
answers. The run goes through the real services: content stores the stories,
the summarizer builds the map's summaries from content's events, and the
questions go to the chat API over SSE.

| Dataset | Genre | Chapters | Characters | Traps |
| --- | --- | --- | --- | --- |
| `glass-garden` | Fantasy | 24 | ~47k | Setting says Lena fears the sea, chapter 8 sends her to sea; a memo plans to move her first awakening |
| `fog-harbor` | Mystery | 18 | ~35k | Setting says the captain never drinks tea, he dies from his teacup; a left-hand glove points at the left-handed mate; a memo plans a different culprit |
| `spring-table` | Family drama | 20 | ~41k | Setting says Teo worked 10 years, chapter 18 says 5; nicknames; a memo plans an ending that never happens |

Each dataset has 12 questions: simple facts, multi-hop chains, complex
questions (timelines, counts, cause and method), contradictions between a
setting and the manuscript, memos that must not be taken as facts, things the
story never shows, nicknames, follow-ups in the same session, and drag
questions (`explain`, `related`, `flow`).

## Files

- `outlines.py`: the stories. Every chapter lists facts that must appear word
  for word, and every dataset lists events the writer must never produce.
- `generate.py`: writes the chapters with the model and rejects a chapter that
  drops a fact. Its output, `datasets/*.json`, is committed so runs compare
  the same text.
- `run.py`: seeds the datasets, waits for summaries, asks the questions, grades
  the answers and writes `report.md`, `results.json` and `summary.json`.
- `results/`: committed reports of past runs.

## Running

```bash
# local services: content on 18001, ai-chat on 18000, summarizer running
OPENAI_API_KEY=... .venv/bin/python -m evaluation.run \
  --content http://localhost:18001 --chat http://localhost:18000 \
  --db "host=localhost port=55432 dbname=ai_chat user=local password=local" \
  --out evaluation/results/latest

# grade an earlier run again without asking
OPENAI_API_KEY=... .venv/bin/python -m evaluation.run --content x --chat x \
  --regrade evaluation/results/latest
```

`--db` lets the run wait until every document is summarized and adds per-turn
metrics. Without it the run waits `--settle` seconds (default 900, longer than
the summarizer's idle time). Projects and chat data are deleted at the end
unless `--keep` is given.

## Grading

- **Verdict** (`correct`, `partial`, `wrong`): a grader model compares the
  answer with the expected points and forbidden claims, given the full text of
  the expected sources and of every document the answer cited. Each answer is
  graded three times and the majority wins (a tie goes to the worse verdict).
  One grading pass varies by about ±3 questions out of 36.
- **Score**: (correct + 0.5 × partial) / questions.
- **Source recall**: the share of expected source documents the answer cited.
- **False `NOT_ENOUGH`**: answerable questions that ended with "could not find a
  certain answer".

# Setting check evaluation

`setting_check.py` runs the setting error check on known cases without the
services or a database: the same finding passes, quote verification, merging
and judgement as `CheckRunner.check`, with the model settings from the
environment (`OPENAI_MODEL`, `SETTING_CHECK_*`).

`datasets/setting-check.json` has 14 cases: the sample project's chapter 7 with
planted contradictions, ten short cases (a magic rule, numbers across chapters,
a dead man in a flashback and a dream, a nickname, a place, appearance, a rule
with its exception, a contradiction the chapter explains and one it does not,
an English case) and three chapters of the chat datasets with their planted
contradiction. Each case lists:

- `expect`: contradictions the check must report, by a phrase of the chapter
  sentence and the documents that may be cited as the source.
- `avoid`: sentences it must not report: time passing, a story change, a lie, a
  dream, a flashback, a rumour, a simile, a detail the settings never mention.
- `may`: contradictions that are fine either way.

```bash
OPENAI_API_KEY=... .venv/bin/python -m evaluation.setting_check --runs 3 \
  --out evaluation/results/setting-check/latest
# one case
OPENAI_API_KEY=... .venv/bin/python -m evaluation.setting_check --only dead-man
```

Three held-out sets check that a change is not tuned to these cases:
`setting-check-holdout.json` (8 cases), `-holdout-2.json` (6) and
`-holdout-3.json` (5), each written before the change it tested. The first two
were read while changing the prompts, so only the newest one is a clean test.

```bash
OPENAI_API_KEY=... .venv/bin/python -m evaluation.setting_check \
  --dataset evaluation/datasets/setting-check-holdout-3.json --out /tmp/holdout-3
# score an earlier run again after editing the dataset
.venv/bin/python -m evaluation.setting_check --rescore evaluation/results/setting-check/dev-before
```

`report.md` gives recall (expected contradictions reported, averaged over runs),
stable recall (reported in every run), the trap rate (`avoid` sentences
reported), precision (findings that are expected or acceptable), unlisted
findings to review by hand, and run-to-run agreement. For each run it shows what
the finder proposed, what the judge dropped and why, and every finding with its
explanation, so a miss can be traced to the finder or the judge.

## Results (2026-10-31, `gpt-6-luna`, 3 runs per case)

| Set | Before (`dev-before`, …) | After | Traps hit | Precision |
| --- | --- | --- | --- | --- |
| Development, 14 cases | 0.77 recall, 0.56 in every run | 0.94, 0.81 | 0 / 0 | 1.0 / 1.0 |
| Holdout 2, 6 cases | 0.75, 0.75 | 0.83, 0.75 | 0 / 0 | 1.0 / 1.0 |
| Holdout 3, 5 cases (clean) | 0.89, 0.67 | 1.00, 1.00 | 0 / 0 | 1.0 / 1.0 |

What moved recall: the judge read only the source document and dismissed
contradictions with explanations the story never gives (a ghost, a second key)
or with a flashback that had ended; it now reads every setting document, the
most recent earlier chapters (up to 30,000 characters) and the older chapters
the finder names, and keeps what the materials do not account for. Two places
that broke the same setting sentence were merged into one finding; now only the
same chapter place is merged. A settings pass, one document at a time, replaced
the core-facts pass and keeps the finder from skimming long projects. A judge
with medium reasoning did not do better than low. Input tokens per check rose
from about 6.8k to 13k (77% cached) and output from about 0.7k to 1.1k.

Misses that remain: a character acting against a setting is sometimes judged a
change over time (Lena at sea, 1 in 3 runs), and a contradiction that needs an
older chapter the finder does not name. The teacup case is fine either way.
