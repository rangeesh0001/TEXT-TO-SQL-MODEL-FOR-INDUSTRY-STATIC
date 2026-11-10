"""
gradio_ui.py — Production UI with tabs for all 9 gaps.

Tabs:
  🗃 DB Browser  — browse real table data (NEW: see exactly what's in each table)
  💬 Chat        — main query interface (with cost badge, ambiguity flag, warning)
  🗄 Schema      — live schema explorer with relevance filtering
  ⚡ Cache       — query memory stats + cache flush
  💰 Cost        — token usage and cost breakdown
  📊 Evaluate    — run the eval harness and show pass/fail table
  🔒 SQL Guard   — live demo of the SQL guard (Gap 5)
"""

import json
import httpx
import gradio as gr

from token_tracker import estimate_cost

API_BASE_URL = "http://localhost:8004"
TIMEOUT = 300.0


def _post(path: str, payload: dict) -> dict:
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.post(f"{API_BASE_URL}{path}", json=payload)
        r.raise_for_status()
        return r.json()


def _get(path: str, params: dict | None = None) -> dict | str:
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.get(f"{API_BASE_URL}{path}", params=params or {})
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Tab 1 — Chat
# ---------------------------------------------------------------------------

EXAMPLES = [
    "Which product category had the highest revenue last quarter?",
    "Who are the top 5 customers by total spending?",
    "What is the average order value by customer region?",
    "How many orders were placed each month in 2023?",
    "Which products have never been ordered?",
    "What percentage of orders are from Platinum tier customers?",
    "Show me the monthly revenue trend for Electronics.",
    "What is total revenue?",   # deliberately ambiguous — Gap 7 demo
]


def query_agent(question: str, db_name: str, chat_history: list):
    # Gradio 6 uses list of {role, content} dicts
    chat_history = chat_history or []
    if not question.strip():
        return chat_history, "", "No question provided.", "", "", ""

    # Add user message immediately
    chat_history = chat_history + [{"role": "user", "content": question}]

    try:
        data = _post("/query", {"question": question, "db_name": db_name})
    except httpx.ConnectError:
        msg = "❌ Cannot connect to API at localhost:8004. Make sure `python3 api.py` is running."
        chat_history = chat_history + [{"role": "assistant", "content": msg}]
        return chat_history, "", msg, "", "", ""
    except Exception as e:
        msg = f"❌ Error: {e}"
        chat_history = chat_history + [{"role": "assistant", "content": msg}]
        return chat_history, "", msg, "", "", ""

    summary       = data.get("summary", "")
    sql           = data.get("sql_query", "")
    rows          = data.get("row_count", 0)
    cached        = data.get("cached", False)
    ambiguous     = data.get("is_ambiguous", False)
    note          = data.get("ambiguity_note", "")
    warning       = data.get("validation_warning", "")
    error         = data.get("error")
    latency       = data.get("latency_ms", 0)
    schema_cached = data.get("schema_was_cached", False)
    token_usage   = data.get("token_usage", {})
    total_in      = sum(v.get("input", 0) for v in token_usage.values())
    total_out     = sum(v.get("output", 0) for v in token_usage.values())
    cost_usd      = estimate_cost(total_in, total_out)   # Gemini rates from .env

    # Build assistant message
    badges = []
    if cached:        badges.append("⚡ Cache hit")
    if schema_cached: badges.append("🗄 Schema cached")
    if ambiguous:     badges.append("❓ Ambiguous question")
    badge_line = "  ".join(badges)

    if error:
        bot = f"❌ **Error:** {error}\n\n{summary}"
    else:
        bot = f"✅ **Answer:** {summary}\n\n📊 *{rows} row(s)*"
    if badge_line:
        bot += f"\n\n{badge_line}"
    if ambiguous and note:
        bot += f"\n\n> 💡 **Assumption:** {note}"
    if warning:
        bot += f"\n\n> ⚠️ **Data quality warning:** {warning}"
    zero_note = data.get("zero_row_note", "")
    if zero_note.startswith("First attempt") and rows:
        bot += "\n\n> 🔁 **Auto-corrected:** the first query used filter values that don't exist in the data; fixed and re-run."
    bot += f"\n\n*Latency: {latency:.0f} ms  |  Cost: ${cost_usd:.5f}*"

    chat_history = chat_history + [{"role": "assistant", "content": bot}]

    sql_md   = f"```sql\n{sql}\n```" if sql else "No SQL generated."
    relevant = ", ".join(data.get("relevant_tables", [])) or "all (small schema)"

    return chat_history, "", sql_md, relevant, f"${cost_usd:.5f}", warning


