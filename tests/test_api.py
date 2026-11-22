"""关键功能自测：入册规则、证据追溯、幂等、撤批换发、事务原子性、并发。"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import logic
from app.db import Database
from app.main import create_app


def obs(obs_id, county="云和县", species="中华秋沙鸭", team="甲队", plot="样地A"):
    return {"obs_id": obs_id, "county": county, "species": species, "team": team, "plot": plot}


def triple(prefix="", county="云和县", species="中华秋沙鸭"):
    """三笔满足条件（三队三样地）的观测。"""
    return [
        obs(f"{prefix}1", county, species, "甲队", "样地A"),
        obs(f"{prefix}2", county, species, "乙队", "样地B"),
        obs(f"{prefix}3", county, species, "丙队", "样地C"),
    ]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.delenv("DEMO_SEED", raising=False)
    app = create_app()
    with TestClient(app) as c:
        yield c


def submit(client, batch_no, observations):
    return client.post("/batches", json={"batch_no": batch_no, "observations": observations})


# ------------------------------------------------------------ 入册规则


def test_three_distinct_obs_listed_with_evidence(client):
    r = submit(client, "B1", triple("O"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] == 3
    assert body["registry_changes"][0]["action"] == "listed"
    ev_no = body["registry_changes"][0]["evidence_no"]

    reg = client.get("/registry").json()
    assert len(reg) == 1
    assert reg[0]["county"] == "云和县"
    assert reg[0]["species"] == "中华秋沙鸭"
    assert reg[0]["evidence_no"] == ev_no
    assert reg[0]["evidence_status"] == "valid"


def test_same_team_or_same_plot_not_listed(client):
    # 三笔但两笔同队 → 不入册
    r = submit(client, "B1", [
        obs("O1", team="甲队", plot="样地A"),
        obs("O2", team="甲队", plot="样地B"),
        obs("O3", team="丙队", plot="样地C"),
    ])
    assert r.status_code == 200
    assert r.json()["registry_changes"][0]["action"] == "none"
    assert client.get("/registry").json() == []

    # 三笔但两笔同样地 → 不入册
    r = submit(client, "B2", [
        obs("P1", team="甲队", plot="样地A"),
        obs("P2", team="乙队", plot="样地A"),
        obs("P3", team="丙队", plot="样地C"),
    ])
    assert r.status_code == 200
    assert client.get("/registry").json() == []


def test_triple_can_span_batches(client):
    submit(client, "B1", [obs("O1", team="甲队", plot="样地A"), obs("O2", team="乙队", plot="样地B")])
    assert client.get("/registry").json() == []
    r = submit(client, "B2", [obs("O3", team="丙队", plot="样地C")])
    change = r.json()["registry_changes"][0]
    assert change["action"] == "listed"

    ev = client.get(f"/evidence/{change['evidence_no']}").json()
    assert {o["batch_no"] for o in ev["observations"]} == {"B1", "B2"}


def test_extra_batch_keeps_original_evidence(client):
    submit(client, "B1", triple("O"))
    ev_before = client.get("/registry").json()[0]["evidence_no"]
    r = submit(client, "B2", triple("X", species="中华秋沙鸭"))
    # 原三笔仍有效 → 保留原证据号
    assert r.json()["registry_changes"][0]["action"] == "kept"
    assert client.get("/registry").json()[0]["evidence_no"] == ev_before


# ------------------------------------------------------------ 幂等与拒收


def test_identical_resubmit_is_noop(client):
    payload = triple("O")
    r1 = submit(client, "B1", payload)
    r2 = submit(client, "B1", payload)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert client.get("/registry").json()[0]["evidence_no"] == \
        r1.json()["registry_changes"][0]["evidence_no"]
    assert len(client.get("/batches").json()) == 1


def test_conflicting_resubmit_rejected(client):
    submit(client, "B1", triple("O"))
    r = submit(client, "B1", triple("X"))
    assert r.status_code == 409
    # 原状态不变
    batches = client.get("/batches").json()
    assert len(batches) == 1
    assert batches[0]["status"] == "active"
    assert client.get("/batches/B1").json()["observations"][0]["obs_id"] == "O1"


def test_resubmit_after_withdrawal_keeps_withdrawn(client):
    payload = triple("O")
    submit(client, "B1", payload)
    client.post("/batches/B1/withdraw")
    r = submit(client, "B1", payload)
    assert r.json()["replayed"] is True
    assert r.json()["status"] == "withdrawn"
    assert client.get("/batches/B1").json()["status"] == "withdrawn"


def test_empty_batch_and_duplicate_obs_id_rejected(client):
    assert submit(client, "B1", []).status_code == 422
    r = submit(client, "B2", [obs("O1"), obs("O1", team="乙队")])
    assert r.status_code == 422
    assert client.get("/batches").json() == []


# ------------------------------------------------------------ 撤批与换发


def test_withdraw_with_replacement_issues_new_evidence(client):
    submit(client, "B1", triple("O"))
    submit(client, "B2", triple("X"))
    old_ev = client.get("/registry").json()[0]["evidence_no"]

    r = client.post("/batches/B1/withdraw")
    assert r.status_code == 200
    change = r.json()["registry_changes"][0]
    assert change["action"] == "replaced"
    new_ev = change["evidence_no"]
    assert new_ev != old_ev

    # 新证据来自 B2 的三笔
    ev = client.get(f"/evidence/{new_ev}").json()
    assert ev["status"] == "valid"
    assert {o["batch_no"] for o in ev["observations"]} == {"B2"}
    assert {o["obs_id"] for o in ev["observations"]} == {"X1", "X2", "X3"}

    # 旧证据号仍可查，标为失效并指向新号
    old = client.get(f"/evidence/{old_ev}").json()
    assert old["status"] == "invalid"
    assert old["replaced_by"] == new_ev
    assert old["all_source_batches_active"] is False
    assert {o["batch_no"] for o in old["observations"]} == {"B1"}

    # 名录仍列出该组合，挂新证据号
    reg = client.get("/registry").json()
    assert reg[0]["evidence_no"] == new_ev


def test_withdraw_without_replacement_delists(client):
    submit(client, "B1", triple("O"))
    old_ev = client.get("/registry").json()[0]["evidence_no"]
    r = client.post("/batches/B1/withdraw")
    change = r.json()["registry_changes"][0]
    assert change["action"] == "delisted"
    assert client.get("/registry").json() == []
    old = client.get(f"/evidence/{old_ev}").json()
    assert old["status"] == "invalid"
    assert old["replaced_by"] is None


def test_double_withdraw_is_noop(client):
    submit(client, "B1", triple("O"))
    submit(client, "B2", triple("X"))
    r1 = client.post("/batches/B1/withdraw")
    r2 = client.post("/batches/B1/withdraw")
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["registry_changes"] == []
    # 状态与第一次撤完后一致
    assert client.get("/registry").json()[0]["evidence_no"] == \
        r1.json()["registry_changes"][0]["evidence_no"]


def test_withdraw_unknown_batch_404(client):
    assert client.post("/batches/NOPE/withdraw").status_code == 404
    assert client.get("/evidence/EV-999999").status_code == 404
    assert client.get("/batches/NOPE").status_code == 404


def test_withdraw_only_revalidates_affected_combos(client):
    submit(client, "B1", triple("O"))                                    # 云和·秋沙鸭
    submit(client, "B2", triple("P", county="景宁县", species="黄腹角雉"))  # 景宁·角雉
    other_ev = [e for e in client.get("/registry").json()
                if e["county"] == "景宁县"][0]["evidence_no"]

    r = client.post("/batches/B1/withdraw")
    # 撤 B1 只影响云和·秋沙鸭，景宁·角雉的证据号不变
    assert all(c["county"] == "云和县" for c in r.json()["registry_changes"])
    remaining = client.get("/registry").json()
    assert len(remaining) == 1
    assert remaining[0]["evidence_no"] == other_ev


def test_partial_withdraw_keeps_evidence_when_triple_survives(client):
    # B1 两笔 + B2 一笔组成证据；撤回 B2 后 B3 补上 → 换发；先验证撤无关批不动证据
    submit(client, "B1", [obs("O1", team="甲队", plot="样地A"), obs("O2", team="乙队", plot="样地B")])
    submit(client, "B2", [obs("O3", team="丙队", plot="样地C")])
    ev_before = client.get("/registry").json()[0]["evidence_no"]
    submit(client, "B3", [obs("O4", team="丁队", plot="样地D")])
    assert client.get("/registry").json()[0]["evidence_no"] == ev_before  # 原三笔有效 → 保留

    r = client.post("/batches/B2/withdraw")  # 原证据缺一笔 → 用 B3 的 O4 换发新号
    change = r.json()["registry_changes"][0]
    assert change["action"] == "replaced"
    ev = client.get(f"/evidence/{change['evidence_no']}").json()
    assert {o["obs_id"] for o in ev["observations"]} == {"O1", "O2", "O4"}


# ------------------------------------------------------------ 事务原子性


def test_failure_rolls_back_everything(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "tx.db"))
    monkeypatch.setattr(
        logic, "_revalidate_combo",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟复核失败")),
    )
    with pytest.raises(RuntimeError):
        logic.submit_batch(db, "B1", triple("O"))
    # 批次、观测、名录、证据全部不留痕
    for table in ("batches", "observations", "registry", "evidence", "evidence_obs"):
        assert db.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0
    db.close()


def test_concurrent_submissions_stay_consistent(tmp_path):
    """多线程并发上报不同批次：全部入账，名录只签发一个证据号。"""
    db = Database(str(tmp_path / "conc.db"))
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            logic.submit_batch(db, f"B{i:02d}", triple(f"O{i}-"))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert db.conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"] == 12
    assert db.conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()["n"] == 36
    reg = db.conn.execute("SELECT * FROM registry").fetchall()
    assert len(reg) == 1
    ev = db.conn.execute(
        "SELECT COUNT(*) AS n FROM evidence WHERE status = 'valid'"
    ).fetchone()["n"]
    assert ev == 1  # 只有一个有效证据，其余组合复核均为 kept
    db.close()
