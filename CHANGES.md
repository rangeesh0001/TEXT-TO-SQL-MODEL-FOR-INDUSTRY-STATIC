# Changes from the original project

Original: [sweta2503's text-to-SQL agent](https://github.com/sweta2503) (MIT).
This file lists only what was changed in this version. Everything else
(the LangGraph pipeline, schema filtering, SQL guard, cache, token tracker,
validation node, FastAPI, Gradio UI and the 10 business.db test cases) is the
original author's work.

## 1. Switched the LLM from Claude Haiku to Google Gemini
- `agent.py`: `ChatAnthropic` → `ChatGoogleGenerativeAI`. The model is set by `GEMINI_MODEL` in `.env`.
- `response.content` → `response.text` in all 4 LLM calls. Gemini can return its reply in several parts, and `.text` always gives a plain string.
- `requirements.txt`, `.env.example` (`GOOGLE_API_KEY`) and the README were updated to match.
- UI timeout raised from 90s to 300s for the slower free tier.

## 2. Fixed silent 0-row answers on the enterprise database
**Problem:** enterprise.db stores codes (`COMP`, `GLD`, `NE`), but the model wrote
the words a person would use (`'completed'`, `'Gold'`, `'Northeast'`). The SQL ran
without error and returned 0 rows. For `COUNT`/`SUM` questions it returned a
confident **0**. The original validation step skipped empty results, so the
user got no warning.

**Fix, in three layers:**
1. **Prevent:** the schema shown to the model now lists the real values of every
   short code column, e.g. `sts_cd (VARCHAR(10)) -- values: 'CANC', 'COMP', 'PROC', 'SHIP'`
   (`database.py`, `schema_index.py`).
2. **Detect and retry:** new `filter_check.py` plus a `zero_row_check` node in `agent.py`.
   When a query returns 0 rows (or a single 0/NULL), the agent checks every filter
   value against the data without calling the LLM. If a value doesn't exist, it
   retries SQL generation once and tells the model exactly what was wrong and which
   values are allowed. If every value is real (e.g. no customers in Canada), it treats
   the result as a genuine empty answer and doesn't spend another LLM call.
3. **Warn:** `validate_result` now warns on empty results instead of passing them,
   and empty results are no longer cached.

## 3. Added an enterprise benchmark (`eval_harness.py`)
- 12 new cases (E01–E12) on the 26-table enterprise.db. They cover code-value filters,
  a lookup-table join, two-filter joins, last-quarter dates, a finance table, and
  one case where **0 rows is the correct answer** (E10).
- New check: the agent's numbers must match the ground-truth query's numbers.
- An unexpected empty result now counts as a **silent wrong** answer. The original
  only checked for silent wrong answers when the agent returned rows.
- `--db enterprise` now actually queries enterprise.db. Before this change it always
  queried business.db, and the ground-truth SQL crashed.
- The cache is cleared before each run. `--save` writes a JSON report, `--label`
  names the run, and `--sleep` adds pauses for free-tier rate limits.
- The API's `/evaluate` endpoint and the UI's Evaluate tab both support enterprise.

## 4. The query cache now separates databases (`query_cache.py`)
- The database name is part of the cache key. Before, asking the same question on
  enterprise reused the business SQL, which failed, and the error was swallowed,
  so the user got 0 rows plus an old summary.
- A cached query that no longer runs is now treated as a cache miss.

## 5. "Last quarter" now works on enterprise.db (`agent.py`)
- The date anchor was hard-coded to `orders.order_date`, which doesn't exist in
  enterprise.db. It now uses `ord_hdr.ord_dt` for enterprise.

## 6. Cost estimates use Gemini prices (`token_tracker.py`)
- Haiku's $0.80/$4.00 per million tokens was hard-coded in three files. There is
  now one `estimate_cost()` function, with prices set in `.env`. The defaults are
  the Gemini 3.5 Flash-Lite paid-tier list prices ($0.30 / $2.50). On the free tier,
  the real cost is $0.

## 7. UI
- New dark theme, a credit footer, a guard for empty tables in the DB browser, and
  an "auto-corrected" note in chat when the 0-row retry fixed a query.
