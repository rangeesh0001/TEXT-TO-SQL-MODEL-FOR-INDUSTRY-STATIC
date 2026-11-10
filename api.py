"""
api.py — FastAPI backend.

New endpoints vs demo version:
  POST /query           — unchanged signature, richer response
  GET  /health          — now includes schema cache age
  GET  /schema          — show filtered or full schema
  POST /query/export    — Gap 9: return results as CSV or JSON for BI tools
  GET  /cache/stats     — Gap 4: query cache statistics
  DELETE /cache         — flush the query cache
  GET  /cost/report     — Gap 8: token usage and cost breakdown
  POST /evaluate        — Gap 6: run the eval harness (test mode only)
  GET  /databases       — list available databases
"""

from __future__ import annotations

import csv
import io
import os

from dotenv import load_dotenv
load_dotenv(override=True)

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Any, Literal, Optional

import uvicorn

from database import (
    get_schema_string, execute_sql, get_table_row_counts,
    test_connection, schema_cache_age, invalidate_schema_cache,
    set_active_db, _DB_CONFIGS, get_engine,
)
from agent import run_agent
from query_cache import stats as cache_stats, clear_all as cache_clear
from token_tracker import get_cost_report

app = FastAPI(
    title="Text-to-SQL Agent API",
    description="Production-grade natural language to SQL agent — LangGraph + Gemini",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    question: str
    db_name: str = "business"

    model_config = {
        "json_schema_extra": {
            "example": {
                "question": "Which product category had the highest revenue last quarter?",
                "db_name": "business",
            }
        }
    }


class QueryResponse(BaseModel):
    question: str
    db_name: str
    sql_query: str
    sql_result: list[Any]
    row_count: int
    summary: str
    error: Optional[str] = None
    retry_count: int = 0
    cached: bool = False
    is_ambiguous: bool = False
    ambiguity_note: str = ""
    validation_warning: str = ""
    relevant_tables: list[str] = []
    schema_was_cached: bool = False
    token_usage: dict = {}
    latency_ms: float = 0.0
    zero_row_note: str = ""


class ExportRequest(BaseModel):
    question: str
    db_name: str = "business"
    format: Literal["json", "csv"] = "json"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/query", response_model=QueryResponse)
async def query_database(request: QueryRequest):
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
    try:
        result = run_agent(request.question.strip(), db_name=request.db_name)
        return QueryResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent execution failed: {e}")


@app.post("/query/export")
async def export_query(request: ExportRequest):
    """
    Gap 9: BI tool integration — return results as CSV or JSON.

    Use from Metabase, Tableau, or any HTTP data source:
      curl -X POST .../query/export -d '{"question":"...", "format":"csv"}'
    """
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")

    result = run_agent(request.question.strip(), db_name=request.db_name)
    rows = result.get("sql_result", [])

    if request.format == "csv":
        if not rows:
            csv_content = "no_results\n"
        else:
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
            csv_content = buf.getvalue()

        return StreamingResponse(
            io.BytesIO(csv_content.encode()),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="query_result.csv"'},
        )

    # JSON — BI tools that speak REST (Metabase custom connector, Grafana JSON)
    return {
        "question": result["question"],
        "sql": result["sql_query"],
        "columns": list(rows[0].keys()) if rows else [],
        "rows": rows,
        "row_count": len(rows),
        "summary": result["summary"],
        "cached": result["cached"],
        "validation_warning": result["validation_warning"],
    }


@app.get("/health")
async def health_check(db_name: str = "business"):
    if not test_connection(db_name):
        raise HTTPException(status_code=503, detail=f"Cannot connect to {db_name}")

    table_counts = get_table_row_counts(db_name)
    age = schema_cache_age(db_name)

    return {
        "status": "healthy",
        "database": db_name,
        "table_count": len(table_counts),
        "table_row_counts": table_counts,
        "schema_cache_age_seconds": round(age, 1) if age is not None else None,
        "schema_cached": age is not None,
    }


@app.get("/schema")
async def get_schema(
    db_name: str = "business",
    filtered_for: Optional[str] = Query(None, description="Question to filter schema for"),
    top_n: int = Query(12, description="Max tables when filtering"),
):
    try:
        if filtered_for:
            from schema_index import SchemaIndex
            from database import get_engine
            eng = get_engine(db_name)
            idx = SchemaIndex.build(eng, db_name)
            tables = idx.search(filtered_for, top_n=top_n)
            schema = idx.to_prompt_schema(tables)
            return {
                "db_name": db_name,
                "filtered_for": filtered_for,
                "tables_shown": len(tables),
                "total_tables": len(idx.table_names()),
                "schema": schema,
            }
        schema = get_schema_string(db_name)
        return {"db_name": db_name, "schema": schema}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/cache/stats")
async def query_cache_stats():
    """Gap 4: query memory statistics."""
    return cache_stats()


@app.delete("/cache")
async def flush_cache():
    """Wipe the query cache (useful after schema changes)."""
    deleted = cache_clear()
    return {"deleted": deleted, "message": "Cache cleared"}


@app.get("/cost/report")
async def cost_report():
    """Gap 8: token usage and cost breakdown across all queries."""
    return get_cost_report()


@app.get("/databases")
async def list_databases():
    """List available databases and their connection status."""
    out = {}
    for name in _DB_CONFIGS:
        ok = test_connection(name)
        out[name] = {"available": ok}
        if ok:
            counts = get_table_row_counts(name)
            out[name]["table_count"] = len(counts)
    return out


@app.post("/evaluate")
async def run_evaluation(db_name: str = "business"):
    """
    Gap 6: Run the evaluation harness and return a JSON report.
    Only suitable for test/staging — do not expose in production.
    """
    try:
        from sqlalchemy import create_engine as _ce
        from eval_harness import CASES_BY_DB, evaluate
        from query_cache import clear_all
        if db_name not in CASES_BY_DB:
            raise HTTPException(status_code=400, detail=f"No eval cases for {db_name!r}")
        clear_all()   # cached answers would hide regressions

        eng = _ce(f"sqlite:///{db_name}.db", connect_args={"check_same_thread": False})

        def _agent(q: str) -> dict:
            return run_agent(q, db_name=db_name)

        results = evaluate(agent_fn=_agent, cases=CASES_BY_DB[db_name], engine=eng, verbose=False)
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        return {
            "total": total,
            "passed": passed,
            "pass_rate_pct": round(100 * passed / max(total, 1), 1),
            "silent_wrong": sum(1 for r in results if r.silent_wrong),
            "errors": sum(1 for r in results if r.error),
            "cases": [
                {
                    "id": r.case_id,
                    "question": r.question,
                    "passed": r.passed,
                    "silent_wrong": r.silent_wrong,
                    "silent_reason": r.silent_reason,
                    "error": r.error,
                    "checks": r.checks,
                    "latency_ms": round(r.latency_ms),
                }
                for r in results
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tables")
async def list_tables(db_name: str = "business"):
    """
    Database Browser — Gap: show real data, not just schema.
    Returns all tables with row counts and a 5-row preview.
    """
    try:
        from sqlalchemy import inspect as sa_inspect
        eng = get_engine(db_name)
        inspector = sa_inspect(eng)
        tables = []
        for tname in inspector.get_table_names():
            try:
                rows = execute_sql(f"SELECT * FROM {tname} LIMIT 5", db_name)
                count_row = execute_sql(f"SELECT COUNT(*) AS cnt FROM {tname}", db_name)
                total = count_row[0]["cnt"] if count_row else 0
                cols = list(rows[0].keys()) if rows else [
                    c["name"] for c in inspector.get_columns(tname)
                ]
                tables.append({
                    "table_name": tname,
                    "row_count": total,
                    "columns": cols,
                    "preview": rows,
                })
            except Exception as e:
                tables.append({"table_name": tname, "row_count": -1, "columns": [], "preview": [], "error": str(e)})
        return {"db_name": db_name, "table_count": len(tables), "tables": tables}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tables/{table_name}")
async def get_table_data(
    table_name: str,
    db_name: str = "business",
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """
    Return paginated rows from any table — for the DB browser UI.
    """
    from sql_guard import check_sql
    safe_query = f"SELECT * FROM {table_name} LIMIT {limit} OFFSET {offset}"
    guard = check_sql(safe_query)
    if not guard:
        raise HTTPException(status_code=400, detail=guard.reason)
    try:
        rows = execute_sql(safe_query, db_name)
        count_rows = execute_sql(f"SELECT COUNT(*) AS cnt FROM {table_name}", db_name)
        total = count_rows[0]["cnt"] if count_rows else 0
        cols = list(rows[0].keys()) if rows else []
        return {
            "db_name": db_name,
            "table_name": table_name,
            "total_rows": total,
            "offset": offset,
            "limit": limit,
            "columns": cols,
            "rows": rows,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not query table '{table_name}': {e}")


@app.get("/")
async def root():
    return {
        "message": "Text-to-SQL Agent API v2",
        "docs": "/docs",
        "endpoints": {
            "query": "POST /query",
            "export": "POST /query/export  (CSV or JSON for BI tools)",
            "health": "GET /health",
            "schema": "GET /schema?db_name=enterprise&filtered_for=<question>",
            "cache_stats": "GET /cache/stats",
            "cost_report": "GET /cost/report",
            "databases": "GET /databases",
            "evaluate": "POST /evaluate",
        },
    }


if __name__ == "__main__":
    uvicorn.run("api:app", host="0.0.0.0", port=8004, reload=True, log_level="info")