def clear_chat():
    return [], "", "SQL will appear here.", "", "", ""


# ---------------------------------------------------------------------------
# Tab 2 — Schema explorer
# ---------------------------------------------------------------------------

def explore_schema(db_name: str, filter_question: str, top_n: int):
    params = {"db_name": db_name}
    if filter_question.strip():
        params["filtered_for"] = filter_question
        params["top_n"] = top_n
    try:
        data = _get("/schema", params)
    except Exception as e:
        return f"Error: {e}", ""

    schema = data.get("schema", "")
    info = ""
    if "tables_shown" in data:
        info = (f"Showing **{data['tables_shown']}** of **{data['total_tables']}** tables "
                f"(filtered for: *{data['filtered_for']}*)")
    else:
        info = f"Full schema ({schema.count('Table:')} tables)"
    return info, f"```\n{schema}\n```"


# ---------------------------------------------------------------------------
# Tab 3 — Cache
# ---------------------------------------------------------------------------

def get_cache_stats():
    try:
        stats = _get("/cache/stats")
        return json.dumps(stats, indent=2)
    except Exception as e:
        return f"Error: {e}"


def flush_cache():
    try:
        with httpx.Client(timeout=10) as c:
            r = c.delete(f"{API_BASE_URL}/cache")
            r.raise_for_status()
            return r.json().get("message", "Cleared")
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Tab 4 — Cost
# ---------------------------------------------------------------------------

def get_cost_report():
    try:
        data = _get("/cost/report")
        lines = []
        for k, v in data.items():
            label = k.replace("_", " ").title()
            if "usd" in k.lower():
                lines.append(f"**{label}**: ${v:.4f}")
            elif "pct" in k.lower():
                lines.append(f"**{label}**: {v}%")
            else:
                lines.append(f"**{label}**: {v:,}" if isinstance(v, int) else f"**{label}**: {v}")
        return "\n\n".join(lines) if lines else "No queries logged yet."
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Tab 5 — Evaluate
# ---------------------------------------------------------------------------

def run_eval(db_name: str):
    try:
        with httpx.Client(timeout=300) as c:
            r = c.post(f"{API_BASE_URL}/evaluate", params={"db_name": db_name})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return f"Error: {e}", []

    summary = (
        f"**Pass rate: {data['pass_rate_pct']}%** "
        f"({data['passed']}/{data['total']} cases)\n\n"
        f"Silent wrong answers: **{data['silent_wrong']}** "
        f"← queries that ran but returned wrong data\n\n"
        f"Execution errors: **{data['errors']}**"
    )
    rows = [
        [c["id"], c["question"][:60], "✓" if c["passed"] else ("⚠ SILENT" if c["silent_wrong"] else "✗"),
         c["error"] or "", c["latency_ms"]]
        for c in data["cases"]
    ]
    return summary, rows


# ---------------------------------------------------------------------------
# DB Browser helpers — show real table data visually
# ---------------------------------------------------------------------------

def get_db_overview(db_name: str):
    """Return summary cards: table name, row count, column count."""
    try:
        data = _get("/tables", {"db_name": db_name})
    except Exception as e:
        return f"❌ Error: {e}", []

    tables = data.get("tables", [])
    total_rows = sum(t.get("row_count", 0) for t in tables)

    info = (
        f"**{db_name}** database · "
        f"**{data['table_count']} tables** · "
        f"**{total_rows:,} total rows**"
    )

    # Build summary table: table name | rows | columns | sample columns
    summary_rows = []
    for t in tables:
        col_preview = ", ".join(t["columns"][:5])
        if len(t["columns"]) > 5:
            col_preview += f" … +{len(t['columns'])-5} more"
        summary_rows.append([
            t["table_name"],
            f"{t.get('row_count', 0):,}",
            str(len(t["columns"])),
            col_preview,
        ])

    return info, summary_rows


def get_table_names(db_name: str) -> list[str]:
    """Fetch table names for the dropdown."""
    try:
        data = _get("/tables", {"db_name": db_name})
        return [t["table_name"] for t in data.get("tables", [])]
    except Exception:
        return []


