"""
token_tracker.py — Gap 8: Track token spend per query, estimate cost, show
how schema caching cuts cost on repeat questions.

Haiku 4.5 pricing (as of 2025): $0.80 / MTok input, $4.00 / MTok output.
Prompt cache read: $0.08 / MTok  (10× cheaper than uncached input).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Pricing (per million tokens)
# ---------------------------------------------------------------------------
INPUT_COST_PER_MTOK = 0.80
OUTPUT_COST_PER_MTOK = 4.00
CACHE_READ_PER_MTOK = 0.08       # prompt cache hit
CACHE_WRITE_PER_MTOK = 1.00      # first-write surcharge (amortised across hits)

_STORE_PATH = Path(__file__).parent / "token_log.db"


@dataclass
class PhaseUsage:
    phase: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0     # tokens served from cache
    cache_write_tokens: int = 0    # tokens written to cache


@dataclass
class QueryUsage:
    question: str
    phases: list[PhaseUsage] = field(default_factory=list)
    schema_was_cached: bool = False
    ts: float = field(default_factory=time.time)

    # Computed ----------------------------------------------------------------

    @property
    def total_input(self) -> int:
        return sum(p.input_tokens for p in self.phases)

    @property
    def total_output(self) -> int:
        return sum(p.output_tokens for p in self.phases)

    @property
    def total_cache_read(self) -> int:
        return sum(p.cache_read_tokens for p in self.phases)

    @property
    def total_cache_write(self) -> int:
        return sum(p.cache_write_tokens for p in self.phases)

    @property
    def cost_usd(self) -> float:
        uncached_input = max(0, self.total_input - self.total_cache_read)
        c = (
            uncached_input * INPUT_COST_PER_MTOK / 1_000_000
            + self.total_output * OUTPUT_COST_PER_MTOK / 1_000_000
            + self.total_cache_read * CACHE_READ_PER_MTOK / 1_000_000
            + self.total_cache_write * CACHE_WRITE_PER_MTOK / 1_000_000
        )
        return round(c, 6)

    @property
    def hypothetical_cost_no_cache(self) -> float:
        """What the same query would cost with no prompt caching at all."""
        all_input = self.total_input + self.total_cache_read
        c = (
            all_input * INPUT_COST_PER_MTOK / 1_000_000
            + self.total_output * OUTPUT_COST_PER_MTOK / 1_000_000
        )
        return round(c, 6)

    @property
    def cache_savings_usd(self) -> float:
        return round(self.hypothetical_cost_no_cache - self.cost_usd, 6)

    def summary(self) -> dict:
        return {
            "total_input_tokens": self.total_input,
            "total_output_tokens": self.total_output,
            "cache_read_tokens": self.total_cache_read,
            "schema_cached": self.schema_was_cached,
            "cost_usd": self.cost_usd,
            "hypothetical_no_cache_usd": self.hypothetical_cost_no_cache,
            "cache_savings_usd": self.cache_savings_usd,
            "phases": [asdict(p) for p in self.phases],
        }


# ---------------------------------------------------------------------------
# Persistent log
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_STORE_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS token_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            schema_cached INTEGER,
            cost_usd REAL,
            ts REAL
        )"""
    )
    conn.commit()
    return conn


def log_usage(usage: QueryUsage) -> None:
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO token_log
               (question, input_tokens, output_tokens, cache_read_tokens,
                schema_cached, cost_usd, ts)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                usage.question[:500],
                usage.total_input,
                usage.total_output,
                usage.total_cache_read,
                int(usage.schema_was_cached),
                usage.cost_usd,
                usage.ts,
            ),
        )


def get_cost_report() -> dict:
    """Aggregate stats across all logged queries."""
    with _get_conn() as conn:
        row = conn.execute(
            """SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens),
                      SUM(cache_read_tokens), SUM(cost_usd),
                      AVG(cost_usd), SUM(schema_cached)
               FROM token_log"""
        ).fetchone()

    if not row or row[0] == 0:
        return {"message": "No queries logged yet"}

    total_queries, total_in, total_out, total_cache_read, total_cost, avg_cost, cached_count = row
    hypothetical = (
        (total_in + total_cache_read) * INPUT_COST_PER_MTOK / 1_000_000
        + total_out * OUTPUT_COST_PER_MTOK / 1_000_000
    )
    return {
        "total_queries": total_queries,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_cache_read_tokens": total_cache_read,
        "schema_cached_queries": int(cached_count),
        "total_cost_usd": round(total_cost, 4),
        "avg_cost_per_query_usd": round(avg_cost, 6),
        "hypothetical_no_cache_cost_usd": round(hypothetical, 4),
        "total_cache_savings_usd": round(hypothetical - total_cost, 4),
        "cache_hit_rate_pct": round(total_cache_read / max(1, total_in + total_cache_read) * 100, 1),
    }


# ---------------------------------------------------------------------------
# Helper: extract usage from Anthropic response object
# ---------------------------------------------------------------------------

def extract_usage(response) -> tuple[int, int, int, int]:
    """Return (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens).

    Handles both:
      - LangChain AIMessage (usage_metadata + response_metadata)
      - Raw Anthropic SDK response (response.usage)
    """
    # ── LangChain AIMessage path ──────────────────────────────────────────
    # usage_metadata is the canonical location in modern langchain-core
    um = getattr(response, "usage_metadata", None)
    if um is not None:
        if isinstance(um, dict):
            input_tok  = um.get("input_tokens", 0) or 0
            output_tok = um.get("output_tokens", 0) or 0
        else:
            input_tok  = getattr(um, "input_tokens", 0) or 0
            output_tok = getattr(um, "output_tokens", 0) or 0

        # Cache details live in response_metadata for langchain-anthropic
        rm = getattr(response, "response_metadata", {}) or {}
        usage_block = rm.get("usage", {})
        cache_read  = usage_block.get("cache_read_input_tokens", 0) or 0
        cache_write = usage_block.get("cache_creation_input_tokens", 0) or 0
        return input_tok, output_tok, cache_read, cache_write

    # ── Raw Anthropic SDK path ────────────────────────────────────────────
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0, 0, 0
    input_tok  = getattr(usage, "input_tokens", 0) or 0
    output_tok = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return input_tok, output_tok, cache_read, cache_write


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    usage = QueryUsage(question="demo query")
    usage.phases.append(PhaseUsage("sql_gen", input_tokens=2000, output_tokens=120, cache_read_tokens=1500))
    usage.phases.append(PhaseUsage("summarize", input_tokens=800, output_tokens=80, cache_read_tokens=600))
    usage.schema_was_cached = True

    import json
    print(json.dumps(usage.summary(), indent=2))
    log_usage(usage)
    print("\nCumulative report:")
    print(json.dumps(get_cost_report(), indent=2))
