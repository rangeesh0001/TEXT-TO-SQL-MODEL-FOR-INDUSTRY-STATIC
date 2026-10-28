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


def _build_schema_string(eng: Engine) -> str:
    inspector = inspect(eng)
    parts = []
    for table_name in inspector.get_table_names():
        cols = inspector.get_columns(table_name)
        col_defs = [f"  {c['name']} ({c['type']})" for c in cols]

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
