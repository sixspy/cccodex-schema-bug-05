import sqlite3

from app.db import get_db
from app.services.errors import ServiceError


def _latest_snapshot(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM enum_snapshots WHERE enum_name=? "
        "ORDER BY id DESC LIMIT 1",
        (name,),
    ).fetchone()


def contract_enum(name: str, db_path: str | None = None) -> list[str]:
    """契约/清洗读取的枚举：唯一来源是枚举快照表最新快照。"""
    with get_db(db_path) as conn:
        row = _latest_snapshot(conn, name)
        return row["values_csv"].split(",") if row and row["values_csv"] else []


def page_enum(name: str, db_path: str | None = None) -> list[str]:
    """页面登记的拟用枚举（meta 为页面入口），换版后须被快照同源收拢。"""
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (f"enum_page_{name}",)
        ).fetchone()
        return (row["value"] if row else "").split(",") if row else []


def list_enum_snapshots(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT s.* FROM enum_snapshots s "
            "ORDER BY s.enum_name, s.id"
        ).fetchall()
        result = []
        for r in rows:
            latest = _latest_snapshot(conn, r["enum_name"])
            result.append({
                "snapshot_id": r["id"],
                "enum_name": r["enum_name"],
                "values": r["values_csv"].split(","),
                "source": r["source"],
                "created_at": r["created_at"],
                "is_latest": bool(latest and latest["id"] == r["id"]),
            })
        return result


def accept_value(name: str, value: str, db_path: str | None = None) -> bool:
    """清洗准入：只认快照表里的现行契约枚举，页面未收拢的新值不放行。"""
    return value in contract_enum(name, db_path)


def consolidate_enums(conn: sqlite3.Connection) -> list[dict]:
    """升版事务内一次性收拢扩值：页面枚举相对最新快照新增的值落成新快照。

    只允许独立枚举快照表这一种形态；任一枚举落库失败即抛错，由外层回滚整单，
    不允许出现“换版成功、枚举漏写”的半完成状态。
    """
    names = [
        r["key"][len("enum_page_"):]
        for r in conn.execute(
            "SELECT key FROM meta WHERE key LIKE 'enum_page_%'"
        ).fetchall()
    ]
    changes = []
    for name in names:
        page_row = conn.execute(
            "SELECT value FROM meta WHERE key=?", (f"enum_page_{name}",)
        ).fetchone()
        page_values = [v for v in (page_row["value"] if page_row else "").split(",") if v]
        snap = _latest_snapshot(conn, name)
        current = snap["values_csv"].split(",") if snap and snap["values_csv"] else []
        added = [v for v in page_values if v not in current]
        if not added:
            if snap:
                changes.append({
                    "enum_name": name,
                    "snapshot_id": snap["id"],
                    "added": [],
                    "values": current,
                })
            continue
        merged = current + added
        cur = conn.execute(
            "INSERT INTO enum_snapshots(enum_name, values_csv, source) VALUES (?,?,?)",
            (name, ",".join(merged), "升版收拢"),
        )
        new_id = cur.lastrowid
        # 落库后立刻同事务复核；写不进去就停，杜绝漏写。
        verify = _latest_snapshot(conn, name)
        if verify is None or verify["id"] != new_id or verify["values_csv"] != ",".join(merged):
            raise ServiceError("枚举扩值落库失败，已中止换版", 500)
        changes.append({
            "enum_name": name,
            "snapshot_id": new_id,
            "added": added,
            "values": merged,
        })
    return changes
