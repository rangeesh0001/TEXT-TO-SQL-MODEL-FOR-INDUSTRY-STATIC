"""
query_cache.py — Gap 4: Give the agent memory of past queries.

Stores question → SQL mappings in a local SQLite file so repeat
questions skip schema re-exploration and LLM SQL generation.

Similarity matching uses a normalised fingerprint (lowercase, stripped
punctuation, sorted tokens) so minor rewording still hits the cache.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_CACHE_PATH = Path(__file__).parent / "query_cache.db"

_STOPWORDS = frozenset(
    "a an the is are was were be been being have has had do does did "
    "will would shall should may might must can could for in on at to "
    "of with by from that this it its me my we our you your he she "
    "they them their what which who how many much when where".split()
)


def _fingerprint(question: str, db_name: str = "business") -> str:
    """Normalise a question to a stable hash for cache lookups.

    The database name is part of the key: the same question asked of
    business.db and enterprise.db needs different SQL, so they must never
    share a cache entry.
    """
    tokens = re.sub(r"[^a-z0-9 ]", " ", question.lower()).split()
    meaningful = sorted(t for t in tokens if t not in _STOPWORDS and len(t) > 1)
    key = f"{db_name}|" + " ".join(meaningful)
    return hashlib.md5(key.encode()).hexdigest()


@dataclass
class CacheEntry:
    question: str
    fingerprint: str
    sql_query: str
    summary: str
    row_count: int
    token_cost_usd: float
    hit_count: int
    created_at: float
    last_hit_at: float


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_CACHE_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS query_cache (
            fingerprint TEXT PRIMARY KEY,
            question     TEXT NOT NULL,
            sql_query    TEXT NOT NULL,
            summary      TEXT NOT NULL,
            row_count    INTEGER DEFAULT 0,
            token_cost_usd REAL DEFAULT 0.0,
            hit_count    INTEGER DEFAULT 0,
            created_at   REAL NOT NULL,
            last_hit_at  REAL NOT NULL
        )"""
    )
    conn.commit()
    return conn


def get(question: str, db_name: str = "business") -> Optional[CacheEntry]:
    """Return a cached entry if one exists for this question on this database, else None."""
    fp = _fingerprint(question, db_name)
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM query_cache WHERE fingerprint = ?", (fp,)
    ).fetchone()
    if row is None:
        conn.close()
        return None

    # Bump hit counter
    conn.execute(
        "UPDATE query_cache SET hit_count = hit_count + 1, last_hit_at = ? WHERE fingerprint = ?",
        (time.time(), fp),
    )
    conn.commit()
    conn.close()

    cols = ["fingerprint", "question", "sql_query", "summary", "row_count",
            "token_cost_usd", "hit_count", "created_at", "last_hit_at"]
    return CacheEntry(**dict(zip(cols, row)))


def put(
    question: str,
    sql_query: str,
    summary: str,
    row_count: int = 0,
    token_cost_usd: float = 0.0,
    db_name: str = "business",
) -> str:
    """Store a new entry and return its fingerprint."""
    fp = _fingerprint(question, db_name)
    now = time.time()
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO query_cache
           (fingerprint, question, sql_query, summary, row_count,
            token_cost_usd, hit_count, created_at, last_hit_at)
           VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (fp, question[:1000], sql_query, summary[:2000], row_count, token_cost_usd, now, now),
    )
    conn.commit()
    conn.close()
    return fp


def invalidate(question: str, db_name: str = "business") -> bool:
    """Remove a single entry from the cache. Returns True if it existed."""
    fp = _fingerprint(question, db_name)
    conn = _get_conn()
    deleted = conn.execute("DELETE FROM query_cache WHERE fingerprint = ?", (fp,)).rowcount
    conn.commit()
    conn.close()
    return deleted > 0


def clear_all() -> int:
    """Wipe the entire cache. Returns number of rows deleted."""
    conn = _get_conn()
    deleted = conn.execute("DELETE FROM query_cache").rowcount
    conn.commit()
    conn.close()
    return deleted


def stats() -> dict:
    conn = _get_conn()
    row = conn.execute(
        """SELECT COUNT(*), SUM(hit_count), AVG(token_cost_usd),
                  SUM(token_cost_usd * hit_count)
           FROM query_cache"""
    ).fetchone()
    conn.close()
    count, total_hits, avg_cost, saved_cost = row
    return {
        "cached_queries": count or 0,
        "total_cache_hits": int(total_hits or 0),
        "avg_cost_per_miss_usd": round(avg_cost or 0, 6),
        "estimated_savings_usd": round(saved_cost or 0, 4),
    }


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    clear_all()
    put(
        "Which product category had the highest revenue last quarter?",
        "SELECT p.category, SUM(oi.total_price) AS revenue FROM order_items oi JOIN products p ON oi.product_id = p.id GROUP BY p.category ORDER BY revenue DESC LIMIT 1",
        "Electronics had the highest revenue at $45,230.",
        row_count=1,
        token_cost_usd=0.0004,
    )

    # Exact hit
    entry = get("Which product category had the highest revenue last quarter?")
    print(f"Exact hit:    {entry.sql_query[:60]}...")

    # Normalised hit — slightly different wording
    entry2 = get("what product category has highest revenue in the last quarter")
    if entry2:
        print(f"Fuzzy hit:    {entry2.sql_query[:60]}...")
    else:
        print("Fuzzy hit:    MISS (fingerprints diverged)")

    print(f"\nCache stats: {stats()}")
