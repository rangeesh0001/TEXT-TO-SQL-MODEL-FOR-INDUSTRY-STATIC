"""
eval_harness.py — Gap 6: Evaluate and benchmark the text-to-SQL agent.

Goes beyond 'it worked on my demo query' by running a suite of
ground-truth question→result pairs and measuring:

  - Execution success rate (did SQL run without error?)
  - Row count match (did we get the expected number of rows?)
  - Value match (does the first result row match expected values?)
  - Silent wrong answer detection (query ran but returned wrong data)

Also demonstrates Gap 3: silent wrong numbers.
The question "What is total revenue?" has two plausible answers depending
on whether you include cancelled orders — this test catches that bug.

Usage:
  python eval_harness.py                        # business.db suite (10 cases)
  python eval_harness.py --db enterprise        # enterprise.db suite (12 cases, added)
  python eval_harness.py --db enterprise --save results/after.json --label "after fix"
  python eval_harness.py --sleep 4              # pause between cases (Gemini free tier)
  python eval_harness.py --show-failures-only

Added on top of the original:
  - ENTERPRISE_DB_CASES: 12 cases on the 26-table enterprise.db, written to
    hit code-value filters (COMP/GLD/NE), a lookup join, last-quarter dates and
    one case where 0 rows IS the correct answer.
  - match_ground_truth check: the agent's numbers must match the ground-truth
    query's numbers (data is random per seed, so values are computed live).
  - An unexpected 0-row result (ground truth has rows, agent returns none, no
    error) now counts as a SILENT WRONG answer. The original only checked
    silent-wrong answers when the agent returned rows.
  - --db now actually runs the agent against that database (it used to always
    query business.db), the query cache is cleared before each run so cached
    answers don't hide regressions, and --save writes a JSON report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sqlalchemy import create_engine, text

# ---------------------------------------------------------------------------
# Test case definition
# ---------------------------------------------------------------------------

@dataclass
class EvalCase:
    id: str
    question: str
    ground_truth_sql: str
    # What to check in the result
    expected_row_count: Optional[int] = None       # exact count (None = skip)
    expected_min_rows: Optional[int] = None        # at least N rows
    expected_col: Optional[str] = None             # column to spot-check
    expected_value: Optional[Any] = None           # expected value in first row
    expected_value_tolerance: float = 0.01         # relative tolerance for floats
    # Silent-wrong-answer scenarios: extra query that must NOT match this one
    silent_wrong_sql: Optional[str] = None
    silent_wrong_description: Optional[str] = None
    # Compare the agent's numbers with the ground-truth query's numbers (added)
    match_ground_truth: bool = False
    tags: list[str] = field(default_factory=list)
    note: str = ""


# ---------------------------------------------------------------------------
# Ground-truth test suite for the clean business.db schema
# ---------------------------------------------------------------------------

BUSINESS_DB_CASES: list[EvalCase] = [
    EvalCase(
        id="B01",
        question="How many customers are in the database?",
        ground_truth_sql="SELECT COUNT(*) AS cnt FROM customers",
        expected_row_count=1,
        expected_col="cnt",
        expected_value=150,
        tags=["count", "basic"],
    ),
    EvalCase(
        id="B02",
        question="What are the top 3 product categories by total revenue?",
        ground_truth_sql="""
            SELECT p.category, SUM(oi.total_price) AS revenue
            FROM order_items oi
            JOIN products p ON oi.product_id = p.id
            JOIN orders o ON oi.order_id = o.id
            WHERE o.status = 'completed'
            GROUP BY p.category
            ORDER BY revenue DESC
            LIMIT 3
        """,
        expected_row_count=3,
        tags=["aggregation", "join", "filter"],
        note="Only completed orders — agent often forgets the status filter",
        # Silent wrong: same query WITHOUT the status filter
        silent_wrong_sql="""
            SELECT p.category, SUM(oi.total_price) AS revenue
            FROM order_items oi
            JOIN products p ON oi.product_id = p.id
            GROUP BY p.category
            ORDER BY revenue DESC
            LIMIT 3
        """,
        silent_wrong_description="Revenue includes cancelled/processing orders",
    ),
    EvalCase(
        id="B03",
        question="Who are the top 5 customers by total spending on completed orders?",
        ground_truth_sql="""
            SELECT c.name, SUM(oi.total_price) AS total_spent
            FROM customers c
            JOIN orders o ON c.id = o.customer_id
            JOIN order_items oi ON o.id = oi.order_id
            WHERE o.status = 'completed'
            GROUP BY c.id, c.name
            ORDER BY total_spent DESC
            LIMIT 5
        """,
        expected_row_count=5,
        expected_min_rows=5,
        tags=["aggregation", "join", "filter", "top-n"],
    ),
    EvalCase(
        id="B04",
        question="Which products have never been ordered?",
        ground_truth_sql="""
            SELECT p.name
            FROM products p
            LEFT JOIN order_items oi ON p.id = oi.product_id
            WHERE oi.id IS NULL
        """,
        expected_min_rows=0,
        tags=["anti-join", "left-join", "null-check"],
        note="Requires LEFT JOIN + IS NULL, not NOT IN",
    ),
    EvalCase(
        id="B05",
        question="What percentage of orders are from Platinum tier customers?",
        ground_truth_sql="""
            SELECT
                ROUND(100.0 * SUM(CASE WHEN c.tier = 'Platinum' THEN 1 ELSE 0 END) / COUNT(*), 2) AS pct
            FROM orders o
            JOIN customers c ON o.customer_id = c.id
        """,
        expected_row_count=1,
        expected_col="pct",
        tags=["percent", "case-when", "join"],
    ),
    EvalCase(
        id="B06",
        question="How many orders were placed each month in 2024?",
        ground_truth_sql="""
            SELECT strftime('%Y-%m', order_date) AS month, COUNT(*) AS order_count
            FROM orders
            WHERE order_date >= '2024-01-01' AND order_date < '2025-01-01'
            GROUP BY month
            ORDER BY month
        """,
        expected_min_rows=1,
        tags=["date-trunc", "time-series", "sqlite-date"],
        note="SQLite uses strftime, not DATE_TRUNC or TO_CHAR",
    ),
    EvalCase(
        id="B07",
        question="What is the average order value by customer region for completed orders?",
        ground_truth_sql="""
            SELECT c.region, AVG(o_total.ttl) AS avg_order_value
            FROM customers c
            JOIN orders o ON c.id = o.customer_id
            JOIN (
                SELECT order_id, SUM(total_price) AS ttl
                FROM order_items GROUP BY order_id
            ) o_total ON o.id = o_total.order_id
            WHERE o.status = 'completed'
            GROUP BY c.region
            ORDER BY avg_order_value DESC
        """,
        expected_min_rows=1,
        tags=["subquery", "avg", "group-by"],
    ),
    EvalCase(
        id="B08",
        question="Show the monthly revenue trend for Electronics in 2023",
        ground_truth_sql="""
            SELECT strftime('%Y-%m', o.order_date) AS month,
                   SUM(oi.total_price) AS revenue
            FROM orders o
            JOIN order_items oi ON o.id = oi.order_id
            JOIN products p ON oi.product_id = p.id
            WHERE p.category = 'Electronics'
              AND o.status = 'completed'
              AND o.order_date >= '2023-01-01'
              AND o.order_date < '2024-01-01'
            GROUP BY month
            ORDER BY month
        """,
        expected_min_rows=1,
        tags=["time-series", "filter", "join"],
    ),
    EvalCase(
        id="B09",
        question="Which salesperson-equivalent (customer) region has the most Gold-tier customers?",
        ground_truth_sql="""
            SELECT region, COUNT(*) AS gold_count
            FROM customers
            WHERE tier = 'Gold'
            GROUP BY region
            ORDER BY gold_count DESC
            LIMIT 1
        """,
        expected_row_count=1,
        tags=["filter", "count", "group-by"],
    ),
    EvalCase(
        id="B10",
        question="What is the total revenue and total cost across all completed orders?",
        ground_truth_sql="""
            SELECT
                SUM(oi.total_price) AS total_revenue,
                SUM(oi.quantity * p.unit_price * 0.6) AS estimated_cogs
            FROM order_items oi
            JOIN products p ON oi.product_id = p.id
            JOIN orders o ON oi.order_id = o.id
            WHERE o.status = 'completed'
        """,
        expected_row_count=1,
        tags=["multi-agg", "join", "filter"],
    ),
]


# ---------------------------------------------------------------------------
# Ground-truth test suite for the 26-table enterprise.db schema  (ADDED)
# ---------------------------------------------------------------------------
# Every case below needs the agent to map a human word to a stored code
# (Completed -> 'COMP', Gold -> 'GLD', Northeast -> 'NE', Express -> 'EXP',
# Defective -> 'DEF') or to anchor a date on ord_hdr.ord_dt. These are exactly
# the places where the original agent returned 0 rows without any warning.

ENTERPRISE_DB_CASES: list[EvalCase] = [
    EvalCase(
        id="E01",
        question="How many orders have been completed?",
        ground_truth_sql="SELECT COUNT(*) AS cnt FROM ord_hdr WHERE sts_cd = 'COMP'",
        expected_row_count=1,
        match_ground_truth=True,
        silent_wrong_sql="SELECT COUNT(*) AS cnt FROM ord_hdr",
        silent_wrong_description="Counted every order, not just completed ones",
        tags=["code-value", "status"],
        note="Status is stored as 'COMP', not 'completed'",
    ),
    EvalCase(
        id="E02",
        question="How many Gold tier customers do we have?",
        ground_truth_sql="SELECT COUNT(*) AS cnt FROM cust_mstr WHERE tier_cd = 'GLD'",
        expected_row_count=1,
        match_ground_truth=True,
        tags=["code-value", "tier"],
        note="Tier is stored as 'GLD', not 'Gold'",
    ),
    EvalCase(
        id="E03",
        question="How many customers are there in each region? Show the region name.",
        ground_truth_sql="""
            SELECT r.rgn_nm, COUNT(*) AS cust_count
            FROM cust_mstr c JOIN rgn_lkp r ON c.rgn_cd = r.rgn_cd
            GROUP BY r.rgn_nm ORDER BY cust_count DESC
        """,
        expected_row_count=6,
        tags=["lookup-join", "group-by"],
        note="Region names live in rgn_lkp, not cust_mstr",
    ),
    EvalCase(
        id="E04",
        question="What is the total revenue from completed orders placed by customers in the Northeast region?",
        ground_truth_sql="""
            SELECT ROUND(SUM(o.ttl_amt), 2) AS revenue
            FROM ord_hdr o JOIN cust_mstr c ON o.cust_id = c.cust_id
            WHERE o.sts_cd = 'COMP' AND c.rgn_cd = 'NE'
        """,
        expected_row_count=1,
        match_ground_truth=True,
        silent_wrong_sql="""
            SELECT ROUND(SUM(o.ttl_amt), 2) AS revenue
            FROM ord_hdr o JOIN cust_mstr c ON o.cust_id = c.cust_id
            WHERE c.rgn_cd = 'NE'
        """,
        silent_wrong_description="Revenue includes cancelled/processing orders",
        tags=["code-value", "join", "two-filters"],
    ),
    EvalCase(
        id="E05",
        question="How many orders were cancelled in 2024?",
        ground_truth_sql="""
            SELECT COUNT(*) AS cnt FROM ord_hdr
            WHERE sts_cd = 'CANC' AND ord_dt >= '2024-01-01' AND ord_dt < '2025-01-01'
        """,
        expected_row_count=1,
        match_ground_truth=True,
        tags=["code-value", "date-filter"],
    ),
    EvalCase(
        id="E06",
        question="What is the total value of completed orders from Platinum customers?",
        ground_truth_sql="""
            SELECT ROUND(SUM(o.ttl_amt), 2) AS revenue
            FROM ord_hdr o JOIN cust_mstr c ON o.cust_id = c.cust_id
            WHERE c.tier_cd = 'PLT' AND o.sts_cd = 'COMP'
        """,
        expected_row_count=1,
        match_ground_truth=True,
        tags=["code-value", "join", "two-filters"],
    ),
    EvalCase(
        id="E07",
        question="What is the total outstanding balance on open invoices?",
        ground_truth_sql="SELECT ROUND(SUM(bal_amt), 2) AS open_balance FROM ar_hdr WHERE sts_cd = 'OPEN'",
        expected_row_count=1,
        match_ground_truth=True,
        tags=["code-value", "finance"],
        note="Invoices are in ar_hdr; ord_hdr also has an sts_cd column with different codes",
    ),
    EvalCase(
        id="E08",
        question="What are the top 3 product categories by revenue from completed orders?",
        ground_truth_sql="""
            SELECT p.cat_cd, ROUND(SUM(d.ext_amt), 2) AS revenue
            FROM ord_dtl d
            JOIN ord_hdr o ON d.ord_id = o.ord_id
            JOIN prd_mstr p ON d.prd_id = p.prd_id
            WHERE o.sts_cd = 'COMP'
            GROUP BY p.cat_cd ORDER BY revenue DESC LIMIT 3
        """,
        expected_row_count=3,
        match_ground_truth=True,
        tags=["code-value", "3-way-join", "top-n"],
    ),
    EvalCase(
        id="E09",
        question="How many orders were shipped using the Express 2-Day method?",
        ground_truth_sql="SELECT COUNT(*) AS cnt FROM ord_hdr WHERE shp_mth_cd = 'EXP'",
        expected_row_count=1,
        match_ground_truth=True,
        tags=["code-value", "lookup"],
    ),
    EvalCase(
        id="E10",
        question="Which customers have an address in Canada?",
        ground_truth_sql="""
            SELECT c.cust_nm FROM cust_mstr c
            JOIN cust_addr a ON c.cust_id = a.cust_id
            WHERE a.ctry_cd = 'CA'
        """,
        expected_row_count=0,
        tags=["correct-empty"],
        note="0 rows IS the right answer — the agent must not 'fix' this into a wrong country",
    ),
    EvalCase(
        id="E11",
        question="How many completed orders were placed last quarter?",
        ground_truth_sql="""
            SELECT COUNT(*) AS cnt FROM ord_hdr
            WHERE sts_cd = 'COMP' AND ord_dt BETWEEN '2024-01-01' AND '2024-03-31'
        """,
        expected_row_count=1,
        match_ground_truth=True,
        tags=["relative-date", "code-value"],
        note="Data ends in June 2024, so 'last quarter' = Jan–Mar 2024 (needs the enterprise date anchor)",
    ),
    EvalCase(
        id="E12",
        question="How many returns were due to defective products?",
        ground_truth_sql="SELECT COUNT(*) AS cnt FROM rtns_hdr WHERE rsn_cd = 'DEF'",
        expected_row_count=1,
        match_ground_truth=True,
        tags=["code-value", "returns"],
    ),
]

CASES_BY_DB: dict[str, list[EvalCase]] = {
    "business": BUSINESS_DB_CASES,
    "enterprise": ENTERPRISE_DB_CASES,
}

# ---------------------------------------------------------------------------
# Ambiguity test cases (Gap 7 demo)
# ---------------------------------------------------------------------------

AMBIGUITY_CASES: list[dict] = [
    {
        "question": "What is total revenue?",
        "interpretation_a": "SUM of order_items.total_price for ALL orders",
        "sql_a": "SELECT SUM(total_price) AS revenue FROM order_items",
        "interpretation_b": "SUM of order_items.total_price for COMPLETED orders only",
        "sql_b": """
            SELECT SUM(oi.total_price) AS revenue
            FROM order_items oi JOIN orders o ON oi.order_id = o.id
            WHERE o.status = 'completed'
        """,
        "why_it_matters": "Including cancelled orders inflates revenue by ~15-20%",
    },
    {
        "question": "Who are the top customers?",
        "interpretation_a": "Customers with highest total order value",
        "sql_a": "SELECT customer_id, SUM(total_price) AS val FROM order_items GROUP BY customer_id ORDER BY val DESC LIMIT 5",
        "interpretation_b": "Customers with most number of orders",
        "sql_b": "SELECT customer_id, COUNT(*) AS ord_count FROM orders GROUP BY customer_id ORDER BY ord_count DESC LIMIT 5",
        "why_it_matters": "A high-frequency low-value customer vs a rare high-value customer",
    },
    {
        "question": "Show me orders from last quarter",
        "interpretation_a": "Previous calendar quarter (Q1/Q2/Q3/Q4)",
        "sql_a": "-- Requires computing the previous calendar quarter boundaries",
        "interpretation_b": "Last 90 days from today",
        "sql_b": "SELECT * FROM orders WHERE order_date >= date('now', '-90 days')",
        "why_it_matters": "For a Q1 analysis run in April, these produce different date ranges",
    },
]


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    case_id: str
    question: str
    passed: bool
    agent_sql: str
    ground_truth_result: list[dict]
    agent_result: list[dict]
    silent_wrong: bool
    error: Optional[str]
    latency_ms: float
    checks: dict[str, bool]
    silent_reason: Optional[str] = None
    zero_row_retried: bool = False


def _run_sql(engine, sql: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text(sql.strip()))
        cols = list(rows.keys())
        return [dict(zip(cols, r)) for r in rows.fetchall()]


def _values_match(actual: Any, expected: Any, tol: float) -> bool:
    if expected is None:
        return True
    try:
        return abs(float(actual) - float(expected)) / max(abs(float(expected)), 1e-9) <= tol
    except (TypeError, ValueError):
        return str(actual).strip() == str(expected).strip()


def _looks_empty(rows: list[dict]) -> bool:
    """0 rows, or one row of only 0/NULL (what COUNT/SUM return when nothing matches)."""
    if not rows:
        return True
    if len(rows) == 1:
        vals = list(rows[0].values())
        return bool(vals) and all(v is None or (isinstance(v, (int, float)) and v == 0) for v in vals)
    return False


def _numbers(row: dict) -> list[float]:
    out = []
    for v in row.values():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(float(v))
    return out


def _matches_ground_truth(agent_rows: list[dict], gt_rows: list[dict], tol: float = 0.01) -> bool:
    """Every number in the ground truth's first row must appear in the agent's first row.

    Column names and order don't matter (the agent may alias 'cnt' as 'total'),
    only the values do.
    """
    if not gt_rows:
        return not agent_rows
    if not agent_rows:
        return False
    agent_nums = _numbers(agent_rows[0])
    for expected in _numbers(gt_rows[0]):
        if not any(_values_match(a, expected, tol) for a in agent_nums):
            return False
    return True


def evaluate(
    agent_fn: Callable[[str], dict],
    cases: list[EvalCase],
    engine,
    verbose: bool = True,
) -> list[TestResult]:
    results: list[TestResult] = []

    for case in cases:
        gt_result = _run_sql(engine, case.ground_truth_sql)
        start = time.time()
        agent_out: dict = {}
        try:
            agent_out = agent_fn(case.question)
            latency_ms = (time.time() - start) * 1000
            agent_sql = agent_out.get("sql_query", "")
            agent_result = agent_out.get("sql_result", [])
            agent_error = agent_out.get("error") or None
        except Exception as e:
            latency_ms = (time.time() - start) * 1000
            agent_sql = ""
            agent_result = []
            agent_error = str(e)

        checks = {}

        # Check 1: execution success
        checks["no_error"] = agent_error is None

        # Check 2: row count
        if case.expected_row_count is not None:
            checks["row_count"] = len(agent_result) == case.expected_row_count
        if case.expected_min_rows is not None:
            checks["min_rows"] = len(agent_result) >= case.expected_min_rows

        # Check 3: value spot-check
        if case.expected_col is not None and case.expected_value is not None and agent_result:
            actual_val = agent_result[0].get(case.expected_col)
            checks["value_match"] = _values_match(actual_val, case.expected_value, case.expected_value_tolerance)

        # Check 3b (added): numbers match the ground-truth query
        if case.match_ground_truth:
            checks["matches_ground_truth"] = _matches_ground_truth(agent_result, gt_result)

        # Check 4: silent wrong answer — did agent match the WRONG query's output instead?
        silent_wrong = False
        silent_reason = case.silent_wrong_description
        # Check 4b (added): ran without error but returned 0 rows when the
        # right answer has rows. The original never flagged this case.
        if not _looks_empty(gt_result) and _looks_empty(agent_result) and agent_error is None:
            silent_wrong = True
            silent_reason = ("Query ran without error but found no matching data "
                             "(0 rows or a count of 0) — likely a wrong filter value")
        if case.silent_wrong_sql and agent_result:
            wrong_result = _run_sql(engine, case.silent_wrong_sql)
            # If agent result matches wrong_result (by row count + first values) but NOT gt_result
            if len(agent_result) == len(wrong_result) and agent_result != gt_result:
                if wrong_result and agent_result:
                    first_wrong = list(wrong_result[0].values())
                    first_agent = list(agent_result[0].values())
                    if first_wrong and first_agent:
                        if _values_match(first_agent[0], first_wrong[0], 0.01):
                            silent_wrong = True

        passed = all(checks.values()) and checks.get("no_error", False) and not silent_wrong
        results.append(TestResult(
            case_id=case.id,
            question=case.question,
            passed=passed,
            agent_sql=agent_sql,
            ground_truth_result=gt_result,
            agent_result=agent_result,
            silent_wrong=silent_wrong,
            error=agent_error,
            latency_ms=latency_ms,
            checks=checks,
            silent_reason=silent_reason if silent_wrong else None,
            zero_row_retried=str(agent_out.get("zero_row_note") or "").startswith("First attempt"),
        ))

        if verbose:
            icon = "✓" if passed else ("⚠ SILENT-WRONG" if silent_wrong else "✗")
            note = f"  [{case.note}]" if case.note and not passed else ""
            print(f"  {icon} [{case.id}] {case.question[:70]}{note}")
            if not passed:
                print(f"        Error: {agent_error}" if agent_error else "")
                print(f"        Checks: {checks}")
                if silent_wrong:
                    print(f"        Silent wrong: {silent_reason}")
                print(f"        SQL: {agent_sql[:160]}")

    return results


def print_report(results: list[TestResult]) -> None:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    silent_wrong = sum(1 for r in results if r.silent_wrong)
    errors = sum(1 for r in results if r.error)
    avg_latency = sum(r.latency_ms for r in results) / max(total, 1)

    print("\n" + "=" * 65)
    print(f"  EVALUATION REPORT")
    print("=" * 65)
    print(f"  Total cases   : {total}")
    print(f"  Passed        : {passed}  ({100*passed//max(total,1)}%)")
    print(f"  Failed        : {total - passed}")
    print(f"  Silent wrong  : {silent_wrong}  ← queries ran but returned wrong data")
    print(f"  Execution err : {errors}")
    print(f"  Avg latency   : {avg_latency:.0f} ms")
    print("=" * 65)

    if silent_wrong:
        print("\n  ⚠ SILENT WRONG ANSWERS (most dangerous failure mode):")
        for r in results:
            if r.silent_wrong:
                print(f"    [{r.case_id}] {r.question}  — {r.silent_reason}")


def save_report(results: list[TestResult], path: str, db_name: str, label: str) -> None:
    """Write a JSON report so before/after runs can be compared."""
    from token_tracker import INPUT_COST_PER_MTOK, OUTPUT_COST_PER_MTOK
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    report = {
        "label": label,
        "db": db_name,
        "model": os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest"),
        "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": total,
        "passed": passed,
        "pass_rate_pct": round(100 * passed / max(total, 1), 1),
        "silent_wrong": sum(1 for r in results if r.silent_wrong),
        "errors": sum(1 for r in results if r.error),
        "zero_row_retries": sum(1 for r in results if r.zero_row_retried),
        "avg_latency_ms": round(sum(r.latency_ms for r in results) / max(total, 1)),
        "price_per_mtok": {"input": INPUT_COST_PER_MTOK, "output": OUTPUT_COST_PER_MTOK},
        "cases": [
            {
                "id": r.case_id, "question": r.question, "passed": r.passed,
                "silent_wrong": r.silent_wrong, "silent_reason": r.silent_reason,
                "error": r.error, "checks": r.checks, "agent_sql": r.agent_sql,
                "agent_rows": len(r.agent_result), "expected_rows": len(r.ground_truth_result),
                "zero_row_retried": r.zero_row_retried, "latency_ms": round(r.latency_ms),
            }
            for r in results
        ],
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(report, indent=2, default=str))
    print(f"\n  Saved report → {path}")


def print_ambiguity_demo(engine) -> None:
    print("\n" + "=" * 65)
    print("  AMBIGUITY DEMO (Gap 7)")
    print("=" * 65)
    for case in AMBIGUITY_CASES:
        print(f"\n  Question: '{case['question']}'")
        print(f"  Why it matters: {case['why_it_matters']}")

        # Run both interpretations and show the difference
        try:
            res_a = _run_sql(engine, case["sql_a"])
            res_b = _run_sql(engine, case["sql_b"])

            val_a = list(res_a[0].values())[0] if res_a else "N/A"
            val_b = list(res_b[0].values())[0] if res_b else "N/A"

            print(f"  Interpretation A: {case['interpretation_a']}")
            print(f"    → Result: {val_a}")
            print(f"  Interpretation B: {case['interpretation_b']}")
            print(f"    → Result: {val_b}")

            if isinstance(val_a, (int, float)) and isinstance(val_b, (int, float)) and val_a:
                diff_pct = abs(val_a - val_b) / abs(val_a) * 100
                print(f"  ⚠ Difference: {diff_pct:.1f}%  ← this is how wrong numbers stay silent")
        except Exception as e:
            print(f"  (Could not run demo: {e})")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate text-to-SQL agent")
    parser.add_argument("--db", default="business", choices=["business", "enterprise"])
    parser.add_argument("--show-failures-only", action="store_true")
    parser.add_argument("--ambiguity-demo", action="store_true", default=True)
    parser.add_argument("--save", help="write a JSON report here, e.g. results/after.json")
    parser.add_argument("--label", default="", help="name for this run in the saved report")
    parser.add_argument("--sleep", type=float, default=0.0,
                        help="seconds to wait between cases (avoids Gemini free-tier rate limits)")
    parser.add_argument("--keep-cache", action="store_true",
                        help="don't clear the query cache first (default: clear it)")
    args = parser.parse_args()

    db_path = "business.db" if args.db == "business" else "enterprise.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    # Import agent — must be done after engine setup to avoid circular imports
    try:
        from agent import run_agent
    except ImportError as e:
        print(f"Could not import agent: {e}")
        sys.exit(1)

    if not args.keep_cache:
        from query_cache import clear_all
        clear_all()   # otherwise repeat runs replay cached SQL and hide changes

    cases = CASES_BY_DB[args.db]

    def _agent(q: str) -> dict:
        out = run_agent(q, db_name=args.db)
        if args.sleep:
            time.sleep(args.sleep)
        return out

    print(f"\nRunning {len(cases)} cases on {db_path}...")
    print("-" * 65)

    results = evaluate(
        agent_fn=_agent,
        cases=cases,
        engine=engine,
        verbose=not args.show_failures_only,
    )

    if args.show_failures_only:
        failed = [r for r in results if not r.passed]
        for r in failed:
            print(f"✗ [{r.case_id}] {r.question}")
            print(f"  Error:   {r.error}")
            print(f"  Checks:  {r.checks}")
            print(f"  SQL:     {r.agent_sql[:120]}")

    print_report(results)

    if args.save:
        save_report(results, args.save, args.db, args.label)

    if args.ambiguity_demo and args.db == "business":
        print_ambiguity_demo(engine)
