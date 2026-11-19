import sqlite3

from app.db import get_db
from app.services.enums import consolidate_enums
from app.services.errors import ServiceError


def list_contracts(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM contract_fields f WHERE f.contract_id=c.id) AS field_count "
            "FROM schema_contracts c ORDER BY c.table_name, c.version_no"
        ).fetchall()
        return [dict(r) for r in rows]


def get_contract_detail(contract_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        fields = conn.execute(
            "SELECT field_name, field_type FROM contract_fields "
            "WHERE contract_id=? ORDER BY id",
            (contract_id,),
        ).fetchall()
        data = dict(draft)
        data["fields"] = [dict(r) for r in fields]
        return data


def _fields(conn, contract_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT field_name, field_type FROM contract_fields WHERE contract_id=?",
        (contract_id,),
    ).fetchall()
    return {r["field_name"]: r["field_type"] for r in rows}


def run_breaking_check(contract_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        draft = conn.execute(
            "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
        ).fetchone()
        if not draft:
            raise ServiceError("契约不存在", 404)
        if draft["status"] != "草稿":
            raise ServiceError("仅草稿可做破坏性检查", 409)
        live = conn.execute(
            "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
            (draft["table_name"],),
        ).fetchone()
        if not live:
            raise ServiceError("无现行契约可对照", 409)
        old_f = _fields(conn, live["id"])
        new_f = _fields(conn, draft["id"])
        removed = sorted(set(old_f) - set(new_f))
        type_changed = sorted(
            k for k in set(old_f) & set(new_f) if old_f[k] != new_f[k]
        )
        added = sorted(set(new_f) - set(old_f))
        breaking = bool(removed or type_changed)
        return {
            "contract_id": contract_id,
            "table_name": draft["table_name"],
            "from_version": live["version_no"],
            "to_version": draft["version_no"],
            "removed_fields": removed,
            "type_changed_fields": type_changed,
            "added_fields": added,
            "breaking": breaking,
            "verdict": "破坏性" if breaking else "兼容",
        }


def apply_contract(contract_id: int, db_path: str | None = None) -> dict:
    # 独立连接上立即取写锁：左右两侧同时抢同一轮换版时，后到者等锁后复核状态，
    # 杜绝缺锁导致的双写枚举列/重复升版。
    with get_db(db_path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise ServiceError("已有换版在执行，请稍后再试", 409) from exc
        try:
            draft = conn.execute(
                "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
            ).fetchone()
            if not draft:
                raise ServiceError("契约不存在", 404)
            if draft["status"] != "草稿":
                raise ServiceError("仅草稿可换版应用", 409)
            live = conn.execute(
                "SELECT * FROM schema_contracts WHERE table_name=? AND status='现行'",
                (draft["table_name"],),
            ).fetchone()
            from_ver = live["version_no"] if live else None
            if live:
                conn.execute(
                    "UPDATE schema_contracts SET status='停用' WHERE id=?",
                    (live["id"],),
                )
            conn.execute(
                "UPDATE schema_contracts SET status='现行' WHERE id=?",
                (contract_id,),
            )
            conn.execute(
                "INSERT INTO catalog_tables(table_name, live_version) VALUES (?,?) "
                "ON CONFLICT(table_name) DO UPDATE SET live_version=excluded.live_version, "
                "registered_at=datetime('now')",
                (draft["table_name"], draft["version_no"]),
            )

            # 同一次事务里收拢枚举扩值；失败即抛短句并整体回滚，枚举漏写即停。
            enum_changes = consolidate_enums(conn)
            status_change = next(
                (c for c in enum_changes if c["enum_name"] == "status"), None
            )
            snapshot_id = status_change["snapshot_id"] if status_change else None
            added_csv = ",".join(status_change["added"]) if status_change else ""

            conn.execute(
                "INSERT INTO apply_runs(contract_id, table_name, from_version, to_version, "
                "result, enum_snapshot_id, enum_added) VALUES (?,?,?,?,?,?,?)",
                (contract_id, draft["table_name"], from_ver, draft["version_no"],
                 "已应用", snapshot_id, added_csv),
            )
            row = conn.execute(
                "SELECT * FROM schema_contracts WHERE id=?", (contract_id,)
            ).fetchone()
            data = dict(row)
            data["enum_expansion"] = [
                {
                    "enum_name": c["enum_name"],
                    "snapshot_id": c["snapshot_id"],
                    "added": c["added"],
                    "values": c["values"],
                }
                for c in enum_changes
            ]
            return data
        except sqlite3.OperationalError as exc:
            # 锁等待超时等库级失败：回滚并给前端一句短报错。
            raise ServiceError("换版抢锁失败，请稍后再试", 409) from exc
