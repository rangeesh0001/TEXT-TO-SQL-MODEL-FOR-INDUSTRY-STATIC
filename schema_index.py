"""
schema_index.py — Gap 1 & 2: Stop hallucination on large/ugly schemas.

For a database with 80+ tables and cryptic column names the full schema
string is too large to safely inject into every prompt. This module:

  1. Builds a lightweight in-memory index of every table and its columns.
  2. Scores tables against the user's question using keyword overlap and
     fuzzy substring matching — zero vector DB, zero cloud cost.
  3. Returns only the top-N relevant tables with their full DDL.
  4. Provides an alias map so the LLM gets human-readable names even when
     the actual column is called something like `cust_nm` or `ttl_amt`.

Multi-schema / multi-database (Gap 2): pass a list of (engine, schema_name)
pairs to build a federated index. The filtered schema will include an
ATTACH / schema-prefix comment so the LLM knows which DB each table lives in.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Optional

from sqlalchemy import inspect as sa_inspect, text, Engine


# ---------------------------------------------------------------------------
# Column alias dictionary for common enterprise abbreviations
# ---------------------------------------------------------------------------

ABBREV_MAP: dict[str, str] = {
    # Identifiers
    "id": "id",
    "nbr": "number",
    "num": "number",
    "no": "number",
    # Names
    "nm": "name",
    "nme": "name",
    "dsc": "description",
    "desc": "description",
    "lbl": "label",
    # Dates / times
    "dt": "date",
    "dttm": "datetime",
    "ts": "timestamp",
    "yr": "year",
    "mo": "month",
    "dy": "day",
    "eff": "effective",
    "exp": "expiry",
    "crt": "created",
    "upd": "updated",
    # Money
    "amt": "amount",
    "prc": "price",
    "ttl": "total",
    "lst": "list",
    "cst": "cost",
    "tax": "tax",
    "disc": "discount",
    "pct": "percent",
    # Quantities
    "qty": "quantity",
    "cnt": "count",
    "on_hnd": "on_hand",
    "rsrv": "reserved",
    "rord": "reorder",
    # Status / flags
    "sts": "status",
    "flg": "flag",
    "actv": "active",
    "prim": "primary",
    "ind": "indicator",
    # People / org
    "cust": "customer",
    "prd": "product",
    "ord": "order",
    "dtl": "detail",
    "hdr": "header",
    "emp": "employee",
    "mgr": "manager",
    "slsr": "salesperson",
    "terr": "territory",
    "rgn": "region",
    "dept": "department",
    "div": "division",
    "mstr": "master",
    # Address
    "addr": "address",
    "ln": "line",
    "cty": "city",
    "st": "state",
    "zip": "zip",
    "ctry": "country",
    "iso3": "iso3_code",
    # Other
    "lkp": "lookup",
    "cd": "code",
    "typ": "type",
    "mth": "method",
    "shp": "shipping",
    "whs": "warehouse",
    "inv": "invoice",
    "ar": "accounts_receivable",
    "gl": "general_ledger",
    "jrnl": "journal",
    "trx": "transaction",
    "rtn": "return",
    "rsn": "reason",
    "ref": "reference",
    "uom": "unit_of_measure",
    "ytd": "year_to_date",
    "quota": "quota",
    "cntct": "contact",
    "ph": "phone",
    "eml": "email",
    "ext": "extended",
    "db": "debit",
    "cr": "credit",
    "aging": "aging",
    "pmt": "payment",
    "bal": "balance",
    "acct": "account",
    "prnt": "parent",
    "map": "mapping",
}


def _humanise(raw_name: str) -> str:
    """Convert a column like `ttl_amt` → `total amount (ttl_amt)`."""
    parts = raw_name.split("_")
    translated = [ABBREV_MAP.get(p.lower(), p) for p in parts]
    human = " ".join(translated)
    if human.lower() == raw_name.lower():
        return raw_name
    return f"{human} ({raw_name})"


def _humanise_table(table_name: str) -> str:
    """Convert `ord_hdr` → `order header (ord_hdr)`."""
    parts = table_name.split("_")
    translated = [ABBREV_MAP.get(p.lower(), p) for p in parts]
    human = " ".join(translated)
    if human.lower() == table_name.lower():
        return table_name
    return f"{human} [{table_name}]"


# ---------------------------------------------------------------------------
# Index structures
# ---------------------------------------------------------------------------

@dataclass
class ColumnMeta:
    name: str
    col_type: str
    human_name: str = ""

    def __post_init__(self):
        self.human_name = _humanise(self.name)


@dataclass
class TableMeta:
    table_name: str
    human_name: str
    db_label: str                    # "main" for single-DB, alias for multi-DB
    columns: list[ColumnMeta] = field(default_factory=list)
    pk_columns: list[str] = field(default_factory=list)
    foreign_keys: list[dict] = field(default_factory=list)
    row_count: int = 0
    score: float = 0.0               # relevance score set by search()

    @property
    def search_text(self) -> str:
        """Combined text blob used for relevance scoring."""
        col_names = " ".join(c.name for c in self.columns)
        col_human = " ".join(
            w for c in self.columns for w in c.human_name.split()
        )
        return f"{self.table_name} {self.human_name} {col_names} {col_human}".lower()

    def to_schema_string(self, include_human_names: bool = True) -> str:
        prefix = f"[{self.db_label}] " if self.db_label != "main" else ""
        lines = [f"Table: {prefix}{self.table_name}  -- {self.human_name}"]
        lines.append("Columns:")
        for col in self.columns:
            annotation = f"  -- {col.human_name}" if include_human_names and col.human_name != col.name else ""
            lines.append(f"  {col.name} ({col.col_type}){annotation}")
        if self.pk_columns:
            lines.append(f"Primary Key: {', '.join(self.pk_columns)}")
        if self.foreign_keys:
            lines.append("Foreign Keys:")
            for fk in self.foreign_keys:
                src = ", ".join(fk.get("constrained_columns", []))
                ref_tbl = fk.get("referred_table", "?")
                ref_cols = ", ".join(fk.get("referred_columns", []))
                tgt_prefix = f"[{self.db_label}]." if self.db_label != "main" else ""
                lines.append(f"  {src} -> {tgt_prefix}{ref_tbl}.{ref_cols}")
        if self.row_count:
            lines.append(f"Row count: ~{self.row_count:,}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# SchemaIndex
# ---------------------------------------------------------------------------

class SchemaIndex:
    """
    Build and query a schema index over one or more databases.

    Usage (single DB):
        idx = SchemaIndex.build(engine)
        relevant = idx.search("top 5 customers by spending", top_n=5)
        schema_str = idx.to_prompt_schema(relevant)

    Usage (multi-DB):
        idx = SchemaIndex.build_multi([(engine_a, "sales"), (engine_b, "hr")])
        relevant = idx.search("employees who closed most orders", top_n=8)
    """

    def __init__(self):
        self._tables: list[TableMeta] = []

    # ------------------------------------------------------------------
    # Builders
    # ------------------------------------------------------------------

    @classmethod
    def build(cls, engine: Engine, db_label: str = "main") -> "SchemaIndex":
        idx = cls()
        inspector = sa_inspect(engine)
        for tname in inspector.get_table_names():
            idx._index_table(engine, inspector, tname, db_label)
        return idx

    @classmethod
    def build_multi(cls, sources: list[tuple[Engine, str]]) -> "SchemaIndex":
        """Build a federated index over multiple databases."""
        idx = cls()
        for engine, label in sources:
            inspector = sa_inspect(engine)
            for tname in inspector.get_table_names():
                idx._index_table(engine, inspector, tname, label)
        return idx

    def _index_table(self, engine: Engine, inspector, tname: str, db_label: str):
        columns = [
            ColumnMeta(c["name"], str(c["type"]))
            for c in inspector.get_columns(tname)
        ]
        pk = inspector.get_pk_constraint(tname).get("constrained_columns", [])
        fks = inspector.get_foreign_keys(tname)

        # Sample row count (fast approximate)
        row_count = 0
        try:
            with engine.connect() as conn:
                row_count = conn.execute(text(f"SELECT COUNT(*) FROM {tname}")).scalar() or 0
        except Exception:
            pass

        self._tables.append(TableMeta(
            table_name=tname,
            human_name=_humanise_table(tname),
            db_label=db_label,
            columns=columns,
            pk_columns=pk,
            foreign_keys=fks,
            row_count=row_count,
        ))

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenise(text: str) -> set[str]:
        return set(re.sub(r"[^a-z0-9]", " ", text.lower()).split())

    def search(self, question: str, top_n: int = 10) -> list[TableMeta]:
        """
        Score all tables by relevance to the question and return top_n.
        Scoring = keyword overlap + fuzzy substring similarity.
        """
        q_tokens = self._tokenise(question)
        # Also expand abbreviations in the question
        expanded = {ABBREV_MAP.get(t, t) for t in q_tokens} | q_tokens

        scored: list[TableMeta] = []
        for tbl in self._tables:
            tbl_tokens = self._tokenise(tbl.search_text)
            overlap = len(expanded & tbl_tokens)

            # Bonus: fuzzy similarity of question vs table human name
            fuzzy = SequenceMatcher(
                None, question.lower(), tbl.human_name.lower()
            ).ratio()

            tbl.score = overlap * 2.0 + fuzzy * 5.0
            scored.append(tbl)

        scored.sort(key=lambda t: t.score, reverse=True)
        return scored[:top_n]

    def all_tables(self) -> list[TableMeta]:
        return list(self._tables)

    def table_names(self) -> list[str]:
        return [t.table_name for t in self._tables]

    # ------------------------------------------------------------------
    # Prompt formatting
    # ------------------------------------------------------------------

    def to_prompt_schema(
        self,
        tables: Optional[list[TableMeta]] = None,
        include_human_names: bool = True,
    ) -> str:
        if tables is None:
            tables = self._tables
        parts = [t.to_schema_string(include_human_names) for t in tables]
        header = (
            f"-- Schema: {len(tables)} of {len(self._tables)} tables shown "
            f"(selected by relevance)\n\n"
            if len(tables) < len(self._tables)
            else f"-- Schema: {len(tables)} tables\n\n"
        )
        return header + "\n\n".join(parts)

    def full_prompt_schema(self) -> str:
        return self.to_prompt_schema(self._tables)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from sqlalchemy import create_engine as _ce

    eng = _ce("sqlite:///enterprise.db", connect_args={"check_same_thread": False})
    idx = SchemaIndex.build(eng, "enterprise")

    print(f"Total tables indexed: {len(idx.table_names())}")
    print("All tables:", idx.table_names(), "\n")

    question = "show me top customers by total spending last year"
    results = idx.search(question, top_n=5)
    print(f"Top 5 tables for: '{question}'")
    for tbl in results:
        print(f"  {tbl.table_name:<20} score={tbl.score:.1f}  ({tbl.human_name})")

    print("\n--- Filtered schema sent to LLM ---")
    print(idx.to_prompt_schema(results))
