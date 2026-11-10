"""
database.py — Multi-database support with cached schema context.

Gap 2: Connect to multiple databases (business.db + enterprise.db).
Gap 8: Cache the schema string so repeat queries don't re-scan the DB.
"""

import threading
import time
from typing import Any, Optional

from sqlalchemy import create_engine, inspect, text, Engine
from sqlalchemy.orm import sessionmaker

# ---------------------------------------------------------------------------
# Database registry — add new databases here
# ---------------------------------------------------------------------------

_DB_CONFIGS = {
    "business": "sqlite:///business.db",
    "enterprise": "sqlite:///enterprise.db",
}

_engines: dict[str, Engine] = {}
_engines_lock = threading.Lock()

# Default active DB (can be changed at startup via set_active_db)
_active_db_name = "business"


def _make_engine(url: str) -> Engine:
    return create_engine(url, connect_args={"check_same_thread": False})


def get_engine(db_name: Optional[str] = None) -> Engine:
    name = db_name or _active_db_name
    with _engines_lock:
        if name not in _engines:
            if name not in _DB_CONFIGS:
                raise ValueError(f"Unknown database: {name!r}. Known: {list(_DB_CONFIGS)}")
            _engines[name] = _make_engine(_DB_CONFIGS[name])
        return _engines[name]


def set_active_db(name: str) -> None:
    global _active_db_name
    if name not in _DB_CONFIGS:
        raise ValueError(f"Unknown database: {name!r}")
    _active_db_name = name


# Backwards-compat alias used by api.py / agent.py
engine = None  # resolved lazily via get_engine()


# ---------------------------------------------------------------------------
# Schema cache (Gap 8: avoid re-scanning on every query)
# ---------------------------------------------------------------------------

_schema_cache: dict[str, tuple[str, float]] = {}   # db_name → (schema_str, ts)
_SCHEMA_TTL = 300.0    # seconds — re-scan after 5 minutes


# ---------------------------------------------------------------------------
# Column value hints (fix for silent 0-row results)
# ---------------------------------------------------------------------------
# Enterprise data stores short codes ('COMP', 'GLD', 'NE') while users and the
# LLM say 'completed', 'Gold', 'Northeast'. If the prompt never shows the real
# values, the model guesses and the query silently returns 0 rows. For every
# low-cardinality text column we list its actual distinct values in the prompt.

VALUE_HINT_MAX_DISTINCT = 15   # only columns with <= this many distinct values
VALUE_HINT_MAX_LEN = 40        # skip long free-text values

_TEXT_TYPES = ("CHAR", "TEXT", "CLOB", "STRING")


def get_column_values(eng: Engine, table: str, column: str, col_type: str) -> list[str]:
    """Return the distinct values of a short text column, or [] if not a good hint."""
    if not any(t in str(col_type).upper() for t in _TEXT_TYPES):
        return []
    try:
        with eng.connect() as conn:
            rows = conn.execute(text(
                f'SELECT DISTINCT "{column}" FROM "{table}" '
                f'WHERE "{column}" IS NOT NULL LIMIT {VALUE_HINT_MAX_DISTINCT + 1}'
            )).fetchall()
    except Exception:
        return []
    values = [str(r[0]) for r in rows]
    if not values or len(values) > VALUE_HINT_MAX_DISTINCT:
        return []
    if any(len(v) > VALUE_HINT_MAX_LEN for v in values):
        return []
    return sorted(values)


def format_value_hint(values: list[str]) -> str:
    return "  -- values: " + ", ".join(f"'{v}'" for v in values) if values else ""


def get_value_map(db_name: Optional[str] = None) -> dict[tuple[str, str], list[str]]:
    """(table, column) -> allowed values, for every hinted column in the database."""
    eng = get_engine(db_name)
    inspector = inspect(eng)
    out: dict[tuple[str, str], list[str]] = {}
    tables = inspector.get_table_names()
    for table_name in tables:
        for c in inspector.get_columns(table_name):
            vals = get_column_values(eng, table_name, c["name"], c["type"])
            if vals:
                out[(table_name, c["name"])] = vals
    # A code can be valid without appearing in the data yet: cust_addr.ctry_cd
    # only holds 'US', but 'CA' is a real country in cntry_lkp. Add the values
    # of the lookup table each foreign key points to, so "no customers in
    # Canada" is treated as a genuine empty answer, not a wrong filter.
    for table_name in tables:
        for fk in inspector.get_foreign_keys(table_name):
            for src, ref in zip(fk.get("constrained_columns", []), fk.get("referred_columns", [])):
                ref_vals = out.get((fk.get("referred_table"), ref))
                if ref_vals and (table_name, src) in out:
                    out[(table_name, src)] = sorted(set(out[(table_name, src)]) | set(ref_vals))
    return out


