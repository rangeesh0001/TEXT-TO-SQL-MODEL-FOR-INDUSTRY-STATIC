"""
agent.py — Production LangGraph text-to-SQL agent.

New nodes vs the demo version:
  cache_check    → Gap 4: query memory (skip LLM on repeat questions)
  schema_filter  → Gap 1: select relevant tables from large/ugly schemas
  disambiguate   → Gap 7: detect ambiguous questions, document assumption
  generate_sql   → (existing, updated for prompt caching)
  sql_guard      → Gap 5: block non-SELECT before execution
  execute_sql    → (existing)
  validate_result→ Gap 3: detect silently wrong numbers
  summarize      → (existing, updated with warnings + token tracking)
  cache_store    → Gap 4: persist result to cache

Graph flow:
  question
    → cache_check ──(hit)──────────────────────────────→ END
    → schema_filter
    → disambiguate
    → generate_sql
    → sql_guard ──(blocked)──→ summarize → END
    → execute_sql
    ↕ (retry up to 2×)
    → validate_result
    → summarize
    → cache_store → END
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from typing_extensions import TypedDict

from database import get_schema_string, execute_sql, get_engine
from schema_index import SchemaIndex
from sql_guard import check_sql
from query_cache import get as cache_get, put as cache_put
from token_tracker import QueryUsage, PhaseUsage, extract_usage, log_usage

load_dotenv(override=True)

llm = ChatGoogleGenerativeAI(
    model="gemini-flash-lite-latest",
    google_api_key=os.getenv("GOOGLE_API_KEY"),
    max_tokens=1024,
    temperature=0,
)

# How many tables to send to the LLM for large schemas
SCHEMA_FILTER_TOP_N = int(os.getenv("SCHEMA_FILTER_TOP_N", "12"))

# Ambiguous phrases that trigger the disambiguation node
_AMBIGUOUS_PHRASES = [
    ("revenue", ["does this include cancelled orders?", "Should it include all statuses or completed only?"]),
    ("top customers", ["by total spend or by order count?"]),
    ("last quarter", ["calendar quarter or last 90 days?"]),
    ("best", ["by revenue or by volume?"]),
    ("sales", ["revenue amount or number of orders?"]),
]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    question: str
    db_name: str                 # which database to query
    schema: str                  # filtered schema sent to LLM
    full_schema: str             # complete schema (for fallback)
    relevant_table_names: list   # from schema_index
    sql_query: str
    sql_result: list
    summary: str
    error: str
    retry_count: int
    # Gap 4
    cached: bool
    cache_key: str
    # Gap 7
    is_ambiguous: bool
    ambiguity_note: str          # assumption the agent made
    # Gap 3
    validation_warning: str
    # Gap 8
    schema_was_cached: bool
    token_usage: dict
    latency_ms: float


def _initial_state(question: str, db_name: str = "business") -> AgentState:
    return AgentState(
        question=question,
        db_name=db_name,
        schema="",
        full_schema="",
        relevant_table_names=[],
        sql_query="",
        sql_result=[],
        summary="",
        error="",
        retry_count=0,
        cached=False,
        cache_key="",
        is_ambiguous=False,
        ambiguity_note="",
        validation_warning="",
        schema_was_cached=False,
        token_usage={},
        latency_ms=0.0,
    )


# ---------------------------------------------------------------------------
# Node 1: cache_check
# ---------------------------------------------------------------------------

def cache_check_node(state: AgentState) -> AgentState:
    entry = cache_get(state["question"])
    if entry is None:
        return state
    # Re-execute the cached SQL so callers always get fresh result rows
    # (saves all LLM calls — only the DB query runs again)
    try:
        fresh_rows = execute_sql(entry.sql_query, state["db_name"])
    except Exception:
        fresh_rows = []
    return {
        **state,
        "cached": True,
        "sql_query": entry.sql_query,
        "sql_result": fresh_rows,
        "summary": entry.summary,
        "error": "",
    }


def _should_use_cache(state: AgentState) -> str:
    return "done" if state["cached"] else "schema_filter"


# ---------------------------------------------------------------------------
# Node 2: schema_filter — Gap 1
# ---------------------------------------------------------------------------

def schema_filter_node(state: AgentState) -> AgentState:
    from database import schema_cache_age
    db_name = state["db_name"]
    t0 = time.time()

    full_schema = get_schema_string(db_name)
    cache_age = schema_cache_age(db_name)
    was_cached = cache_age is not None and cache_age < 290  # < TTL

    # Count tables in full schema
    table_count = full_schema.count("Table:")

    if table_count <= 8:
        # Small schema: send everything
        filtered_schema = full_schema
        relevant_names = []
    else:
        # Large schema: filter to top-N relevant tables
        eng = get_engine(db_name)
        idx = SchemaIndex.build(eng, db_name)
        relevant_tables = idx.search(state["question"], top_n=SCHEMA_FILTER_TOP_N)
        filtered_schema = idx.to_prompt_schema(relevant_tables)
        relevant_names = [t.table_name for t in relevant_tables]

    return {
        **state,
        "schema": filtered_schema,
        "full_schema": full_schema,
        "relevant_table_names": relevant_names,
        "schema_was_cached": was_cached,
    }


# ---------------------------------------------------------------------------
# Node 3: disambiguate — Gap 7
# ---------------------------------------------------------------------------

def disambiguate_node(state: AgentState) -> AgentState:
    q = state["question"].lower()
    notes = []
    for phrase, hints in _AMBIGUOUS_PHRASES:
        if phrase in q:
            notes.extend(hints)

    if not notes:
        return state

    # Instead of blocking on user input we document the assumption automatically.
    # In the video this is where you show the agent "thinking out loud".
    assumption_prompt = f"""The question "{state['question']}" is ambiguous.
