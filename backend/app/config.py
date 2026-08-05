"""Settings for the API, extending the loader's settings.

Everything the embedding pipeline needed (database_url, gemini_api_key,
embed_model, embed_dim) is inherited, so the API is guaranteed to embed queries
with the same model and dimensionality the chunks were indexed with.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

from pydantic import Field, field_validator

BACKEND_DIR: Final[Path] = Path(__file__).resolve().parents[1]

# The prefix contract lives next to the loader that wrote the index. Import it
# rather than retyping the format string — see embed_and_store.py.
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

from embed_and_store import Settings as LoaderSettings  # noqa: E402
from embed_and_store import query_embed_text, to_vector_literal  # noqa: E402,F401


class Settings(LoaderSettings):
    # Models. Both overridable from .env; prices and names change, so check
    # https://ai.google.dev/gemini-api/docs/models before assuming.
    chat_model: str = "gemini-3.6-flash"
    condense_model: str = "gemini-3.5-flash-lite"

    # Retrieval
    top_k: int = Field(default=8, gt=0, le=20)
    recap_penalty: float = Field(default=0.02, ge=0)

    # Request shaping
    max_question_chars: int = Field(default=1000, gt=0)
    history_turns: int = Field(default=6, ge=0)

    # Abuse limits. In-memory and per-process — see limits.py.
    rate_per_hour: int = Field(default=10, gt=0)
    rate_per_day: int = Field(default=2000, gt=0)

    # Usage recording — see usage.py. Off by flipping usage_enabled=false.
    usage_enabled: bool = True

    # Salt for hashing IPs. Left empty it derives from database_url, which is
    # already secret, already stable across restarts, and never leaves the
    # server — so hashes stay comparable between deploys without asking you to
    # manage another secret. Set it explicitly if you ever rotate the DB URL
    # and want old rows to keep matching new ones.
    usage_salt: str = ""

    # Admin dashboard at /admin. Unset means the routes 404 — a dashboard with
    # a default password would be worse than no dashboard. Set it with
    # `fly secrets set ADMIN_PASSWORD='…'`.
    admin_password: str = ""

    # Gemini pricing, in dollars per million tokens, for turning recorded token
    # counts into a spend figure. Left at zero the dashboard shows tokens only
    # and no money — deliberately, because a hardcoded price that has since
    # changed is worse than no number at all. Set these from the current rates
    # at https://ai.google.dev/gemini-api/docs/pricing
    price_in_per_mtok: float = Field(default=0.0, ge=0)
    price_out_per_mtok: float = Field(default=0.0, ge=0)

    # Built frontend, served so the UI and API share one origin.
    frontend_dist: Path = Path("../frontend/dist")

    @field_validator("frontend_dist", mode="after")
    @classmethod
    def _anchor_dist(cls, v: Path) -> Path:
        return v if v.is_absolute() else (BACKEND_DIR / v).resolve()


settings = Settings()  # type: ignore[call-arg]