def _build_schema_string(eng: Engine) -> str:
    inspector = inspect(eng)
    parts = []
    for table_name in inspector.get_table_names():
        cols = inspector.get_columns(table_name)
        col_defs = [
            f"  {c['name']} ({c['type']})"
            + format_value_hint(get_column_values(eng, table_name, c["name"], c["type"]))
            for c in cols
        ]

        try:
            pk = inspector.get_pk_constraint(table_name).get("constrained_columns", [])
        except Exception:
            pk = []
        try:
            fks = inspector.get_foreign_keys(table_name)
        except Exception:
            fks = []

        tdef = f"Table: {table_name}\nColumns:\n" + "\n".join(col_defs)
        if pk:
            tdef += f"\nPrimary Key: {', '.join(pk)}"
        if fks:
            fk_lines = []
            for fk in fks:
                src = ", ".join(fk.get("constrained_columns", []))
                ref = fk.get("referred_table", "")
                ref_cols = ", ".join(fk.get("referred_columns", []))
                fk_lines.append(f"  {src} -> {ref}.{ref_cols}")
            tdef += "\nForeign Keys:\n" + "\n".join(fk_lines)
        parts.append(tdef)

    return "\n\n".join(parts)


def get_schema_string(db_name: Optional[str] = None) -> str:
    """Return schema DDL string, served from cache when fresh."""
    name = db_name or _active_db_name
    cached = _schema_cache.get(name)
    if cached:
        schema_str, ts = cached
        if time.time() - ts < _SCHEMA_TTL:
            return schema_str

    eng = get_engine(name)
    schema_str = _build_schema_string(eng)
    _schema_cache[name] = (schema_str, time.time())
    return schema_str


def invalidate_schema_cache(db_name: Optional[str] = None) -> None:
    name = db_name or _active_db_name
    _schema_cache.pop(name, None)


def schema_cache_age(db_name: Optional[str] = None) -> Optional[float]:
    """Return seconds since last schema cache build, or None if not cached."""
    name = db_name or _active_db_name
    cached = _schema_cache.get(name)
    return time.time() - cached[1] if cached else None


# ---------------------------------------------------------------------------
# SQL execution
# ---------------------------------------------------------------------------

def execute_sql(query: str, db_name: Optional[str] = None) -> list[dict[str, Any]]:
    eng = get_engine(db_name)
    with eng.connect() as conn:
        result = conn.execute(text(query))
        columns = list(result.keys())
        rows = result.fetchall()
        return [dict(zip(columns, row)) for row in rows]


def get_table_row_counts(db_name: Optional[str] = None) -> dict[str, int]:
    eng = get_engine(db_name)
    inspector = inspect(eng)
    counts = {}
    with eng.connect() as conn:
        for tname in inspector.get_table_names():
            try:
                count = conn.execute(text(f"SELECT COUNT(*) FROM {tname}")).scalar()
                counts[tname] = count or 0
            except Exception:
                counts[tname] = -1
    return counts


def test_connection(db_name: Optional[str] = None) -> bool:
    try:
        with get_engine(db_name).connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# SessionLocal (kept for API compatibility)
# ---------------------------------------------------------------------------

def get_session_maker(db_name: Optional[str] = None):
    return sessionmaker(autocommit=False, autoflush=False, bind=get_engine(db_name))


def get_db(db_name: Optional[str] = None):
    Session = get_session_maker(db_name)
    db = Session()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    for db in ["business", "enterprise"]:
        ok = test_connection(db)
        if not ok:
            print(f"{db}: connection failed (run seed_db.py / ugly_schema_seed.py first)")
            continue
        schema = get_schema_string(db)
        table_count = schema.count("Table:")
        print(f"{db}: connected, {table_count} tables, schema cached")
        age = schema_cache_age(db)
        print(f"  cache age: {age:.1f}s")
        counts = get_table_row_counts(db)
        print(f"  row counts: {counts}")