Potential ambiguities: {'; '.join(notes)}
Choose the most conservative business-safe interpretation and state your assumption in one sentence."""

    messages = [
        SystemMessage(content="You are a precise data analyst. State one assumption to resolve the ambiguity."),
        HumanMessage(content=assumption_prompt),
    ]
    t0 = time.time()
    response = llm.invoke(messages)
    assumption = response.text.strip()

    usage = state.get("token_usage", {})
    in_t, out_t, cr_t, cw_t = extract_usage(response)
    usage["disambiguate"] = {"input": in_t, "output": out_t, "cache_read": cr_t}

    return {
        **state,
        "is_ambiguous": True,
        "ambiguity_note": assumption,
        "token_usage": usage,
    }


# ---------------------------------------------------------------------------
# Node 4: generate_sql
# ---------------------------------------------------------------------------

_SQL_SYSTEM = """You are an expert SQL query writer for SQLite databases.
Rules:
- Write ONLY a SELECT statement — never INSERT, UPDATE, DELETE, DROP, or ALTER
- Use SQLite syntax (strftime for dates, no DATE_TRUNC)
- SQLite's strftime does NOT support %q (quarter) — compute quarters manually using
  CASE WHEN strftime('%m', date) IN ('01','02','03') THEN 1
       WHEN strftime('%m', date) IN ('04','05','06') THEN 2
       WHEN strftime('%m', date) IN ('07','08','09') THEN 3
       ELSE 4 END, or use BETWEEN with explicit date literals
