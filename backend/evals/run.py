"""run.py — measure the RAG pipeline against a golden set.

    python evals/run.py                     # retrieval only (cheap, default)
    python evals/run.py --suite condense
    python evals/run.py --suite answers     # generates + judges; costs tokens
    python evals/run.py --suite all
    python evals/run.py -k 5                # sweep k without touching .env
    python evals/run.py --json out.json     # machine-readable, for tracking drift

Runs against `Engine` directly rather than over HTTP: no rate limiter in the
way, no server to start, and retrieval can be measured on its own — which is
the point, because if the right passage is not retrieved, no prompt fixes it.

Exit code is 1 if any suite falls below its threshold in dataset.yaml, so this
can gate a deploy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVALS_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

try:
    import yaml
except ImportError:
    sys.exit("evals need PyYAML:  pip install pyyaml")

from google.genai import types  # noqa: E402

from app.config import settings  # noqa: E402
from app.rag import Engine, Hit, citations  # noqa: E402

DATASET = EVALS_DIR / "dataset.yaml"
GOLDEN = EVALS_DIR / "golden.json"


def chapter_key(name: str) -> str:
    """'Chapter 3: Is My Product Just a Wrapper?' -> 'Chapter 3'.

    Matching on this rather than a prefix is deliberate: `startswith("Chapter
    1")` would also match Chapters 10, 11 and 12 and quietly inflate the score.
    """
    return name.split(":")[0].strip()


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------

@dataclass
class Case:
    question: str
    passed: bool
    detail: str = ""
    rank: int | None = None       # retrieval: rank of the first correct chapter


@dataclass
class Suite:
    name: str
    cases: list[Case] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    checks: dict[str, tuple[float, float]] = field(default_factory=dict)  # name -> (got, want)

    @property
    def ok(self) -> bool:
        return all(got >= want for got, want in self.checks.values())


# --------------------------------------------------------------------------
# suites
# --------------------------------------------------------------------------

async def run_retrieval(engine: Engine, cases: list[dict], want: dict) -> Suite:
    suite = Suite("retrieval")
    ranks: list[int | None] = []

    for c in cases:
        expected = {e.strip() for e in c["expect"]}
        hits: list[Hit] = await engine.retrieve(c["question"])
        got = [chapter_key(h.chapter) for h in hits]

        rank = next((i + 1 for i, ch in enumerate(got) if ch in expected), None)
        ranks.append(rank)

        if rank == 1:
            detail = f"rank 1 · {got[0]}"
        elif rank:
            detail = f"rank {rank} · top was {got[0]}"
        else:
            detail = f"MISS · wanted {'/'.join(sorted(expected))}, top was {got[0] if got else '(nothing)'}"

        suite.cases.append(Case(c["question"], rank is not None, detail, rank))

    n = len(ranks) or 1
    hit_k = sum(1 for r in ranks if r) / n
    hit_1 = sum(1 for r in ranks if r == 1) / n
    mrr = sum(1 / r for r in ranks if r) / n
    found = [r for r in ranks if r]

    suite.metrics = {
        "hit@k": hit_k,
        "hit@1": hit_1,
        "mrr": mrr,
        "mean_rank_when_found": sum(found) / len(found) if found else 0.0,
    }
    suite.checks = {
        "hit@k": (hit_k, want["retrieval_hit_at_k"]),
        "hit@1": (hit_1, want["retrieval_hit_at_1"]),
        "mrr": (mrr, want["retrieval_mrr"]),
    }
    return suite


async def run_condense(engine: Engine, cases: list[dict], want: dict) -> Suite:
    suite = Suite("condense")

    for c in cases:
        messages = [*c["history"], {"role": "user", "content": c["question"]}]
        standalone = await engine.condense(messages)
        low = standalone.lower()
        ok = any(kw.lower() in low for kw in c["expect_any"])
        suite.cases.append(
            Case(c["question"], ok, f"-> {standalone.strip()[:88]}")
        )

    rate = sum(1 for x in suite.cases if x.passed) / (len(suite.cases) or 1)
    suite.metrics = {"pass_rate": rate}
    suite.checks = {"pass_rate": (rate, want["condense_pass"])}
    return suite


GROUNDED_JUDGE = """You are grading an answer for a book Q&A system.

The answer must rest ENTIRELY on the excerpts below. Using correct outside \
knowledge is a FAILURE — the whole product depends on answers coming from this \
book and nowhere else. Saying the excerpts do not cover something is a PASS.

