# Production Text-to-SQL AI Agent 🤖

A **production-grade Text-to-SQL AI agent** built with **LangGraph**, **Claude Haiku**, and **FastAPI**.
Goes far beyond demo agents — handles real enterprise schemas, prevents SQL injection, caches queries,
tracks costs, validates results, and benchmarks accuracy.

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-latest-green)
![Claude Haiku](https://img.shields.io/badge/Claude-Haiku_4.5-orange)
![FastAPI](https://img.shields.io/badge/FastAPI-latest-teal)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## 📺 Full Video Walkthrough

> [Watch on YouTube →][https://www.youtube.com/@agenticdatalab ]
> Complete technical walkthrough — every file, every prompt, every design decision explained.

---

## 🚀 What This Agent Does

Most text-to-SQL demos break on real databases. This one doesn't. Here's why:

| Problem | Solution in this repo |
|---|---|
| 80-table enterprise schema overwhelms the LLM | `schema_index.py` — keyword scoring filters to top 12 relevant tables |
| Cryptic column names (`cust_nm`, `ttl_amt`) cause hallucination | 70-entry abbreviation map translates column names before the LLM sees them |
| Agent forgets `WHERE status = 'completed'`, returns wrong revenue | `validate_result` node — second LLM pass checks result plausibility |
| "What is total revenue?" — ambiguous, gets answered silently wrong | `disambiguate` node — documents the assumption made before generating SQL |
| LLM writes `DELETE FROM customers` | `sql_guard.py` — deterministic SELECT-only enforcement |
| Same question costs full LLM spend every time | `query_cache.py` — semantic fingerprint cache skips LLM on repeat queries |
| No visibility into cost or token usage | `token_tracker.py` — per-node token tracking with prompt cache savings |
| No way to know if a prompt change improved or regressed accuracy | `eval_harness.py` — 10-case benchmark with silent-wrong-answer detection |

---

## 🏗️ Architecture
User Question
→ cache_check       ─── (hit) ──────────────────────────→ END
→ schema_filter     ← keyword scoring + alias translation
→ disambiguate      ← auto-resolves ambiguous phrasing
→ generate_sql      ← Claude Haiku with schema context
→ sql_guard         ─── (blocked) ──→ summarize → END
→ execute_sql
↕ (retry up to 2× with error feedback)
→ validate_result   ← plausibility check
→ summarize
→ cache_store → END
---

## 📁 Project Structure
├── agent.py              # LangGraph graph — all 9 nodes, 4 conditional edges
├── schema_index.py       # Schema filtering + enterprise column alias translation
├── sql_guard.py          # Deterministic SELECT-only SQL safety enforcement
├── query_cache.py        # Semantic fingerprint query cache (SQLite-backed)
├── token_tracker.py      # Per-query token usage + prompt cache cost analysis
├── eval_harness.py       # Structured benchmark with silent-wrong-answer tests
├── database.py           # Multi-DB engine registry + schema string cache
├── api.py                # FastAPI — 10 endpoints including CSV/JSON export
├── gradio_ui.py          # 7-tab Gradio UI (chat, schema, cache, cost, eval...)
├── seed_db.py            # Seeds business.db (clean schema — customers/orders)
├── ugly_schema_seed.py   # Seeds enterprise.db (25+ tables, cryptic names)
├── requirements.txt
├── .env.example
└── README.md

---

## ⚡ Quickstart

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/text-to-sql-agent.git
cd text-to-sql-agent
pip install -r requirements.txt
```

### 2. Set your API key

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

### 3. Seed the databases

```bash
# Clean schema (customers, products, orders — 4 tables)
python seed_db.py

# Enterprise schema (25+ tables, cryptic column names — the hard one)
python ugly_schema_seed.py
```

### 4. Run the agent

**Option A — Gradio UI (recommended for demo)**
```bash
python api.py          # terminal 1 — start FastAPI backend on :8004
python gradio_ui.py    # terminal 2 — start Gradio UI
```
Open http://localhost:7860

**Option B — FastAPI only**
```bash
python api.py
# Docs at http://localhost:8004/docs
```

**Option C — Direct Python**
```python
from agent import run_agent
result = run_agent("Which product category had the highest revenue last quarter?")
print(result["summary"])
print(result["sql_query"])
print(f"Cost: ${result['token_usage']}")
```

---

## 🔌 API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| POST | `/query` | Main agent endpoint — returns SQL, summary, token usage, warnings |
| POST | `/query/export` | Returns results as CSV or JSON (BI tool integration) |
| GET | `/schema` | View schema, optionally filtered for a specific question |
| GET | `/cache/stats` | Query cache statistics and estimated savings |
| DELETE | `/cache` | Flush the query cache |
| GET | `/cost/report` | Token usage and cost breakdown across all queries |
| GET | `/databases` | List available databases and connection status |
| POST | `/evaluate` | Run the eval harness and return JSON report |
| GET | `/health` | Health check with schema cache age |
| GET | `/tables` | DB browser — all tables with row counts and previews |

---

## 🧠 Key Design Decisions Explained

### Schema Filtering (schema_index.py)
Sending 80 tables to an LLM causes hallucination and burns tokens. The `SchemaIndex` scores every table against the question using keyword overlap + fuzzy similarity — no vector database, no embedding API, just Python's standard library. Only the top 12 most relevant tables get passed to the LLM.

### Column Alias Translation
Enterprise databases have columns like `cust_nm`, `ttl_amt`, `ord_hdr`. The agent ships with a 70-entry abbreviation map that translates these into human-readable names. `ttl_amt` becomes "total amount (ttl_amt)" in the prompt — the LLM sees the meaning AND the raw name it needs to write correct SQL.

### Semantic Query Cache
Not an exact-string cache. The fingerprinting algorithm strips stopwords, removes punctuation, lowercases everything, and **sorts tokens alphabetically before hashing**. "Which product category had the highest revenue last quarter?" and "what product category has highest revenue in the last quarter" resolve to the same hash — cache hit.

### Deterministic SQL Guard
The LLM is told to only write SELECT queries. The guard enforces this with code, not trust. It strips SQL comments first (so `--DROP` tricks don't bypass the check), checks the first token, detects multi-statement injection (`SELECT 1; DROP TABLE`), and scans for banned keywords anywhere in the cleaned query.

### Silent Wrong Answer Detection
This is the hardest failure mode — SQL executes without error, returns plausible data, but is wrong. The eval harness defines a `silent_wrong_sql` for each test case: a plausible-but-incorrect variant. If the agent's output matches the wrong query's result instead of the ground truth, it's flagged — even though no exception was raised.

---

## 📊 Running the Eval Harness

```bash
python eval_harness.py

# Output:
# ✓ [B01] How many customers are in the database?
# ✓ [B02] What are the top 3 product categories by total revenue?
# ⚠ SILENT-WRONG [B02] What are the top 3... [Revenue includes cancelled orders]
# ✓ [B03] Who are the top 5 customers by total spending?
# ...
#
# =================================================================
#   EVALUATION REPORT
# =================================================================
#   Total cases   : 10
#   Passed        : 8  (80%)
#   Silent wrong  : 1  ← queries ran but returned wrong data
#   Avg latency   : 1,847 ms
# =================================================================
```

---

## 💰 Token Cost Tracking

```bash
# After running some queries:
curl http://localhost:8004/cost/report

# Returns:
# {
#   "total_queries": 47,
#   "total_cost_usd": 0.0183,
#   "avg_cost_per_query_usd": 0.000389,
#   "cache_hit_rate_pct": 34.1,
#   "total_cache_savings_usd": 0.0062
# }
```

---

## 🛠️ Tech Stack

- **LangGraph** — agent graph with conditional routing and retry loops
- **LangChain Anthropic** — Claude Haiku integration
- **Claude Haiku 4.5** — SQL generation, disambiguation, validation, summarization
- **SQLAlchemy** — multi-database engine management
- **FastAPI** — production API layer with Pydantic response models
- **Gradio** — 7-tab demo UI
- **SQLite** — business.db, enterprise.db, query_cache.db, token_log.db
- **Python standard library** — `difflib`, `hashlib`, `re` for schema scoring and caching

---

## 📦 Requirements
fastapi
uvicorn
langgraph
langchain
langchain-anthropic
langchain-community
anthropic
sqlalchemy
gradio
python-dotenv
pydantic
aiosqlite
tabulate
httpx

---

## 🤝 Contributing

PRs welcome. If you find a question type the agent gets wrong, add it to `eval_harness.py` as a new `EvalCase` — that's the best way to improve robustness.

---

## 📄 License

MIT

---

## 🔗 Related

If you found this useful, check out my other AI engineering projects on this channel.
