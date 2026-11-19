from app.db import get_db
from app.services.enums import accept_value, contract_enum, list_enum_snapshots, page_enum

# 清洗水线固定复验的枚举值：页面登记的新值必须能过清洗准入。
WATERLINE_PROBES = [("status", "NEW"), ("status", "CANCEL")]


def enum_waterline(db_path: str | None = None) -> dict:
    """清洗与水线各复验一轮：对照快照表同一结局核对页面新值可入。"""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM enum_snapshots ORDER BY enum_name, id DESC"
        ).fetchall()
        latest_ids = {}
        for r in rows:
            latest_ids.setdefault(r["enum_name"], r["id"])
        snapshot = next(
            (r for r in rows if r["enum_name"] == "status" and r["id"] == latest_ids.get("status")),
            None,
        )
        snapshot_id = snapshot["id"] if snapshot else None
        contract_values = contract_enum("status", db_path)
        page_values = page_enum("status", db_path)
        probes = [
            {"enum_name": name, "value": value, "accepted": accept_value(name, value, db_path)}
            for name, value in WATERLINE_PROBES
        ]
        same_source = all(v in contract_values for v in page_values)
        enum_ok = same_source and all(p["accepted"] for p in probes)
        detail_parts = []
        if not same_source:
            missing = [v for v in page_values if v not in contract_values]
            detail_parts.append("页面枚举未同源：缺 " + ",".join(missing))
        rejected = [p["value"] for p in probes if not p["accepted"]]
        if rejected:
            detail_parts.append("新值被清洗拒绝：" + ",".join(rejected))
        return {
            "snapshot_id": snapshot_id,
            "page_enum": page_values,
            "contract_enum": contract_values,
            "probes": probes,
            "same_source": same_source,
            "enum_ok": enum_ok,
            "detail": "；".join(detail_parts) if detail_parts else "页面与契约枚举同源，新值可入",
        }


def run_reconcile(as_of: str, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        contracts = conn.execute(
            "SELECT table_name, version_no FROM schema_contracts WHERE status='现行'"
        ).fetchall()
        catalog = {
            r["table_name"]: r["live_version"]
            for r in conn.execute("SELECT table_name, live_version FROM catalog_tables")
        }
        matched = 0
        drifted = 0
        details = []
        for c in contracts:
            live = catalog.get(c["table_name"])
            if live == c["version_no"]:
                matched += 1
            else:
                drifted += 1
                details.append(f"{c['table_name']}:契约v{c['version_no']}/目录v{live}")

        # 水线对照枚举快照同一结局再验一轮，结果落库留痕。
        water = enum_waterline(db_path)
        enum_detail = (
            f"快照#{water['snapshot_id']}：{water['detail']}"
            if water["snapshot_id"] is not None
            else water["detail"]
        )
        summary = (
            f"对账{as_of}：对齐{matched}，漂移{drifted}"
            + (("；" + "，".join(details)) if details else "")
            + f"；枚举水线{'通过' if water['enum_ok'] else '失败'}（{enum_detail}）"
        )
        cur = conn.execute(
            "INSERT INTO reconcile_reports(as_of, matched, drifted, summary, "
            "enum_snapshot_id, enum_ok, enum_detail) VALUES (?,?,?,?,?,?,?)",
            (as_of, matched, drifted, summary,
             water["snapshot_id"], 1 if water["enum_ok"] else 0, enum_detail),
        )
        row = conn.execute(
            "SELECT * FROM reconcile_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        result = dict(row)
        result["enum_waterline"] = water
        return result


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM reconcile_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