- Always alias tables when joining
- Return ONLY the raw SQL with no markdown, no code fences, no explanation
- Reference only tables and columns that exist in the provided schema
- For ambiguous questions, use the stated assumption
- When the question uses relative time terms (last quarter, last month, last year, etc.),
  anchor them to the Data date range provided, NOT to today's date.
  Example: if max date is 2024-06-29 (Q2 2024), then "last quarter" = Q1 2024 (Jan–Mar 2024),
  so use BETWEEN '2024-01-01' AND '2024-03-31' with explicit literals derived from the max date"""


def _get_date_range(db_name: str) -> str:
    """Return a note about the actual date range in the orders table, if available."""
    try:
        rows = execute_sql(
            "SELECT MIN(order_date) AS min_d, MAX(order_date) AS max_d FROM orders", db_name
        )
        if rows and rows[0].get("min_d"):
            from datetime import date
            import calendar
            min_d, max_d = rows[0]["min_d"], rows[0]["max_d"]
            # Derive last-quarter date range from max date
            max_date = date.fromisoformat(max_d)
            q = (max_date.month - 1) // 3  # current quarter index (0-based)
            if q == 0:
                lq_start = date(max_date.year - 1, 10, 1)
                lq_end   = date(max_date.year - 1, 12, 31)
            else:
                lq_month_start = (q - 1) * 3 + 1
                lq_start = date(max_date.year, lq_month_start, 1)
                lq_end_month = lq_month_start + 2
                lq_end = date(max_date.year, lq_end_month,
                              calendar.monthrange(max_date.year, lq_end_month)[1])
            return (
                f"Data date range: {min_d} to {max_d}. "
                f"Treat {max_d} as 'today'. "
                f"'Last quarter' = {lq_start} to {lq_end}. "
                f"Use BETWEEN '{lq_start}' AND '{lq_end}' for last-quarter filters."
            )
    except Exception:
        pass
    return ""


def generate_sql_node(state: AgentState) -> AgentState:
    schema = state["schema"]
    question = state["question"]
    error = state.get("error", "")
    retry_count = state.get("retry_count", 0)
    ambiguity_note = state.get("ambiguity_note", "")

    assumption_block = f"\nAssumption: {ambiguity_note}\n" if ambiguity_note else ""
    date_range = _get_date_range(state["db_name"])
    date_block = f"\n{date_range}\n" if date_range else ""

    if error and retry_count > 0:
        user_prompt = f"""Schema:
{schema}
{date_block}
Question: {question}{assumption_block}

Previous SQL (failed):
{state.get('sql_query', '')}

Error:
{error}

Fix the SQL. Return only the corrected SELECT query:"""
    else:
        user_prompt = f"""Schema:
{schema}
{date_block}
Question: {question}{assumption_block}

