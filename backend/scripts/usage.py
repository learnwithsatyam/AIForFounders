"""usage.py — what readers are actually doing with the book.

    python scripts/usage.py                 # last 7 days
    python scripts/usage.py --days 30
    python scripts/usage.py --questions 40  # the raw questions, newest first
    python scripts/usage.py --misses        # questions the book did not cover

A script rather than an API endpoint on purpose: usage data should not be one
forgotten auth check away from being public, and you already have psql-level
access to Neon. Nothing here writes.

The most valuable output is not the counts. It is `--questions` and `--misses`:
what readers ask, in their words, and what they asked that the book could not
answer. That is a table of contents for whatever you write next.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import psycopg

# The app's Settings, not the loader's: the loader knows nothing about pricing,
# so importing it here would silently report every configured price as zero.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings  # noqa: E402


def bar(n: int, top: int, width: int = 28) -> str:
    return "█" * max(1, round(width * n / top)) if n and top else ""


def summary(cur, days: int) -> None:
    cur.execute(
        """
        SELECT count(*), count(DISTINCT ip_hash),
               count(*) FILTER (WHERE outcome = 'ok'),
               count(*) FILTER (WHERE outcome = 'no_hits'),
               count(*) FILTER (WHERE outcome = 'disconnected'),
               count(*) FILTER (WHERE outcome = 'rate_limited'),
               count(*) FILTER (WHERE outcome = 'error'),
               count(*) FILTER (WHERE turn > 1)
        FROM usage WHERE ts > now() - make_interval(days => %s);
        """,
        (days,),
    )
    total, people, ok, misses, stopped, limited, errors, followups = cur.fetchone()

    print(f"\n{'=' * 62}")
    print(f"  LAST {days} DAYS")
    print("=" * 62)
    if not total:
        print("\n  no questions recorded yet\n")
        return

    print(f"\n  {total} question{'' if total == 1 else 's'} from {people}"
          f" {'person' if people == 1 else 'people'}"
          f"   ({total / days:.1f}/day, {total / max(people, 1):.1f} each)")
    print(f"  {followups} {'was a' if followups == 1 else 'were'} follow-up"
          f"{'' if followups == 1 else 's'}"
          f" ({followups / total:.0%} of questions came in a conversation)")

    print("\n  outcomes")
    for label, n in [("answered", ok), ("not in the book", misses),
                     ("stopped by reader", stopped), ("rate limited", limited),
                     ("errored", errors)]:
        if n:
            print(f"    {label:<20} {n:>5}  {n / total:>5.0%}  {bar(n, total, 20)}")

    cur.execute(
        """
        SELECT percentile_disc(0.5)  WITHIN GROUP (ORDER BY ttft_ms),
               percentile_disc(0.95) WITHIN GROUP (ORDER BY ttft_ms),
               percentile_disc(0.5)  WITHIN GROUP (ORDER BY total_ms),
               avg(answer_chars)::int
        FROM usage
        WHERE ts > now() - make_interval(days => %s)
          -- Both percentiles must cover the same rows. Questions abandoned
          -- before the first token have total_ms but no ttft_ms, and including
          -- them made the "full answer" median come out below the TTFT median.
          AND ttft_ms IS NOT NULL;
        """,
        (days,),
    )
    ttft50, ttft95, total50, chars = cur.fetchone()
    if ttft50:
        print("\n  speed")
        print(f"    time to first token   p50 {ttft50 / 1000:.1f}s   p95 {ttft95 / 1000:.1f}s")
        print(f"    full answer           p50 {total50 / 1000:.1f}s   ~{chars} chars")

        # Where that wait actually goes — the reason to record the split.
        cur.execute(
            """SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY condense_ms),
                      percentile_disc(0.5) WITHIN GROUP (ORDER BY retrieve_ms)
               FROM usage WHERE ts > now() - make_interval(days => %s)
                 AND ttft_ms IS NOT NULL;""",
            (days,),
        )
        cond, retr = cur.fetchone()
        if retr is not None:
            cond = cond or 0
            gem = max(0, ttft50 - cond - retr)
            print(f"      rewriting question  {cond / 1000:5.1f}s")
            print(f"      vector search       {retr / 1000:5.1f}s")
            print(f"      gemini writing      {gem / 1000:5.1f}s  ({gem / ttft50:.0%} of the wait)")

    cur.execute(
        """SELECT coalesce(sum(prompt_tokens), 0), coalesce(sum(output_tokens), 0),
                  count(*) FILTER (WHERE prompt_tokens IS NOT NULL)
           FROM usage WHERE ts > now() - make_interval(days => %s);""",
        (days,),
    )
    tok_in, tok_out, priced = cur.fetchone()
    if priced:
        s = Settings()  # type: ignore[call-arg]
        pin, pout = s.price_in_per_mtok, s.price_out_per_mtok
        print("\n  tokens")
        print(f"    prompt  {tok_in:>9,}   ({tok_in // max(priced, 1):,} per question)")
        print(f"    output  {tok_out:>9,}")
        if pin or pout:
            cost = tok_in / 1e6 * pin + tok_out / 1e6 * pout
            print(f"    cost    ${cost:>8.2f}   (${cost / max(priced, 1):.4f} per question)")
        else:
            print("    set PRICE_IN_PER_MTOK / PRICE_OUT_PER_MTOK to see cost")

    cur.execute(
        """
        SELECT ch, count(*) FROM usage, unnest(chapters) AS ch
        WHERE ts > now() - make_interval(days => %s)
        GROUP BY ch ORDER BY count(*) DESC LIMIT 12;
        """,
        (days,),
    )
    rows = cur.fetchall()
    if rows:
        top = rows[0][1]
        print("\n  chapters readers land on")
        for ch, n in rows:
            print(f"    {ch[:44]:<44} {n:>4}  {bar(n, top)}")

    cur.execute(
        """
        SELECT to_char(date_trunc('day', ts), 'Mon DD'), count(*)
        FROM usage WHERE ts > now() - make_interval(days => %s)
        GROUP BY 1, date_trunc('day', ts) ORDER BY date_trunc('day', ts);
        """,
        (days,),
    )
    rows = cur.fetchall()
    if len(rows) > 1:
        top = max(n for _, n in rows)
        print("\n  by day")
        for day, n in rows:
            print(f"    {day}  {n:>4}  {bar(n, top)}")
    print()


def questions(cur, days: int, limit: int) -> None:
    cur.execute(
        """
        SELECT to_char(ts, 'Mon DD HH24:MI'), question, outcome, turn
        FROM usage WHERE ts > now() - make_interval(days => %s)
        ORDER BY ts DESC LIMIT %s;
        """,
        (days, limit),
    )
    rows = cur.fetchall()
    print(f"\n  {len(rows)} most recent questions\n")
    for ts, q, outcome, turn in rows:
        flag = "" if outcome == "ok" else f"  [{outcome}]"
        depth = f"  (turn {turn // 2 + 1})" if turn > 1 else ""
        print(f"  {ts}  {q[:82]}{flag}{depth}")
    print()


def misses(cur, days: int) -> None:
    """Questions the book could not answer, and near-misses.

    A high top_distance means nothing in the book was really close, even though
    something was returned — those are the interesting gaps, not just the
    outright no_hits rows."""
    cur.execute(
        """
        SELECT to_char(ts, 'Mon DD'), question, outcome, top_distance
        FROM usage
        WHERE ts > now() - make_interval(days => %s)
          AND (outcome = 'no_hits' OR top_distance > 0.45)
        ORDER BY top_distance DESC NULLS FIRST LIMIT 40;
        """,
        (days,),
    )
    rows = cur.fetchall()
    print(f"\n  {len(rows)} questions the book struggled with\n")
    if not rows:
        print("  none — every question found something close\n")
        return
    for ts, q, outcome, dist in rows:
        d = "no hits at all" if dist is None else f"best distance {dist:.3f}"
        print(f"  {ts}  {q[:74]}")
        print(f"          {d}  [{outcome}]")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--questions", type=int, nargs="?", const=30, metavar="N",
                    help="list the raw questions instead of the summary")
    ap.add_argument("--misses", action="store_true",
                    help="questions the book could not answer well")
    args = ap.parse_args()

    s = Settings()  # type: ignore[call-arg]
    with psycopg.connect(s.database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('usage');")
        if cur.fetchone()[0] is None:
            sys.exit("no usage table yet — start the API once and ask a question")

        if args.questions:
            questions(cur, args.days, args.questions)
        elif args.misses:
            misses(cur, args.days)
        else:
            summary(cur, args.days)


if __name__ == "__main__":
    main()
