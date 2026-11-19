import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_contracts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('现行','草稿','停用')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(table_name, version_no)
);

CREATE TABLE IF NOT EXISTS contract_fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL REFERENCES schema_contracts(id),
  field_name TEXT NOT NULL,
  field_type TEXT NOT NULL,
  UNIQUE(contract_id, field_name)
);

CREATE TABLE IF NOT EXISTS catalog_tables (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  live_version INTEGER NOT NULL,
  registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS apply_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL,
  table_name TEXT NOT NULL,
  from_version INTEGER,
  to_version INTEGER NOT NULL,
  result TEXT NOT NULL,
  enum_snapshot_id INTEGER,
  enum_added TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of TEXT NOT NULL,
  matched INTEGER NOT NULL,
  drifted INTEGER NOT NULL,
  summary TEXT NOT NULL,
  enum_snapshot_id INTEGER,
  enum_ok INTEGER NOT NULL DEFAULT 1,
  enum_detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 唯一形态：独立枚举快照表。每次扩值追加一行不可变快照，
-- 契约枚举/清洗校验/页面枚举同源读最新快照，不与物化清单并存。
CREATE TABLE IF NOT EXISTS enum_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  enum_name TEXT NOT NULL,
  values_csv TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '升版收拢',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_enum_snapshots_name_id
  ON enum_snapshots(enum_name, id);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SEED = [
    ("dw_orders_d", 1, "现行", "初始现行", [("order_id", "INT"), ("amount", "DECIMAL"), ("biz_date", "DATE")]),
    ("dw_orders_d", 2, "草稿", "拟删 amount 改币种", [("order_id", "INT"), ("currency", "STRING"), ("biz_date", "DATE")]),
    ("dw_payments_d", 1, "现行", "支付现行", [("pay_id", "INT"), ("order_id", "INT"), ("paid_at", "TIMESTAMP")]),
]


def _migrate_legacy_enum(conn: sqlite3.Connection) -> None:
    """旧库把契约枚举存在 meta.enum_contract_*，迁移成不可变快照并删除旧键。"""
    legacy = conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'enum_contract_%'"
    ).fetchall()
    for row in legacy:
        name = row["key"][len("enum_contract_"):]
        exists = conn.execute(
            "SELECT 1 FROM enum_snapshots WHERE enum_name=?", (name,)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO enum_snapshots(enum_name, values_csv, source) VALUES (?,?,?)",
                (name, row["value"], "旧库迁移"),
            )
        conn.execute("DELETE FROM meta WHERE key=?", (row["key"],))


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate_legacy_enum(conn)
        if conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            return
        for table, ver, status, note, fields in SEED:
            cur = conn.execute(
                "INSERT INTO schema_contracts(table_name, version_no, status, note) VALUES (?,?,?,?)",
                (table, ver, status, note),
            )
            cid = cur.lastrowid
            for fname, ftype in fields:
                conn.execute(
                    "INSERT INTO contract_fields(contract_id, field_name, field_type) VALUES (?,?,?)",
                    (cid, fname, ftype),
                )
            if status == "现行":
                conn.execute(
                    "INSERT OR REPLACE INTO catalog_tables(table_name, live_version) VALUES (?,?)",
                    (table, ver),
                )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
        # 页面侧已登记新值 CANCEL；契约枚举基线快照只含 NEW,PAID，
        # 待换版时一次性收拢扩值（独立枚举快照表，唯一形态）。
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('enum_page_status','NEW,PAID,CANCEL')")
        conn.execute(
            "INSERT INTO enum_snapshots(enum_name, values_csv, source) VALUES (?,?,?)",
            ("status", "NEW,PAID", "基线快照"),
        )
