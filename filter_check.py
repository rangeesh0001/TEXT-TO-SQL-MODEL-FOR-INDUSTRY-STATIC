"""
filter_check.py — catch silent 0-row results caused by wrong filter values.

Added on top of the original project. Problem it solves: on enterprise.db the
data stores codes ('COMP', 'GLD', 'NE'), but the LLM tends to write the words a
person would use ('completed', 'Gold', 'Northeast'). The SQL runs fine, returns
0 rows, and the original agent reported that as a valid answer.

How it works (no LLM call, so it costs nothing):
  1. Pull every  column = 'value'  /  column IN ('a','b')  filter out of the SQL.
  2. Work out which table each column belongs to (resolving aliases like o.sts_cd).
  3. Compare the value against the real distinct values in that column.
  4. Report any value that does not exist in the data, with the allowed values.

The agent uses the report to retry SQL generation once with precise feedback.
If every filter value is real, 0 rows is treated as a genuine empty answer and
no retry is spent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SQL_WORDS = {
    "where", "on", "join", "left", "right", "inner", "outer", "full", "cross",
    "group", "order", "limit", "having", "union", "select", "as", "and", "or",
    "natural", "using",
}

# FROM table [AS] alias  /  JOIN table [AS] alias
_TABLE_REF = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_]\w*)(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?", re.I)
# [alias.]col = 'value'    (also !=, <>, and LIKE without wildcards)
_EQ_FILTER = re.compile(r"([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)?)\s*(=|!=|<>|\bLIKE\b)\s*'((?:[^']|'')*)'", re.I)
# [alias.]col [NOT] IN ('a', 'b')
_IN_FILTER = re.compile(r"([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)?)\s+(?:NOT\s+)?IN\s*\(([^)]*)\)", re.I)
_LITERAL = re.compile(r"'((?:[^']|'')*)'")


@dataclass
class FilterMismatch:
    column: str            # as written in the SQL, e.g. "o.sts_cd"
    value: str             # the value the model used, e.g. "completed"
    allowed: list[str]     # the values that actually exist
    case_only: bool        # True if it matches except for upper/lower case

    def describe(self) -> str:
        allowed = ", ".join(f"'{v}'" for v in self.allowed)
        hint = " (wrong upper/lower case — SQLite '=' is case-sensitive)" if self.case_only else ""
        return f"{self.column} = '{self.value}' does not exist in the data{hint}. Allowed values: {allowed}"


def _alias_map(sql: str) -> dict[str, str]:
    """alias or table name (lower-case) -> real table name."""
    out: dict[str, str] = {}
    for table, alias in _TABLE_REF.findall(sql):
        out[table.lower()] = table
        if alias and alias.lower() not in _SQL_WORDS:
            out[alias.lower()] = table
    return out


def _candidate_values(col_ref: str, aliases: dict[str, str],
                      value_map: dict[tuple[str, str], list[str]]) -> list[str] | None:
    """Allowed values for a column reference, or None if we know nothing about it."""
    if "." in col_ref:
        prefix, col = col_ref.split(".", 1)
        table = aliases.get(prefix.lower())
        tables = [table] if table else []
    else:
        col = col_ref
        tables = list({t for t in aliases.values()})
    found: set[str] = set()
    known = False
    for (t, c), vals in value_map.items():
        if c.lower() == col.lower() and t in tables:
            found.update(vals)
            known = True
    return sorted(found) if known else None


def find_filter_mismatches(sql: str, value_map: dict[tuple[str, str], list[str]]) -> list[FilterMismatch]:
    """Return every filter value in `sql` that does not exist in its column."""
    aliases = _alias_map(sql)
    checks: list[tuple[str, str, bool]] = []   # (column, value, is_like)

    for col_ref, op, value in _EQ_FILTER.findall(sql):
        if op.upper() == "LIKE" and ("%" in value or "_" in value):
            continue  # wildcard pattern, not an exact value
        checks.append((col_ref, value.replace("''", "'"), op.upper() == "LIKE"))
    for col_ref, inner in _IN_FILTER.findall(sql):
        for value in _LITERAL.findall(inner):
            checks.append((col_ref, value.replace("''", "'"), False))

    mismatches: list[FilterMismatch] = []
    seen: set[tuple[str, str]] = set()
    for col_ref, value, is_like in checks:
        if (col_ref, value) in seen:
            continue
        seen.add((col_ref, value))
        allowed = _candidate_values(col_ref, aliases, value_map)
        if allowed is None or value in allowed:
            continue
        case_only = value.lower() in {a.lower() for a in allowed}
        if case_only and is_like:
            continue  # SQLite LIKE ignores case, so this one is fine
        mismatches.append(FilterMismatch(col_ref, value, allowed, case_only))
    return mismatches
