import threading

import pytest

from app.db import get_db, init_db
from app.services import apply_contract
from app.services.contracts import get_contract_detail
from app.services.enums import (
    accept_value,
    contract_enum,
    list_enum_snapshots,
    page_enum,
)
from app.services.errors import ServiceError
from app.services.reconcile import enum_waterline, run_reconcile


def _draft_id(db_path: str) -> int:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM schema_contracts WHERE status='草稿' LIMIT 1"
        ).fetchone()
        return row["id"]


def test_before_apply_new_enum_value_rejected(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    # 复现：页面已挂 CANCEL，契约快照没有，清洗先拒收
    assert "CANCEL" in page_enum("status", db)
    assert "CANCEL" not in contract_enum("status", db)
    assert accept_value("status", "CANCEL", db) is False
    assert accept_value("status", "NEW", db) is True


def test_apply_collects_enum_expansion_into_snapshot(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    cid = _draft_id(db)

    result = apply_contract(cid, db)
    expansion = {e["enum_name"]: e for e in result["enum_expansion"]}
    assert expansion["status"]["added"] == ["CANCEL"]

    snaps = list_enum_snapshots(db)
    latest = next(s for s in snaps if s["is_latest"])
    assert latest["snapshot_id"] == expansion["status"]["snapshot_id"]
    assert latest["values"] == ["NEW", "PAID", "CANCEL"]
    # 旧快照不可变，仍保留
    assert sorted(s["snapshot_id"] for s in snaps) == [1, 2]
    assert snaps[0]["values"] == ["NEW", "PAID"]

    # 清洗与页面同源后新值可入
    assert accept_value("status", "CANCEL", db) is True
    assert contract_enum("status", db) == page_enum("status", db)

    with get_db(db) as conn:
        run = conn.execute(
            "SELECT enum_snapshot_id, enum_added FROM apply_runs "
            "WHERE contract_id=?", (cid,)
        ).fetchone()
    assert run["enum_snapshot_id"] == latest["snapshot_id"]
    assert run["enum_added"] == "CANCEL"


def test_reapply_does_not_double_write_enum(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    cid = _draft_id(db)
    apply_contract(cid, db)
    with pytest.raises(ServiceError) as exc:
        apply_contract(cid, db)
    assert exc.value.status_code == 409
    # 没有产生重复快照
    assert len(list_enum_snapshots(db)) == 2


def test_concurrent_apply_only_one_wins(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    cid = _draft_id(db)
    outcomes = []

    def worker():
        try:
            apply_contract(cid, db)
            outcomes.append("ok")
        except ServiceError as exc:
            outcomes.append(f"err:{exc.status_code}")

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert sorted(outcomes) == ["err:409", "ok"]
    # 缺锁双写会出现 3 个快照；正确加锁后只有基线+收拢两行
    assert len(list_enum_snapshots(db)) == 2
    with get_db(db) as conn:
        live_count = conn.execute(
            "SELECT COUNT(*) AS n FROM schema_contracts WHERE status='现行'"
        ).fetchone()["n"]
        apply_count = conn.execute(
            "SELECT COUNT(*) AS n FROM apply_runs WHERE result='已应用'"
        ).fetchone()["n"]
    assert live_count == 2
    assert apply_count == 1


def test_waterline_verifies_same_outcome(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)

    before = enum_waterline(db)
    assert before["enum_ok"] is False
    assert any(p["value"] == "CANCEL" and not p["accepted"] for p in before["probes"])

    apply_contract(_draft_id(db), db)
    after = enum_waterline(db)
    assert after["enum_ok"] is True
    assert after["same_source"] is True
    assert all(p["accepted"] for p in after["probes"])

    report = run_reconcile("2026-11-19", db)
    assert report["enum_waterline"]["enum_ok"] is True
    assert report["enum_ok"] == 1
    assert f"快照#{after['snapshot_id']}" in report["enum_detail"]
    assert "枚举水线通过" in report["summary"]


def test_contract_detail_exposes_fields(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    cid = _draft_id(db)
    detail = get_contract_detail(cid, db)
    names = {f["field_name"] for f in detail["fields"]}
    assert {"order_id", "currency", "biz_date"} <= names


async def test_enum_api_chain(authed):
    meta = await (await authed.get("/api/meta")).json()
    assert meta["accept_CANCEL"] is False

    contracts = (await (await authed.get("/api/contracts")).json())["items"]
    cid = next(c["id"] for c in contracts if c["status"] == "草稿")
    applied = await (await authed.post(f"/api/contracts/{cid}/apply", json={})).json()
    snap_id = applied["contract"]["enum_expansion"][0]["snapshot_id"]

    enums = await (await authed.get("/api/enums")).json()
    latest = next(i for i in enums["items"] if i["is_latest"])
    assert latest["snapshot_id"] == snap_id
    assert "CANCEL" in latest["values"]
    assert enums["page_enum_status"] == enums["contract_enum_status"]

    water = await (await authed.get("/api/enum-waterline")).json()
    assert water["waterline"]["enum_ok"] is True

    detail = await (await authed.get(f"/api/contracts/{cid}")).json()
    assert detail["contract"]["fields"]

    rep = await (await authed.post("/api/reconcile-reports", json={"as_of": "2026-11-19"})).json()
    assert rep["report"]["enum_waterline"]["enum_ok"] is True


async def test_legacy_meta_enum_migrates_to_snapshot(tmp_path):
    db = str(tmp_path / "t.db")
    init_db(db)
    # 手工造出旧库形态：塞回 meta 键并清空快照，再走一次 init_db 迁移
    with get_db(db) as conn:
        conn.execute("DELETE FROM enum_snapshots")
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('enum_contract_status','NEW,PAID')"
        )
    init_db(db)
    snaps = list_enum_snapshots(db)
    assert any(s["values"] == ["NEW", "PAID"] and s["source"] == "旧库迁移" for s in snaps)
    with get_db(db) as conn:
        assert not conn.execute(
            "SELECT 1 FROM meta WHERE key='enum_contract_status'"
        ).fetchone()