def browse_table(db_name: str, table_name: str, limit: int, offset: int):
    """Fetch paginated data from a table and return as dataframe-ready list."""
    if not table_name:
        return "Select a table above.", [], "", ""

    try:
        data = _get(
            f"/tables/{table_name}",
            {"db_name": db_name, "limit": int(limit), "offset": int(offset)},
        )
    except Exception as e:
        return f"❌ Error: {e}", [], "", ""

    rows = data.get("rows", [])
    cols = data.get("columns", [])
    total = data.get("total_rows", 0)
    page_info = (
        f"Showing rows **{offset + 1}–{min(offset + limit, total)}** "
        f"of **{total:,}** total · Table: **{table_name}**"
    )

    # Convert to list-of-lists for gr.Dataframe
    data_rows = [[str(r.get(c, "")) for c in cols] for r in rows]

    # Column stats line
    stats = f"Columns ({len(cols)}): " + " · ".join(
        f"`{c}`" for c in cols[:10]
    ) + (" …" if len(cols) > 10 else "")

    return page_info, data_rows, cols, stats


def refresh_table_dropdown(db_name: str):
    names = get_table_names(db_name)
    return gr.Dropdown(choices=names, value=names[0] if names else None)


# ---------------------------------------------------------------------------
# Tab 6 — Guard demo
# ---------------------------------------------------------------------------

def test_guard(sql_input: str):
    from sql_guard import check_sql
    result = check_sql(sql_input)
    if result.allowed:
        return "✅ **ALLOWED** — this query would execute"
    return f"🔒 **BLOCKED** — {result.reason}"


# ---------------------------------------------------------------------------
# Build UI
# ---------------------------------------------------------------------------

# "Midnight Ledger" — dark ink surfaces, emerald accent, mono for data.
# Same palette is applied to both light and dark modes so the look is
# consistent regardless of the viewer's OS setting.
_INK      = "#0b0f14"   # page background
_PANEL    = "#11171f"   # blocks / cards
_PANEL_2  = "#161e28"   # inputs, table rows
_LINE     = "#223040"   # borders
_TEXT     = "#e6edf3"
_MUTED    = "#8b9aab"
_ACCENT   = "#2dd4a7"   # emerald
_ACCENT_D = "#1fae89"
_WARN     = "#f5b14c"
_DANGER   = "#f0616d"

_THEME = gr.themes.Base(
    primary_hue=gr.themes.colors.emerald,
    secondary_hue=gr.themes.colors.slate,
    neutral_hue=gr.themes.colors.slate,
    radius_size=gr.themes.sizes.radius_md,
    font=[gr.themes.GoogleFont("IBM Plex Sans"), "system-ui", "sans-serif"],
    font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "ui-monospace", "monospace"],
)

_theme_vars = dict(
    body_background_fill=_INK,
    body_text_color=_TEXT,
    body_text_color_subdued=_MUTED,
    background_fill_primary=_PANEL,
    background_fill_secondary=_PANEL_2,
    block_background_fill=_PANEL,
    block_border_color=_LINE,
    block_border_width="1px",
    block_label_background_fill=_PANEL_2,
    block_label_text_color=_MUTED,
    block_title_text_color=_TEXT,
    border_color_primary=_LINE,
    border_color_accent=_ACCENT,
    color_accent=_ACCENT,
    color_accent_soft=_PANEL_2,
    input_background_fill=_PANEL_2,
    input_background_fill_focus=_PANEL_2,
    input_border_color=_LINE,
    input_border_color_focus=_ACCENT,
    input_placeholder_color=_MUTED,
    button_primary_background_fill=_ACCENT,
    button_primary_background_fill_hover=_ACCENT_D,
    button_primary_text_color=_INK,
    button_primary_border_color=_ACCENT,
    button_secondary_background_fill=_PANEL_2,
    button_secondary_background_fill_hover=_LINE,
    button_secondary_text_color=_TEXT,
    button_secondary_border_color=_LINE,
    button_cancel_background_fill=_DANGER,
    button_cancel_background_fill_hover="#d94d59",
    button_cancel_text_color=_INK,
    table_even_background_fill=_PANEL,
    table_odd_background_fill=_PANEL_2,
    table_border_color=_LINE,
    table_row_focus=_LINE,
    code_background_fill="#0a0e13",
    link_text_color=_ACCENT,
    link_text_color_hover=_ACCENT_D,
    slider_color=_ACCENT,
    checkbox_background_color_selected=_ACCENT,
    loader_color=_ACCENT,
    shadow_drop="none",
    shadow_drop_lg="none",
)
# Apply identical values to the *_dark variants (only where one exists)
import inspect as _inspect
_settable = set(_inspect.signature(_THEME.set).parameters)
_dark_vars = {f"{k}_dark": v for k, v in _theme_vars.items() if f"{k}_dark" in _settable}
_THEME = _THEME.set(**_theme_vars, **_dark_vars)