EXCERPTS:
{excerpts}

ANSWER:
{answer}

Reply with only JSON: {{"pass": true or false, "why": "one short sentence"}}"""

REFUSE_JUDGE = """You are grading a book Q&A system that must only discuss the \
book "AI for Founders" and a reader's situation as a founder.

It was asked an off-topic question and should have declined, or said the topic \
is outside what it can help with. Actually answering the question is a FAILURE.

QUESTION: {question}

ANSWER:
{answer}

Reply with only JSON: {{"pass": true or false, "why": "one short sentence"}}"""


async def judge(engine: Engine, prompt: str) -> tuple[bool, str]:
    resp = await engine.client.aio.models.generate_content(
        model=settings.condense_model,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=200),
    )
    raw = (resp.text or "").strip()
    if raw.startswith("```"):  # models like fencing JSON no matter what you ask
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        v = json.loads(raw)
        return bool(v.get("pass")), str(v.get("why", ""))[:90]
    except (json.JSONDecodeError, AttributeError):
        return False, f"judge returned unparseable output: {raw[:60]!r}"


async def run_answers(engine: Engine, cases: list[dict], want: dict) -> Suite:
    suite = Suite("answers")

    for c in cases:
        messages = [{"role": "user", "content": c["question"]}]
        hits = await engine.retrieve(c["question"])
        answer = "".join([chunk async for chunk in engine.answer(hits, messages)])

        if c["mode"] == "refuse":
            prompt = REFUSE_JUDGE.format(question=c["question"], answer=answer)
        else:
            excerpts = "\n\n".join(
                f"--- {h.chapter} > {h.section} ---\n{h.content}" for h in hits
            )
            prompt = GROUNDED_JUDGE.format(excerpts=excerpts, answer=answer)

        ok, why = await judge(engine, prompt)
        suite.cases.append(Case(f"[{c['mode']}] {c['question']}", ok, why))

    rate = sum(1 for x in suite.cases if x.passed) / (len(suite.cases) or 1)
    suite.metrics = {"pass_rate": rate}
    suite.checks = {"pass_rate": (rate, want["answer_pass"])}
    return suite


# --------------------------------------------------------------------------
# golden snapshots
#
# The threshold suites answer "is retrieval good?". These answer "did anything
# change?" — which is a different and quieter failure. Re-chunking the book,
# re-running the loader, tuning recap_penalty, or Google silently revising the
# embedding model can all shift what comes back without moving hit@k at all.
# Committing the exact top-k makes that visible in a diff.
# --------------------------------------------------------------------------

DRIFT_EPS = 0.02  # distances are deterministic; movement means the model moved


def snapshot(hits: list[Hit]) -> dict:
    return {
        "chunks": [
            {"id": h.id, "chapter": h.chapter, "section": h.section,
             "distance": round(h.distance, 4)}
            for h in hits
        ],
        # What the reader actually sees under the answer, so a change to the
        # dedupe or ordering in citations() shows up here too.
        "citations": citations(hits),
    }


async def build_golden(engine: Engine, cases: list[dict], k: int) -> dict:
    out: dict = {
        "k": k,
        "embed_model": settings.embed_model,
        "embed_dim": settings.embed_dim,
        "recap_penalty": settings.recap_penalty,
        "cases": {},
    }
    for c in cases:
        out["cases"][c["question"]] = snapshot(await engine.retrieve(c["question"]))
    return out


async def run_golden(engine: Engine, cases: list[dict], want: dict, k: int) -> Suite:
    suite = Suite("golden")
    if not GOLDEN.exists():
        sys.exit(f"no baseline at {GOLDEN}\nrecord one:  python evals/run.py --update-golden")

    ref = json.loads(GOLDEN.read_text())
    for key, now in (("k", k), ("embed_model", settings.embed_model),
                     ("recap_penalty", settings.recap_penalty)):
        if ref.get(key) != now:
            print(f"  ! baseline recorded with {key}={ref.get(key)!r}, running {now!r}"
                  f" — differences below are expected")

    drifts: list[float] = []

    for c in cases:
        q = c["question"]
        want_case = ref["cases"].get(q)
        if want_case is None:
            suite.cases.append(Case(q, False, "not in baseline — re-record with --update-golden"))
            continue

        got = snapshot(await engine.retrieve(q))
        want_ids = [x["id"] for x in want_case["chunks"]]
        got_ids = [x["id"] for x in got["chunks"]]

        if got_ids == want_ids:
            # Same chunks in the same order. Distances still worth watching:
            # if they move, the embedding model changed under the index.
            deltas = [
                abs(a["distance"] - b["distance"])
                for a, b in zip(got["chunks"], want_case["chunks"])
            ]
            worst = max(deltas, default=0.0)
            drifts.append(worst)
            if worst > DRIFT_EPS:
                suite.cases.append(Case(q, False, f"same chunks but distances moved {worst:.4f}"))
            else:
                suite.cases.append(Case(q, True, "identical"))
            continue

        if set(got_ids) == set(want_ids):
            suite.cases.append(Case(q, False, f"reordered: {want_ids} -> {got_ids}"))
            continue

        gone = [i for i in want_ids if i not in got_ids]
        new = [i for i in got_ids if i not in want_ids]
        suite.cases.append(Case(q, False, f"membership changed: -{gone} +{new}"))

    rate = sum(1 for x in suite.cases if x.passed) / (len(suite.cases) or 1)
    suite.metrics = {"identical_rate": rate, "max_distance_drift": max(drifts, default=0.0)}
    suite.checks = {"identical_rate": (rate, want["golden_identical"])}
    return suite


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def report(suites: list[Suite], k: int, elapsed: float) -> bool:
    for s in suites:
        failures = [c for c in s.cases if not c.passed]
        print(f"\n{'=' * 74}")
        print(f"{s.name.upper()}   {len(s.cases) - len(failures)}/{len(s.cases)} passed")
        print("=" * 74)

        for c in failures:
            print(f"  ✗ {c.question}")
            print(f"      {c.detail}")
        if not failures:
            print("  all cases passed")

        print()
        for key, val in s.metrics.items():
            bar = ""
            if key in s.checks:
                got, want = s.checks[key]
                bar = f"   (threshold {want:.2f}) {'OK' if got >= want else 'BELOW'}"
            shown = f"{val:.3f}" if val < 10 else f"{val:.2f}"
            print(f"  {key:<22} {shown}{bar}")

    passed = all(s.ok for s in suites)
    print(f"\n{'=' * 74}")
    print(f"k={k}   {elapsed:.1f}s   {'PASS' if passed else 'FAIL — below threshold'}")
    print("=" * 74)
    return passed


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--suite", default="retrieval",
        choices=["retrieval", "golden", "condense", "answers", "all"],
        help="'answers' generates and judges, so it costs tokens (default: retrieval)",
    )
    ap.add_argument("-k", type=int, default=settings.top_k, help="chunks to retrieve")
    ap.add_argument("--json", type=Path, help="write results here for tracking drift")
    ap.add_argument(
        "--update-golden", action="store_true",
        help="re-record the snapshot baseline. Do this deliberately, and read the diff.",
    )
    args = ap.parse_args()

    data = yaml.safe_load(DATASET.read_text())
    want = data["thresholds"]

    engine = Engine(settings)
    # Sweep k from the command line without editing .env and redeploying.
    engine.s.top_k = args.k

    started = time.time()
    await engine.start()
    try:
        if args.update_golden:
            golden = await build_golden(engine, data["retrieval"], args.k)
            GOLDEN.write_text(json.dumps(golden, indent=2) + "\n")
            print(f"recorded {len(golden['cases'])} snapshots at k={args.k} -> {GOLDEN}")
            print("commit this, and review the diff whenever it changes.")
            return 0

        suites = []
        if args.suite in ("retrieval", "all"):
            suites.append(await run_retrieval(engine, data["retrieval"], want))
        if args.suite in ("golden", "all"):
            suites.append(await run_golden(engine, data["retrieval"], want, args.k))
        if args.suite in ("condense", "all"):
            suites.append(await run_condense(engine, data["condense"], want))
        if args.suite in ("answers", "all"):
            suites.append(await run_answers(engine, data["answers"], want))
    finally:
        await engine.stop()

    elapsed = time.time() - started
    passed = report(suites, args.k, elapsed)

    if args.json:
        args.json.write_text(json.dumps({
            "k": args.k,
            "seconds": round(elapsed, 1),
            "passed": passed,
            "suites": [
                {
                    "name": s.name,
                    "metrics": s.metrics,
                    "failures": [
                        {"question": c.question, "detail": c.detail}
                        for c in s.cases if not c.passed
                    ],
                }
                for s in suites
            ],
        }, indent=2))
        print(f"wrote {args.json}")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
