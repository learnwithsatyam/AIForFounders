"""retrieve.py — ask a question, see which chunks come back.

    python scripts/retrieve.py "how do I know if my product is just a wrapper?"
    python scripts/retrieve.py -k 5 "what is a moat?"
    python scripts/retrieve.py --full "how should I price an AI product?"

No web server, no LLM call, no answer generation. This shows you the raw
retrieval result, which is the only thing worth checking before any of that
exists: if the right passage is not in this list, no prompt can save it.
"""

from __future__ import annotations

import argparse
import textwrap

import psycopg
from google import genai

from embed_and_store import Settings, embed_batch, query_embed_text, to_vector_literal

# <=> is pgvector's cosine distance: 0 is identical, 2 is opposite. The index
# built in embed_and_store.py uses vector_cosine_ops, which is what makes this
# operator use the index rather than scanning.
SEARCH = """
SELECT id, chapter, section, kind, word_count, content,
       embedding <=> %s::vector AS distance
FROM chunks
WHERE embedding IS NOT NULL
ORDER BY distance
LIMIT %s;
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("question", help="what a reader would type")
    ap.add_argument("-k", type=int, default=8, help="how many chunks (default 8)")
    ap.add_argument("--full", action="store_true", help="print whole chunks")
    args = ap.parse_args()

    s = Settings()  # type: ignore[call-arg]
    client = genai.Client(api_key=s.gemini_api_key)

    # Same function the API will use, imported rather than retyped — the query
    # format has to match the format the chunks were indexed with.
    [vector] = embed_batch(client, [query_embed_text(args.question)], s)

    with psycopg.connect(s.database_url) as conn, conn.cursor() as cur:
        cur.execute(SEARCH, (to_vector_literal(vector), args.k))
        rows = cur.fetchall()

    print(f"\nQ: {args.question}\n")
    for rank, (cid, chapter, section, kind, words, content, dist) in enumerate(rows, 1):
        flag = "" if kind == "prose" else f"  [{kind}]"
        print(f"{rank}. #{cid}  dist {dist:.4f}  {words}w{flag}")
        print(f"   {chapter}")
        print(f"   > {section}")
        body = content if args.full else textwrap.shorten(content, 150, placeholder=" …")
        print(textwrap.indent(textwrap.fill(body, 92), "   "))
        print()


if __name__ == "__main__":
    main()