_CSS = f"""
    .gradio-container {{ max-width: 1320px !important; margin: auto; }}
    footer {{ display: none !important; }}

    /* Header */
    #app-header {{
        border: 1px solid {_LINE};
        background: linear-gradient(135deg, {_PANEL} 0%, #0f1a1a 100%);
        border-radius: 12px;
        padding: 22px 26px;
        margin-bottom: 8px;
        display: flex; align-items: center; justify-content: space-between; gap: 16px;
        flex-wrap: wrap;
    }}
    #app-header .title {{
        font-size: 1.6em; font-weight: 600; letter-spacing: -0.01em; margin: 0; color: {_TEXT};
    }}
    #app-header .title .prompt {{ color: {_ACCENT}; font-family: 'JetBrains Mono', monospace; }}
    #app-header .sub {{ color: {_MUTED}; margin: 6px 0 0; font-size: 0.98em; }}
    #app-header .chips {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    #app-header .chip {{
        font-family: 'JetBrains Mono', monospace; font-size: 0.78em;
        padding: 4px 10px; border-radius: 999px;
        border: 1px solid {_LINE}; color: {_MUTED}; background: {_PANEL_2};
    }}
    #app-header .chip.on {{ color: {_ACCENT}; border-color: {_ACCENT}33; }}

    /* Tabs */
    .tab-nav button, button[role="tab"] {{ color: {_MUTED} !important; font-weight: 500; }}
    .tab-nav button.selected, button[role="tab"][aria-selected="true"] {{
        color: {_ACCENT} !important; border-color: {_ACCENT} !important;
    }}

    /* Markdown headings inside tabs */
    .prose h3, .md h3 {{ color: {_TEXT}; font-weight: 600; }}
    .prose h4, .md h4 {{ color: {_MUTED}; font-weight: 500; text-transform: uppercase;
                         letter-spacing: 0.06em; font-size: 0.8em; }}
    .prose code, .md code {{ color: {_ACCENT}; background: {_PANEL_2}; }}
    .prose blockquote, .md blockquote {{ border-left: 3px solid {_WARN}; color: {_TEXT}; }}

    /* Chat bubbles */
    .message.user, [data-testid="user"] {{ background: {_PANEL_2} !important; border: 1px solid {_LINE} !important; }}
    .message.bot, [data-testid="bot"] {{ background: #0f1a17 !important; border: 1px solid {_ACCENT}33 !important; }}

    /* Footer credit */
    #app-credit {{ text-align: center; color: {_MUTED}; font-size: 0.82em; margin-top: 18px; }}
    #app-credit a {{ color: {_ACCENT}; text-decoration: none; }}
"""