Write a SQLite SELECT query:"""

    messages = [SystemMessage(content=_SQL_SYSTEM), HumanMessage(content=user_prompt)]
    response = llm.invoke(messages)
    sql = response.text.strip().replace("```sql", "").replace("```", "").strip()

    usage = state.get("token_usage", {})
    in_t, out_t, cr_t, cw_t = extract_usage(response)
    usage[f"generate_sql_attempt_{retry_count}"] = {
        "input": in_t, "output": out_t, "cache_read": cr_t,
    }

    return {**state, "sql_query": sql, "error": "", "sql_result": [], "token_usage": usage}


# ---------------------------------------------------------------------------
# Node 5: sql_guard — Gap 5
# ---------------------------------------------------------------------------

def sql_guard_node(state: AgentState) -> AgentState:
    result = check_sql(state["sql_query"])
    if result.allowed:
        return state
    return {
        **state,
        "error": f"SQL guard blocked query: {result.reason}",
        "sql_result": [],
    }


def _after_guard(state: AgentState) -> str:
    if state["error"]:
        return "blocked"
    return "execute_sql"


# ---------------------------------------------------------------------------
# Node 6: execute_sql
# ---------------------------------------------------------------------------

def execute_sql_node(state: AgentState) -> AgentState:
    sql = state["sql_query"]
    if not sql:
        return {**state, "error": "No SQL query was generated", "sql_result": []}
    try:
        results = execute_sql(sql, state["db_name"])
        return {**state, "sql_result": results, "error": ""}
    except Exception as e:
        return {**state, "sql_result": [], "error": str(e)}


def _after_execute(state: AgentState) -> str:
    error = state.get("error", "")
    retry = state.get("retry_count", 0)
    if error and retry < 2:
        return "handle_error"
    return "validate_result"


def handle_error_node(state: AgentState) -> AgentState:
    return {**state, "retry_count": state.get("retry_count", 0) + 1}


# ---------------------------------------------------------------------------
# Node 7: validate_result — Gap 3 (silent wrong numbers)
# ---------------------------------------------------------------------------

_VALIDATION_SYSTEM = """You are a data quality reviewer checking SQL results for plausibility.
Look for: impossible values (negative counts, revenue > 10× average),
empty results that seem wrong, or totals that are suspiciously round.
Reply with VALID if the result looks correct, or WARNING: <brief reason> if suspicious."""


def validate_result_node(state: AgentState) -> AgentState:
    result = state.get("sql_result", [])
    if not result:
        return {**state, "validation_warning": ""}

    preview = result[:5]
    messages = [
        SystemMessage(content=_VALIDATION_SYSTEM),
        HumanMessage(
            content=f"Question: {state['question']}\nSQL: {state['sql_query']}\n"
                    f"Result (first 5 rows): {preview}\nTotal rows: {len(result)}"
        ),
    ]
    response = llm.invoke(messages)
    verdict = response.text.strip()
    warning = "" if verdict.upper().startswith("VALID") else verdict

    usage = state.get("token_usage", {})
    in_t, out_t, cr_t, cw_t = extract_usage(response)
    usage["validate"] = {"input": in_t, "output": out_t, "cache_read": cr_t}

    return {**state, "validation_warning": warning, "token_usage": usage}


# ---------------------------------------------------------------------------
# Node 8: summarize
# ---------------------------------------------------------------------------

_SUMMARY_SYSTEM = """You are a data analyst summarising query results in plain English.
Rules:
- 2-3 sentences maximum
- Include specific numbers
- If there is a validation warning, mention it clearly as a caveat
- If there is an assumption note, include it so the reader knows what was assumed"""


def summarize_node(state: AgentState) -> AgentState:
    result = state.get("sql_result", [])
    error = state.get("error", "")
    warning = state.get("validation_warning", "")
    note = state.get("ambiguity_note", "")

    if result:
        preview = result[:10]
        result_str = str(preview)
        if len(result) > 10:
            result_str += f"\n... and {len(result) - 10} more rows"
    else:
        result_str = f"No results returned. Error: {error}" if error else "No results returned."

    caveat_block = f"\nValidation warning: {warning}" if warning else ""
    assumption_block = f"\nAssumption used: {note}" if note else ""

    user_prompt = (
        f"Question: {state['question']}\n"
        f"SQL: {state['sql_query']}\n"
        f"Rows returned: {len(result)}\n"
        f"Results: {result_str}"
        f"{caveat_block}{assumption_block}\n\n"
        "Provide a plain-English summary:"
    )
    messages = [SystemMessage(content=_SUMMARY_SYSTEM), HumanMessage(content=user_prompt)]
    response = llm.invoke(messages)
    summary = response.text.strip()

    usage = state.get("token_usage", {})
    in_t, out_t, cr_t, cw_t = extract_usage(response)
    usage["summarize"] = {"input": in_t, "output": out_t, "cache_read": cr_t}

    return {**state, "summary": summary, "token_usage": usage}


# ---------------------------------------------------------------------------
# Node 9: cache_store — Gap 4
# ---------------------------------------------------------------------------

def cache_store_node(state: AgentState) -> AgentState:
    if state.get("error") or not state.get("sql_query"):
        return state
    # Estimate total cost from token_usage
    total_input = sum(v.get("input", 0) for v in state["token_usage"].values())
    total_output = sum(v.get("output", 0) for v in state["token_usage"].values())
    cost = (total_input * 0.80 + total_output * 4.00) / 1_000_000

    cache_put(
        question=state["question"],
        sql_query=state["sql_query"],
        summary=state["summary"],
        row_count=len(state.get("sql_result", [])),
        token_cost_usd=cost,
    )

    # Persist to token log
    qu = QueryUsage(question=state["question"], schema_was_cached=state["schema_was_cached"])
    for phase_name, counts in state["token_usage"].items():
        qu.phases.append(PhaseUsage(
            phase=phase_name,
            input_tokens=counts.get("input", 0),
            output_tokens=counts.get("output", 0),
            cache_read_tokens=counts.get("cache_read", 0),
        ))
    log_usage(qu)

    return state


# ---------------------------------------------------------------------------
# Build graph
# ---------------------------------------------------------------------------

workflow = StateGraph(AgentState)

workflow.add_node("cache_check", cache_check_node)
workflow.add_node("schema_filter", schema_filter_node)
workflow.add_node("disambiguate", disambiguate_node)
workflow.add_node("generate_sql", generate_sql_node)
workflow.add_node("sql_guard", sql_guard_node)
workflow.add_node("execute_sql", execute_sql_node)
workflow.add_node("handle_error", handle_error_node)
workflow.add_node("validate_result", validate_result_node)
workflow.add_node("summarize", summarize_node)
workflow.add_node("cache_store", cache_store_node)

workflow.set_entry_point("cache_check")

workflow.add_conditional_edges(
    "cache_check",
    _should_use_cache,
    {"done": END, "schema_filter": "schema_filter"},
)

workflow.add_edge("schema_filter", "disambiguate")
workflow.add_edge("disambiguate", "generate_sql")
workflow.add_edge("generate_sql", "sql_guard")

workflow.add_conditional_edges(
    "sql_guard",
    _after_guard,
    {"blocked": "summarize", "execute_sql": "execute_sql"},
)

workflow.add_conditional_edges(
    "execute_sql",
    _after_execute,
    {"handle_error": "handle_error", "validate_result": "validate_result"},
)

workflow.add_edge("handle_error", "generate_sql")
workflow.add_edge("validate_result", "summarize")
workflow.add_edge("summarize", "cache_store")
workflow.add_edge("cache_store", END)

graph = workflow.compile()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_agent(question: str, db_name: str = "business") -> dict:
    t0 = time.time()
    state = _initial_state(question, db_name)
    final = graph.invoke(state)
    latency = round((time.time() - t0) * 1000)

    return {
        "question": final["question"],
        "db_name": final["db_name"],
        "sql_query": final["sql_query"],
        "sql_result": final["sql_result"],
        "row_count": len(final.get("sql_result") or []),
        "summary": final["summary"],
        "error": final.get("error") or None,
        "retry_count": final["retry_count"],
        "cached": final["cached"],
        "is_ambiguous": final["is_ambiguous"],
        "ambiguity_note": final["ambiguity_note"],
        "validation_warning": final["validation_warning"],
        "relevant_tables": final["relevant_table_names"],
        "schema_was_cached": final["schema_was_cached"],
        "token_usage": final["token_usage"],
        "latency_ms": latency,
    }


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    questions = [
        "Which product category had the highest revenue last quarter?",
        # Re-ask to demo cache hit:
        "Which product category had the highest revenue last quarter?",
        # Ambiguous question (Gap 7):
        "What is the total revenue?",
        # Security test (Gap 5):
        # (The guard node blocks this before DB access — for demo purposes we
        #  would inject a bad SQL; here we show the guard via sql_guard.py)
    ]
    for q in questions:
        print(f"\nQ: {q}")
        result = run_agent(q)
        print(f"  Cached:        {result['cached']}")
        print(f"  Ambiguous:     {result['is_ambiguous']} — {result['ambiguity_note']}")
        print(f"  SQL:           {result['sql_query'][:80]}")
        print(f"  Rows:          {result['row_count']}")
        print(f"  Warning:       {result['validation_warning']}")
        print(f"  Summary:       {result['summary'][:120]}")
        print(f"  Latency:       {result['latency_ms']} ms")
        if result["error"]:
            print(f"  Error:         {result['error']}")
