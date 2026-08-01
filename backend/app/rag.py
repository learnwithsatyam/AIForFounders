"""Retrieval and answer generation.

The whole RAG path lives here so main.py stays a transport layer. Nothing in
this module knows about HTTP.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from google import genai
from google.genai import types
from psycopg_pool import AsyncConnectionPool

from .config import Settings, query_embed_text, to_vector_literal

# Recap chunks get a small additive distance penalty. Enough to break a tie
# against real prose, not enough to exclude them — "summarise chapter 4" should
# still find the recap.
SEARCH = """
SELECT id, chapter, section, kind, content,
       embedding <=> %s::vector AS distance
FROM chunks
WHERE embedding IS NOT NULL
ORDER BY (embedding <=> %s::vector)
         + CASE WHEN kind = 'recap' THEN %s ELSE 0 END
LIMIT %s;
"""

CHAPTERS = """
SELECT DISTINCT chapter FROM chunks
WHERE kind = 'prose' ORDER BY chapter;
"""

SYSTEM = """You are the assistant for the book "AI for Founders" by Satyam.

You answer strictly from the excerpts supplied with each question. The excerpts \
are the book's actual text.

Rules:
- Answer only from the excerpts. If they do not cover the question, say so \
plainly and suggest what the book does cover instead. Never fill a gap from \
general knowledge and never present outside knowledge as the book's position.
- Write as the book does: direct, concrete, second person, no corporate hedging.
- Refer to chapters by name when it helps the reader navigate ("Chapter 9 works \
through the token maths"). Do not invent chapter names — use only those listed \
below or shown in the excerpts.
- Prefer the book's own examples and numbers over generic ones.
- Be concise. Two or three short paragraphs is usually right. Use markdown.
- If the question is not about the book, its subject matter, or the reader's \
situation as a founder, say that is outside what you can help with.

The book contains these chapters:
{chapters}
"""

CONDENSE = """Rewrite the final question as a standalone question that makes \
sense with no conversation history. Resolve pronouns and references using the \
history. Keep it short. Output only the rewritten question, nothing else.

Conversation:
{history}

Final question: {question}

Standalone question:"""


@dataclass(frozen=True)
class Hit:
    id: int
    chapter: str
    section: str
    kind: str
    content: str
    distance: float


class Engine:
    """Holds the connection pool, the Gemini client and the chapter list."""

    def __init__(self, s: Settings) -> None:
        self.s = s
        self.client = genai.Client(api_key=s.gemini_api_key)
        self.pool = AsyncConnectionPool(s.database_url, min_size=1, max_size=4, open=False)
        self.chapters: list[str] = []

    async def start(self) -> None:
        await self.pool.open(wait=True)
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(CHAPTERS)
            self.chapters = [r[0] for r in await cur.fetchall()]

    async def stop(self) -> None:
        await self.pool.close()

    # --- retrieval ---------------------------------------------------------

    async def embed_query(self, question: str) -> list[float]:
        resp = await self.client.aio.models.embed_content(
            model=self.s.embed_model,
            contents=[
                types.Content(
                    parts=[types.Part.from_text(text=query_embed_text(question))]
                )
            ],
            config=types.EmbedContentConfig(output_dimensionality=self.s.embed_dim),
        )
        vectors = [list(e.values) for e in (resp.embeddings or [])]
        if len(vectors) != 1:
            raise RuntimeError(f"expected 1 query embedding, got {len(vectors)}")
        return vectors[0]

    async def retrieve(self, question: str) -> list[Hit]:
        vec = to_vector_literal(await self.embed_query(question))
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                SEARCH, (vec, vec, self.s.recap_penalty, self.s.top_k)
            )
            rows = await cur.fetchall()
        return [Hit(*r) for r in rows]

    # --- generation --------------------------------------------------------

    async def condense(self, messages: list[dict[str, str]]) -> str:
        """Turn a follow-up into a standalone question.

        "what about moats?" then "how long do they last?" — the second question
        embedded alone retrieves nothing useful, because standalone it is about
        nothing. One cheap call fixes that.
        """
        question = messages[-1]["content"]
        history = messages[: -1][-self.s.history_turns :]
        if not history:
            return question

        transcript = "\n".join(
            f"{m['role'].capitalize()}: {m['content']}" for m in history
        )
        resp = await self.client.aio.models.generate_content(
            model=self.s.condense_model,
            contents=CONDENSE.format(history=transcript, question=question),
            config=types.GenerateContentConfig(
                temperature=0.0, max_output_tokens=120
            ),
        )
        rewritten = (resp.text or "").strip()
        return rewritten or question

    def _prompt(self, hits: list[Hit], messages: list[dict[str, str]]) -> str:
        excerpts = "\n\n".join(
            f"--- Excerpt {i} | {h.chapter} > {h.section} ---\n{h.content}"
            for i, h in enumerate(hits, start=1)
        )
        history = messages[:-1][-self.s.history_turns :]
        prior = (
            "\n".join(f"{m['role'].capitalize()}: {m['content']}" for m in history)
            if history
            else ""
        )
        parts = []
        if prior:
            parts.append(f"Conversation so far:\n{prior}\n")
        parts.append(f"Excerpts from the book:\n\n{excerpts}\n")
        parts.append(f"Reader's question: {messages[-1]['content']}")
        return "\n".join(parts)

    async def answer(
        self, hits: list[Hit], messages: list[dict[str, str]]
    ) -> AsyncIterator[str]:
        system = SYSTEM.format(
            chapters="\n".join(f"- {c}" for c in self.chapters) or "- (unavailable)"
        )
        stream = await self.client.aio.models.generate_content_stream(
            model=self.s.chat_model,
            contents=self._prompt(hits, messages),
            config=types.GenerateContentConfig(
                system_instruction=system, temperature=0.3
            ),
        )
        async for chunk in stream:
            if chunk.text:
                yield chunk.text


def citations(hits: list[Hit], limit: int = 4) -> list[dict[str, str]]:
    """Unique chapter/section pairs, best match first, for the chips under the
    answer. Deduped because eight hits are often four sections."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for h in hits:
        key = (h.chapter, h.section)
        if key in seen:
            continue
        seen.add(key)
        out.append({"chapter": h.chapter, "section": h.section})
        if len(out) >= limit:
            break
    return out