with gr.Blocks(title="Text-to-SQL Agent — Rangeesh") as demo:

    gr.HTML("""
        <div id="app-header">
            <div>
                <p class="title"><span class="prompt">&gt;_</span> Text-to-SQL AI Agent</p>
                <p class="sub">Ask plain-English questions about your business data — get SQL, results and a summary.</p>
            </div>
            <div class="chips">
                <span class="chip on">Gemini</span>
                <span class="chip">LangGraph</span>
                <span class="chip">FastAPI</span>
                <span class="chip">SQLite</span>
            </div>
        </div>
    """)

    with gr.Tabs():

        # ------------------------------------------------------------------
        # Tab 0: DB Browser  ← NEW — show the real data first
        # ------------------------------------------------------------------
        with gr.Tab("🗃 DB Browser"):
            gr.Markdown(
                "### Live Database Browser\n"
                "See the **actual data** in both databases — clean 4-table `business.db` "
                "and the ugly 26-table `enterprise.db`. "
                "This is what the AI agent is querying against."
            )

            with gr.Row():
                dbr_db = gr.Dropdown(
                    choices=["business", "enterprise"],
                    value="business",
                    label="📂 Database",
                    scale=0,
                    min_width=180,
                )
                dbr_overview_btn = gr.Button("🔍 Load Overview", variant="primary", scale=0)
            dbr_overview_info = gr.Markdown("")

            # Overview table: all tables at a glance
            gr.Markdown("#### All Tables — Overview")
            dbr_overview_table = gr.Dataframe(
                headers=["Table Name", "Row Count", "# Columns", "Columns Preview"],
                datatype=["str", "str", "str", "str"],
                interactive=False,
                wrap=True,
                row_count=(10, "dynamic"),
            )

            gr.Markdown("---")
            gr.Markdown("#### Browse a Table — See Real Rows")

            with gr.Row():
                dbr_table_dd = gr.Dropdown(
                    choices=[],
                    label="📋 Select Table",
                    scale=2,
                    allow_custom_value=False,
                )
                dbr_limit = gr.Slider(5, 100, value=20, step=5, label="Rows per page", scale=1)
                dbr_offset = gr.Number(value=0, label="Start from row", precision=0, scale=1)
                dbr_load_btn = gr.Button("📥 Load Rows", variant="primary", scale=0)

            dbr_page_info = gr.Markdown("")
            dbr_col_stats = gr.Markdown("", elem_classes=["table-info"])

            dbr_data_table = gr.Dataframe(
                headers=["col_1", "col_2", "col_3"],
                datatype=["str", "str", "str"],
                interactive=False,
                wrap=False,
                row_count=(20, "dynamic"),
                max_height=420,
            )

            with gr.Row():
                dbr_prev_btn = gr.Button("◀ Previous page", size="sm", scale=0)
                dbr_next_btn = gr.Button("Next page ▶", size="sm", scale=0)

            # -- Quick-pick buttons for interesting tables --
            gr.Markdown("#### Quick picks — click to jump straight to a table")
            with gr.Row():
                for tbl, label in [
                    ("customers",  "👤 customers"),
                    ("orders",     "🛒 orders"),
                    ("order_items","📦 order_items"),
                    ("products",   "🏷 products"),
                ]:
                    b = gr.Button(label, size="sm", variant="secondary")
                    b.click(
                        fn=lambda t=tbl: t,
                        outputs=[dbr_table_dd],
                    )

            gr.Markdown("#### Enterprise DB quick picks (ugly schema)")
            with gr.Row():
                for tbl, label in [
                    ("cust_mstr", "👤 cust_mstr"),
                    ("ord_hdr",   "🛒 ord_hdr"),
                    ("ord_dtl",   "📦 ord_dtl"),
                    ("prd_mstr",  "🏷 prd_mstr"),
                    ("ar_hdr",    "💰 ar_hdr"),
                    ("gl_jrnl",   "📒 gl_jrnl"),
                ]:
                    b = gr.Button(label, size="sm", variant="secondary")
                    b.click(fn=lambda t=tbl: t, outputs=[dbr_table_dd])

            # -- Wire events --

            def _overview_and_populate_dd(db_name):
                info, rows = get_db_overview(db_name)
                names = get_table_names(db_name)
                dd_update = gr.Dropdown(choices=names, value=names[0] if names else None)
                return info, rows, dd_update

            dbr_overview_btn.click(
                fn=_overview_and_populate_dd,
                inputs=[dbr_db],
                outputs=[dbr_overview_info, dbr_overview_table, dbr_table_dd],
                show_progress=True,
            )

            # Also load overview on tab load
            demo.load(
                fn=_overview_and_populate_dd,
                inputs=[dbr_db],
                outputs=[dbr_overview_info, dbr_overview_table, dbr_table_dd],
            )

            def _load_table(db_name, table_name, limit, offset):
                page_info, data_rows, cols, stats = browse_table(
                    db_name, table_name, int(limit), int(offset)
                )
                if not cols:  # API offline / no table selected
                    return page_info, stats, gr.Dataframe(value=[["—"]], headers=["(no data)"])
                # Rebuild Dataframe with correct headers
                df_update = gr.Dataframe(
                    value=data_rows,
                    headers=cols,
                    datatype=["str"] * len(cols),
                    interactive=False,
                    wrap=False,
                    max_height=420,
                )
                return page_info, stats, df_update

            dbr_load_btn.click(
                fn=_load_table,
                inputs=[dbr_db, dbr_table_dd, dbr_limit, dbr_offset],
                outputs=[dbr_page_info, dbr_col_stats, dbr_data_table],
                show_progress=True,
            )

            # Auto-load when a table is selected from dropdown
            dbr_table_dd.change(
                fn=_load_table,
                inputs=[dbr_db, dbr_table_dd, dbr_limit, dbr_offset],
                outputs=[dbr_page_info, dbr_col_stats, dbr_data_table],
                show_progress=True,
            )

            def _prev_page(offset, limit):
                return max(0, int(offset) - int(limit))

            def _next_page(offset, limit):
                return int(offset) + int(limit)

            dbr_prev_btn.click(fn=_prev_page, inputs=[dbr_offset, dbr_limit], outputs=[dbr_offset]).then(
                fn=_load_table,
                inputs=[dbr_db, dbr_table_dd, dbr_limit, dbr_offset],
                outputs=[dbr_page_info, dbr_col_stats, dbr_data_table],
            )
            dbr_next_btn.click(fn=_next_page, inputs=[dbr_offset, dbr_limit], outputs=[dbr_offset]).then(
                fn=_load_table,
                inputs=[dbr_db, dbr_table_dd, dbr_limit, dbr_offset],
                outputs=[dbr_page_info, dbr_col_stats, dbr_data_table],
            )

        # ------------------------------------------------------------------
        # Tab 1: Chat
        # ------------------------------------------------------------------
        with gr.Tab("💬 Chat"):
            with gr.Row():
                db_selector = gr.Dropdown(
                    choices=["business", "enterprise"],
                    value="business",
                    label="Database",
                    scale=0,
                    min_width=160,
                )
                health_md = gr.Markdown("")
                refresh_btn = gr.Button("🔄 Status", scale=0, size="sm")

            with gr.Row(equal_height=False):
                with gr.Column(scale=3):
                    chatbot = gr.Chatbot(label="Conversation", height=480)
                    with gr.Row():
                        q_input = gr.Textbox(
                            placeholder="Ask a business question...",
                            label="Question", lines=2, scale=5,
                        )
                        with gr.Column(scale=1, min_width=110):
                            ask_btn = gr.Button("🚀 Ask", variant="primary", size="lg")
                            clr_btn = gr.Button("🗑 Clear", variant="secondary", size="sm")

                with gr.Column(scale=2):
                    gr.Markdown("### Generated SQL")
                    sql_out = gr.Markdown("SQL will appear here.")

                    with gr.Row():
                        cost_badge = gr.Markdown("Cost: —")
                        warning_badge = gr.Markdown("")

                    gr.Markdown("**Relevant tables selected:**")
                    tables_out = gr.Markdown("—")

                    gr.Markdown("### Example Questions")
                    for ex in EXAMPLES:
                        b = gr.Button(f"📌 {ex[:55]}{'…' if len(ex) > 55 else ''}", size="sm", variant="secondary")
                        b.click(fn=lambda e=ex: e, outputs=[q_input])

            ask_btn.click(
                fn=query_agent,
                inputs=[q_input, db_selector, chatbot],
                outputs=[chatbot, q_input, sql_out, tables_out, cost_badge, warning_badge],
                show_progress=True,
            )
            q_input.submit(
                fn=query_agent,
                inputs=[q_input, db_selector, chatbot],
                outputs=[chatbot, q_input, sql_out, tables_out, cost_badge, warning_badge],
                show_progress=True,
            )
            clr_btn.click(fn=clear_chat, outputs=[chatbot, q_input, sql_out, tables_out, cost_badge, warning_badge])

            def check_health(db):
                try:
                    data = _get("/health", {"db_name": db})
                    cache_age = data.get("schema_cache_age_seconds")
                    age_str = f"Schema cache: {cache_age:.0f}s old" if cache_age is not None else "Schema: not cached"
                    return (f"✅ {db} · {data['table_count']} tables · {age_str}")
                except Exception as e:
                    return f"❌ API offline: {e}"

            refresh_btn.click(fn=check_health, inputs=[db_selector], outputs=[health_md])
            demo.load(fn=check_health, inputs=[db_selector], outputs=[health_md])

        # ------------------------------------------------------------------
        # Tab 2: Schema
        # ------------------------------------------------------------------
        with gr.Tab("🗄 Schema"):
            gr.Markdown("### Schema Explorer — Gap 1: See which tables the agent picks for your question")
            with gr.Row():
                sch_db = gr.Dropdown(["business", "enterprise"], value="business", label="Database", scale=0)
                sch_q  = gr.Textbox(label="Filter for question (leave blank for full schema)", scale=4)
                sch_n  = gr.Slider(4, 20, value=12, step=1, label="Max tables", scale=1)
                sch_btn = gr.Button("🔍 Explore", variant="primary", scale=0)
            sch_info = gr.Markdown("")
            sch_out  = gr.Markdown("")
            sch_btn.click(fn=explore_schema, inputs=[sch_db, sch_q, sch_n], outputs=[sch_info, sch_out])

        # ------------------------------------------------------------------
        # Tab 3: Cache
        # ------------------------------------------------------------------
        with gr.Tab("⚡ Cache"):
            gr.Markdown("### Query Cache — Gap 4: Memory of past queries")
            with gr.Row():
                cache_btn  = gr.Button("🔄 Refresh Stats", variant="primary")
                flush_btn  = gr.Button("🗑 Flush Cache", variant="stop")
            cache_out = gr.Code(language="json", label="Cache Statistics")
            flush_msg = gr.Markdown("")

            cache_btn.click(fn=get_cache_stats, outputs=[cache_out])
            flush_btn.click(fn=flush_cache, outputs=[flush_msg])
            demo.load(fn=get_cache_stats, outputs=[cache_out])

        # ------------------------------------------------------------------
        # Tab 4: Cost
        # ------------------------------------------------------------------
        with gr.Tab("💰 Cost"):
            gr.Markdown("### Token Usage & Cost — Gap 8: Know your cost per query")
            cost_btn = gr.Button("🔄 Refresh Report", variant="primary")
            cost_out = gr.Markdown("")
            cost_btn.click(fn=get_cost_report, outputs=[cost_out])
            demo.load(fn=get_cost_report, outputs=[cost_out])

        # ------------------------------------------------------------------
        # Tab 5: Evaluate
        # ------------------------------------------------------------------
        with gr.Tab("📊 Evaluate"):
            gr.Markdown(
                "### Eval Harness — Gap 6: Benchmark beyond your demo query\n"
                "Runs the ground-truth test suite and shows silent wrong answers."
            )
            with gr.Row():
                eval_db  = gr.Dropdown(["business", "enterprise"], value="business", label="Database")
                eval_btn = gr.Button("▶ Run Evaluation", variant="primary")
            eval_summary = gr.Markdown("")
            eval_table   = gr.Dataframe(
                headers=["ID", "Question", "Result", "Error", "Latency ms"],
                datatype=["str", "str", "str", "str", "number"],
                interactive=False,
            )
            eval_btn.click(fn=run_eval, inputs=[eval_db], outputs=[eval_summary, eval_table], show_progress=True)

        # ------------------------------------------------------------------
        # Tab 6: Guard demo
        # ------------------------------------------------------------------
        with gr.Tab("🔒 SQL Guard"):
            gr.Markdown(
                "### SQL Guard — Gap 5: Live SELECT-only enforcement\n"
                "Paste any SQL to see whether the guard allows or blocks it."
            )
            guard_input = gr.Textbox(
                value="SELECT * FROM customers LIMIT 5",
                label="SQL to test",
                lines=4,
            )
            guard_btn = gr.Button("🔍 Check", variant="primary")
            guard_out = gr.Markdown("")

            gr.Markdown("#### Try these dangerous queries:")
            dangerous = [
                "DELETE FROM customers WHERE id = 1",
                "DROP TABLE order_items",
                "SELECT 1; UPDATE products SET unit_price = 0",
                "PRAGMA table_info(customers)",
                "WITH cte AS (SELECT 1) SELECT * FROM cte",
            ]
            for d in dangerous:
                b = gr.Button(d, size="sm", variant="secondary")
                b.click(fn=lambda q=d: q, outputs=[guard_input])

            guard_btn.click(fn=test_guard, inputs=[guard_input], outputs=[guard_out])
            guard_input.change(fn=test_guard, inputs=[guard_input], outputs=[guard_out])

    gr.HTML("""
        <div id="app-credit">
            Maintained by <strong>Rangeesh</strong> ·
            based on the open-source project by
            <a href="https://github.com/sweta2503" target="_blank">sweta2503</a> (MIT License)
        </div>
    """)


if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        show_error=True,
        theme=_THEME,
        css=_CSS,
    )
