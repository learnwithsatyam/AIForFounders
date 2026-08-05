# Evals

Four suites over the RAG pipeline. They run against `Engine` directly rather
than over HTTP — no server to start, no rate limiter in the way, and retrieval
can be measured on its own, which is the point: **if the right passage is not
retrieved, no prompt fixes it.**

```bash
cd backend
python evals/run.py                    # retrieval only — cheap, the default
python evals/run.py --suite golden     # did anything change?
python evals/run.py --suite condense
python evals/run.py --suite answers    # generates + judges; costs tokens
python evals/run.py --suite all
```

Exit code is 1 if any suite is below its threshold, so this can gate a deploy.

## The suites

| Suite | Asks | Cost per run |
|---|---|---|
| `retrieval` | Does the right chapter come back for a real reader's question? | 34 embedding calls |
| `golden` | Did the exact top-k change since the last baseline? | 34 embedding calls |
| `condense` | Does a follow-up survive being rewritten standalone? | 4 cheap LLM calls |
| `answers` | Is the answer grounded in the excerpts, and are off-topic questions declined? | 9 generations + 9 judge calls |

**retrieval** is the one to run constantly. Questions are written the way a
founder would type them, not in the book's vocabulary, because that gap is what
retrieval has to close — a case that quotes a chapter heading back at the index
proves nothing. Some questions are genuinely covered in two chapters and list
both; that is honest, not lenient.

**golden** is a different question from "is it good?". It is "did anything
change?" Re-chunking the book, re-running the loader, tuning `recap_penalty`,
or Google silently revising the embedding model can all shift results without
moving `hit@k` at all. `golden.json` pins the exact chunk ids, order, distances
and the citation chips a reader sees, so drift shows up in a diff.

Distances are deterministic — a clean run reports `max_distance_drift 0.000`.
If that number ever moves while the chunk ids stay the same, the embedding
model changed underneath your index, and everything needs re-embedding.

**answers** uses `condense_model` as an LLM judge. Groundedness is the failure
that matters most: the model answering from general knowledge instead of the
book is exactly what would quietly make the product untrustworthy.

## Baseline (2026-08-02, k=8)

| Metric | Value |
|---|---|
| `hit@k` | **1.000** (34/34) |
| `hit@1` | 0.824 |
| `mrr` | 0.912 |
| mean rank when found | 1.18 |
| condense pass | 1.000 (4/4) |
| answers pass | 1.000 (9/9) |
| golden identical | 1.000, drift 0.000 |

Thresholds in `dataset.yaml` sit just under these.

## Adding cases

Edit `dataset.yaml`. Chapters are matched on the part before the colon, so
`Chapter 1` can never accidentally match `Chapter 10` — write `Chapter 3`, not
the full heading.

After adding retrieval cases, re-record the baseline:

```bash
python evals/run.py --update-golden
```

Do that **deliberately**, and read the diff before committing. Re-recording to
make a failure go away is how you lose the only thing this suite is for.

## Sweeping k

`-k` overrides `top_k` without touching `.env`:

```bash
for k in 3 4 5 6 8; do python evals/run.py -k $k; done
```

At the time of writing, metrics are identical from k=3 to k=8 — every correct
chapter is found within the top 3. That is a live cost question, since `top_k`
is how many excerpts get stuffed into every prompt. Before dropping it, run
`--suite answers` at the lower k: retrieval finding the right chapter and the
model having enough material to answer well are different things, and only the
second one is worth paying for.

## Requirements

PyYAML, plus the normal backend deps and a working `.env` (`DATABASE_URL`,
`GEMINI_API_KEY`). The suites read the same settings the API does, so they
measure what you actually deploy.
