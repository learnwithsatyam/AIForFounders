"""A ragjudge Judge backed by Gemini, plus a refusal metric.

ragjudge ships OpenAI and Anthropic judges. This app runs Gemini, and its
`Judge` is a runtime_checkable Protocol — anything with the right `judge`
method qualifies — so the evals can be judged by the same provider the app
already has credentials for, rather than adding a second vendor and a second
API key just to grade answers.

Gemini's `response_schema` accepts only a subset of JSON Schema (it rejects
`additionalProperties`, which ragjudge's schemas set), so this asks for JSON
mode and carries the schema in the prompt instead. That is provider-agnostic
and avoids translating between two schema dialects.
"""

from __future__ import annotations

import json
from typing import Any

from google.genai import types
from ragjudge import JudgeResponse, Sample, Score

SYSTEM = (
    "You are a strict evaluator. Reply with valid JSON matching the requested "
    "schema exactly. No prose, no markdown fences, nothing outside the JSON."
)


class GeminiJudge:
    """Satisfies ragjudge's Judge protocol. No inheritance required."""

    def __init__(self, client, model: str, temperature: float = 0.0) -> None:
        self._client = client
        self.model = model
        self.temperature = temperature

    async def judge(self, prompt: str, schema: dict[str, Any]) -> JudgeResponse:
        resp = await self._client.aio.models.generate_content(
            model=self.model,
            contents=(
                f"{prompt}\n\nReturn JSON matching this schema:\n"
                f"{json.dumps(schema)}"
            ),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM,
                temperature=self.temperature,
                response_mime_type="application/json",
            ),
        )
        raw = (resp.text or "").strip()
        try:
            parsed = json.loads(_unfence(raw))
        except json.JSONDecodeError:
            # A judge that cannot be parsed must not be read as a pass. Empty
            # parsed data makes every metric score zero, which is the safe
            # direction to fail in.
            parsed = {}
        return JudgeResponse(parsed=parsed, raw=raw)


def _unfence(text: str) -> str:
    """Strip ```json fences. JSON mode should prevent them; models fence anyway."""
    if not text.startswith("```"):
        return text
    body = text.strip("`")
    return body[4:].strip() if body.lower().startswith("json") else body.strip()


# --------------------------------------------------------------------------
# a custom metric
# --------------------------------------------------------------------------

REFUSAL_PROMPT = """A question-answering system may only discuss the book \
"AI for Founders" and a reader's situation as a founder. It was asked an \
off-topic question and should have declined, or said the topic is outside \
what it can help with.

Answering the question anyway is a failure.

QUESTION: {question}

ANSWER:
{answer}"""

REFUSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "declined": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["declined", "reason"],
}


class Refusal:
    """Off-topic questions must be turned down.

    None of ragjudge's built-in metrics cover this: faithfulness and relevance
    both assume the system was *supposed* to answer. Staying in scope is its
    own property, and the Metric protocol makes it a small class rather than a
    fork of the library.
    """

    name = "refusal"
    threshold = 1.0

    async def score(self, sample: Sample, judge) -> Score:
        resp = await judge.judge(
            REFUSAL_PROMPT.format(question=sample.question, answer=sample.answer),
            REFUSAL_SCHEMA,
        )
        declined = bool(resp.parsed.get("declined"))
        reason = str(resp.parsed.get("reason", ""))[:120] or "judge gave no reason"
        return Score(
            metric=self.name,
            value=1.0 if declined else 0.0,
            reasoning=reason,
            passed=declined,
        )
