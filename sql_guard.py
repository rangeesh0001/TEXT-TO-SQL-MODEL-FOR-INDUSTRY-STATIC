"""
sql_guard.py — Gap 5: Restrict the agent to SELECT-only queries.

Blocks DROP, DELETE, UPDATE, INSERT, TRUNCATE, ALTER, CREATE, and
multi-statement injection before any SQL reaches the database engine.
"""

import re
from dataclasses import dataclass
from typing import Optional

_BANNED = re.compile(
    r"\b(DROP|DELETE|UPDATE|INSERT|TRUNCATE|ALTER|CREATE|REPLACE|MERGE|"
    r"ATTACH|DETACH|PRAGMA|VACUUM|REINDEX|ANALYZE)\b",
    re.IGNORECASE,
)

_MULTI_STATEMENT = re.compile(r";\s*\S")
_COMMENT_STRIP = re.compile(r"(--[^\n]*|/\*.*?\*/)", re.DOTALL)


@dataclass
class GuardResult:
    allowed: bool
    reason: Optional[str] = None

    def __bool__(self) -> bool:
        return self.allowed


def check_sql(query: str) -> GuardResult:
    """Return GuardResult(allowed=True) or GuardResult(allowed=False, reason=...)."""
    # Strip comments so `--DROP` tricks don't bypass the check
    clean = _COMMENT_STRIP.sub(" ", query).strip()

    if not clean:
        return GuardResult(False, "Empty query")

    # Must start with SELECT (allow optional leading whitespace / WITH for CTEs)
    first_token = clean.lstrip().split()[0].upper()
    if first_token not in ("SELECT", "WITH", "EXPLAIN"):
        return GuardResult(
            False,
            f"Only SELECT statements are allowed. Got: {first_token}",
        )

    # Catch stacked statements: SELECT ...; DROP TABLE ...
    if _MULTI_STATEMENT.search(clean):
        return GuardResult(False, "Multi-statement queries are not allowed")

    # Catch banned keywords anywhere in the query
    match = _BANNED.search(clean)
    if match:
        return GuardResult(
            False,
            f"Forbidden keyword detected: {match.group().upper()}",
        )

    return GuardResult(True)


def assert_safe(query: str) -> str:
    """Raise ValueError if the query is not safe, otherwise return the query."""
    result = check_sql(query)
    if not result:
        raise ValueError(f"SQL guard blocked query: {result.reason}")
    return query


# ---------------------------------------------------------------------------
# Demo: show what failure looks like
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [
        ("SELECT * FROM customers", True),
        ("SELECT id FROM orders WHERE id = 1; DROP TABLE orders;--", False),
        ("DELETE FROM customers WHERE id = 1", False),
        ("UPDATE products SET unit_price = 0", False),
        ("DROP TABLE order_items", False),
        ("-- safe looking\nSELECT 1; INSERT INTO customers VALUES(1,'x','y','z')", False),
        ("WITH cte AS (SELECT 1) SELECT * FROM cte", True),
        ("PRAGMA table_info(customers)", False),
        ("  select count(*) from orders  ", True),
    ]
    print(f"{'Query':<65} {'Expected':<10} {'Result'}")
    print("-" * 95)
    for q, expected in tests:
        r = check_sql(q)
        icon = "✓" if r.allowed == expected else "✗ MISMATCH"
        reason = f"  [{r.reason}]" if not r.allowed else ""
        print(f"{q[:64]:<65} {str(expected):<10} {icon}{reason}")
