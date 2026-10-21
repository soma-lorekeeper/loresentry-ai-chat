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
