"""丰富度比对与统计摘要：共同抽样量、期望种数、摘要维护、旧库回填、快照一致性。"""

from __future__ import annotations

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import logic
from app.db import Database
from app.main import create_app


def obs(obs_id, county, species, team, plot):
    return {"obs_id": obs_id, "county": county, "species": species,
            "team": team, "plot": plot}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.delenv("DEMO_SEED", raising=False)
    app = create_app()
    with TestClient(app) as c:
        yield c


def submit(client, batch_no, observations):
    return client.post("/batches", json={"batch_no": batch_no, "observations": observations})


def compare(client, *counties):
    return client.get("/compare", params=[("county", c) for c in counties])


def results_by_county(body):
    return {r["county"]: r for r in body["results"]}


# ------------------------------------------------------------ 比对计算


def test_compare_basic_expectation(client):
    # 甲县 4 样地：麻雀 3 样地、喜鹊 2 样地、乌鸦 1 样地（乌鸦仅一笔，未入册也计入）
    submit(client, "B1", [
        obs("A1", "甲县", "麻雀", "一队", "P1"),
        obs("A2", "甲县", "麻雀", "二队", "P2"),
        obs("A3", "甲县", "麻雀", "三队", "P3"),
        obs("A4", "甲县", "喜鹊", "二队", "P1"),
        obs("A5", "甲县", "乌鸦", "一队", "P4"),
        obs("A6", "甲县", "喜鹊", "三队", "P3"),
    ])
    # 乙县 2 样地：麻雀 2 样地、白鹭 1 样地
    submit(client, "B2", [
        obs("B1", "乙县", "麻雀", "一队", "Q1"),
        obs("B2", "乙县", "麻雀", "二队", "Q2"),
        obs("B3", "乙县", "白鹭", "三队", "Q1"),
    ])
    r = compare(client, "甲县", "乙县")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counties"] == ["甲县", "乙县"]
    assert body["common_sample_size"] == 2  # min(4, 2)
    res = results_by_county(body)
    assert res["甲县"]["valid_plots"] == 4
    assert res["乙县"]["valid_plots"] == 2
    # 甲县 N=4 抽 2：麻雀缺席概率 C(1,2)/C(4,2)=0 → 1；
    # 喜鹊 C(2,2)/C(4,2)=1/6 → 5/6；乌鸦 C(3,2)/C(4,2)=1/2 → 1/2
    assert res["甲县"]["expected_species"] == pytest.approx(1 + 5 / 6 + 1 / 2, abs=1e-6)
    # 乙县：抽 2 样地即全部，两种必现
    assert res["乙县"]["expected_species"] == pytest.approx(2.0, abs=1e-6)


def test_compare_dedupes_same_plot_species(client):
    # 同县同样地同种：批内重复、跨批重复都只算一次
    submit(client, "B1", [
        obs("A1", "甲县", "麻雀", "一队", "P1"),
        obs("A2", "甲县", "麻雀", "二队", "P1"),  # 批内同地同种
    ])
    submit(client, "B2", [
        obs("A3", "甲县", "麻雀", "三队", "P1"),  # 跨批同地同种
        obs("A4", "甲县", "喜鹊", "一队", "P2"),
    ])
    submit(client, "B3", [obs("B1", "乙县", "麻雀", "一队", "Q1")])
    body = compare(client, "甲县", "乙县").json()
    res = results_by_county(body)
    assert res["甲县"]["valid_plots"] == 2  # P1、P2，不是 3
    assert body["common_sample_size"] == 1
    # 甲县 N=2，麻雀/喜鹊各 1 样地：E = 2 × (1 - C(1,1)/C(2,1)) = 1.0
    assert res["甲县"]["expected_species"] == pytest.approx(1.0, abs=1e-6)
    assert res["乙县"]["expected_species"] == pytest.approx(1.0, abs=1e-6)


def test_compare_ignores_withdrawn_batches(client):
    submit(client, "B1", [obs("A1", "甲县", "麻雀", "一队", "P1")])
    submit(client, "B2", [obs("A2", "甲县", "喜鹊", "二队", "P2")])
    submit(client, "B3", [obs("B1", "乙县", "麻雀", "一队", "Q1")])
    client.post("/batches/B2/withdraw")
    body = compare(client, "甲县", "乙县").json()
    res = results_by_county(body)
    assert res["甲县"]["valid_plots"] == 1  # P2 随撤批移除


def test_compare_updates_after_withdraw(client):
    submit(client, "B1", [
        obs("A1", "甲县", "麻雀", "一队", "P1"),
        obs("A2", "甲县", "麻雀", "二队", "P2"),
    ])
    submit(client, "B2", [
        obs("A3", "甲县", "喜鹊", "一队", "P3"),
        obs("A4", "甲县", "乌鸦", "二队", "P4"),
    ])
    submit(client, "B3", [
        obs("B1", "乙县", "麻雀", "一队", "Q1"),
        obs("B2", "乙县", "白鹭", "二队", "Q2"),
    ])
    before = results_by_county(compare(client, "甲县", "乙县").json())
    # 甲县 N=4：麻雀 n=2 → 5/6；喜鹊、乌鸦 n=1 → 各 1/2
    assert before["甲县"]["expected_species"] == pytest.approx(11 / 6, abs=1e-6)
    assert before["乙县"]["expected_species"] == pytest.approx(2.0, abs=1e-6)

    client.post("/batches/B2/withdraw")
    after_body = compare(client, "甲县", "乙县").json()
    after = results_by_county(after_body)
    assert after_body["common_sample_size"] == 2
    assert after["甲县"]["valid_plots"] == 2
    assert after["甲县"]["expected_species"] == pytest.approx(1.0, abs=1e-6)
    assert after["乙县"]["expected_species"] == pytest.approx(2.0, abs=1e-6)

    # 乙县唯一批次撤回后缺数 → 422 并列出
    client.post("/batches/B3/withdraw")
    r = compare(client, "甲县", "乙县")
    assert r.status_code == 422
    assert r.json()["missing_counties"] == ["乙县"]


def test_compare_missing_county_422(client):
    submit(client, "B1", [obs("A1", "甲县", "麻雀", "一队", "P1")])
    r = compare(client, "甲县", "丙县")
    assert r.status_code == 422
    body = r.json()
    assert body["missing_counties"] == ["丙县"]
    assert "丙县" in body["detail"]


def test_compare_input_validation(client):
    submit(client, "B1", [obs("A1", "甲县", "麻雀", "一队", "P1")])
    assert compare(client, "甲县").status_code == 422          # 少于 2 个县
    assert compare(client, *[f"县{i}" for i in range(9)]).status_code == 422  # 多于 8 个
    r = compare(client, "甲县", "甲县")
    assert r.status_code == 422                                # 重复县名
    assert "重复" in r.json()["detail"]
    assert compare(client, "甲县", "  ").status_code == 422    # 空白县名
    assert client.get("/compare").status_code == 422           # 缺参数


def test_compare_accepts_eight_counties(client):
    """边界：8 个县可正常比对。"""
    for i in range(8):
        submit(client, f"B{i}", [obs(f"O{i}", f"县{i}", "麻雀", "一队", f"P{i}")])
    r = compare(client, *[f"县{i}" for i in range(8)])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["common_sample_size"] == 1
    assert len(body["results"]) == 8
    assert all(x["expected_species"] == pytest.approx(1.0) for x in body["results"])


def test_compare_idempotent_under_replay(client):
    payload = [obs("A1", "甲县", "麻雀", "一队", "P1")]
    submit(client, "B1", payload)
    submit(client, "B2", [obs("B1", "乙县", "喜鹊", "一队", "Q1")])
    first = compare(client, "甲县", "乙县").json()

    submit(client, "B1", payload)  # 原样重报：无操作
    assert compare(client, "甲县", "乙县").json() == first

    client.post("/batches/B1/withdraw")
    client.post("/batches/B1/withdraw")  # 重复撤回：无操作
    r = compare(client, "甲县", "乙县")
    assert r.status_code == 422
    assert r.json()["missing_counties"] == ["甲县"]

    submit(client, "B1", payload)  # 已撤回批次重报：仍不计入
    assert compare(client, "甲县", "乙县").json()["missing_counties"] == ["甲县"]


# ------------------------------------------------------------ 摘要与事务


def test_withdraw_failure_rolls_back_summary(tmp_path, monkeypatch):
    """撤批中途失败：批次状态与摘要都保持原数据。"""
    db = Database(str(tmp_path / "tx.db"))
    logic.submit_batch(db, "B1", [obs("A1", "甲县", "麻雀", "一队", "P1")])
    logic.submit_batch(db, "B2", [obs("B1", "乙县", "喜鹊", "一队", "Q1")])
    before = logic.compare_counties(db, ["甲县", "乙县"])

    monkeypatch.setattr(
        logic, "_revalidate_combo",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟复核失败")),
    )
    with pytest.raises(RuntimeError):
        logic.withdraw_batch(db, "B1")

    batch = db.conn.execute(
        "SELECT status FROM batches WHERE batch_no = 'B1'").fetchone()
    assert batch["status"] == "active"  # 批次未改动
    assert logic.compare_counties(db, ["甲县", "乙县"]) == before  # 摘要未改动
    db.close()


def test_compare_reads_only_committed_state(tmp_path, monkeypatch):
    """写事务进行中，比对查询等待其提交，只读同一已提交状态的摘要。"""
    db = Database(str(tmp_path / "snap.db"))
    logic.submit_batch(db, "B1", [obs("A1", "甲县", "麻雀", "一队", "P1")])

    entered = threading.Event()
    release = threading.Event()
    orig_apply = logic._apply_batch_to_summary

    def slow_apply(conn, batch_no, sign):
        orig_apply(conn, batch_no, sign)
        if batch_no == "B2":
            entered.set()          # 摘要已改但事务未提交
            assert release.wait(5)

    monkeypatch.setattr(logic, "_apply_batch_to_summary", slow_apply)
    submit_thread = threading.Thread(
        target=logic.submit_batch,
        args=(db, "B2", [obs("B1", "乙县", "喜鹊", "二队", "Q1")]),
    )
    submit_thread.start()
    assert entered.wait(5)

    outcome: dict = {}

    def run_compare():
        try:
            outcome["result"] = logic.compare_counties(db, ["甲县", "乙县"])
        except Exception as exc:  # pragma: no cover
            outcome["error"] = exc

    compare_thread = threading.Thread(target=run_compare)
    compare_thread.start()
    compare_thread.join(0.5)
    assert compare_thread.is_alive()  # 阻塞在写锁上，读不到未提交状态
    release.set()
    submit_thread.join(5)
    compare_thread.join(5)

    assert "error" not in outcome
    result = outcome["result"]
    assert result["common_sample_size"] == 1
    assert {r["county"] for r in result["results"]} == {"甲县", "乙县"}
    db.close()


# ------------------------------------------------------------ 旧库回填与重启

OLD_SCHEMA = """
CREATE TABLE batches (
    batch_no     TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('active', 'withdrawn')),
    received_at  TEXT NOT NULL,
    withdrawn_at TEXT
);
CREATE TABLE observations (
    batch_no TEXT NOT NULL REFERENCES batches (batch_no),
    obs_id   TEXT NOT NULL,
    county   TEXT NOT NULL,
    species  TEXT NOT NULL,
    team     TEXT NOT NULL,
    plot     TEXT NOT NULL,
    PRIMARY KEY (batch_no, obs_id)
);
CREATE TABLE counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


def build_legacy_db(path):
    """模拟旧版本留下的库：只有批次与观测，没有摘要表、没有回填标记。"""
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO batches VALUES ('B1', 'h1', 'active', '2026-01-01T00:00:00+00:00', NULL)")
    conn.execute(
        "INSERT INTO batches VALUES ('B2', 'h2', 'active', '2026-01-02T00:00:00+00:00', NULL)")
    conn.execute(
        "INSERT INTO batches VALUES ('B3', 'h3', 'withdrawn',"
        " '2026-01-03T00:00:00+00:00', '2026-01-04T00:00:00+00:00')")
    conn.executemany(
        "INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("B1", "A1", "甲县", "麻雀", "一队", "P1"),
            ("B1", "A2", "甲县", "麻雀", "二队", "P2"),
            ("B1", "A3", "甲县", "喜鹊", "三队", "P1"),
            ("B2", "A4", "甲县", "麻雀", "一队", "P1"),  # 与 B1 同地同种
            ("B2", "B1", "乙县", "麻雀", "二队", "Q1"),
            ("B3", "A5", "甲县", "乌鸦", "一队", "P9"),  # 已撤回，不计入
        ],
    )
    conn.commit()
    conn.close()


def test_legacy_db_backfilled_once(tmp_path):
    path = str(tmp_path / "legacy.db")
    build_legacy_db(path)

    db = Database(path)  # 打开旧库 → 自动回填摘要
    rows = db.conn.execute(
        "SELECT county, plot, species, batch_count FROM summary_plot_species"
    ).fetchall()
    got = {(r["county"], r["plot"], r["species"]): r["batch_count"] for r in rows}
    assert got == {
        ("甲县", "P1", "麻雀"): 2,  # B1、B2 两个有效批次
        ("甲县", "P2", "麻雀"): 1,
        ("甲县", "P1", "喜鹊"): 1,
        ("乙县", "Q1", "麻雀"): 1,
    }
    result = logic.compare_counties(db, ["甲县", "乙县"])
    res = {r["county"]: r for r in result["results"]}
    assert result["common_sample_size"] == 1
    assert res["甲县"]["valid_plots"] == 2
    assert res["甲县"]["expected_species"] == pytest.approx(1.5, abs=1e-6)
    db.close()

    # 重启（重开同一库）：摘要保持不变，不重复累计
    db2 = Database(path)
    again = db2.conn.execute(
        "SELECT county, plot, species, batch_count FROM summary_plot_species"
    ).fetchall()
    assert {(r["county"], r["plot"], r["species"]): r["batch_count"] for r in again} == got
    assert logic.compare_counties(db2, ["甲县", "乙县"]) == result
    db2.close()


def test_restart_does_not_double_count(tmp_path):
    """新版库正常上报后重启：摘要由增量维护，重开不重复累计。"""
    path = str(tmp_path / "restart.db")
    db = Database(path)
    logic.submit_batch(db, "B1", [obs("A1", "甲县", "麻雀", "一队", "P1")])
    logic.submit_batch(db, "B2", [obs("A2", "甲县", "麻雀", "二队", "P1")])
    logic.submit_batch(db, "B3", [obs("B1", "乙县", "喜鹊", "一队", "Q1")])
    before = logic.compare_counties(db, ["甲县", "乙县"])
    db.close()

    db2 = Database(path)
    row = db2.conn.execute(
        "SELECT batch_count FROM summary_plot_species"
        " WHERE county = '甲县' AND plot = 'P1' AND species = '麻雀'"
    ).fetchone()
    assert row["batch_count"] == 2  # 两个有效批次，未重复累计
    assert logic.compare_counties(db2, ["甲县", "乙县"]) == before
    db2.close()


# ------------------------------------------------------------ 演示数据剧情


@pytest.fixture()
def demo_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "demo.db"))
    monkeypatch.setenv("DEMO_SEED", "1")
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_demo_seed_compare_before_and_after_withdraw(demo_client):
    """README 中的 /docs 演示剧情：撤批前后比对结果都符合手算值。"""
    r = compare(demo_client, "云和县", "景宁畲族自治县")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["common_sample_size"] == 6  # min(8, 6)
    res = results_by_county(body)
    assert res["云和县"]["valid_plots"] == 8
    assert res["景宁畲族自治县"]["valid_plots"] == 6
    # 云和：秋沙鸭 n=6 → 必现；白颈长尾雉 n=2 → 1 - C(6,6)/C(8,6) = 27/28
    assert res["云和县"]["expected_species"] == pytest.approx(1 + 27 / 28, abs=1e-6)
    # 景宁：黄腹角雉 n=5、白鹇 n=2，抽 6 样地（全部）均必现
    assert res["景宁畲族自治县"]["expected_species"] == pytest.approx(2.0, abs=1e-6)

    demo_client.post("/batches/LY-2026-041/withdraw")
    body = compare(demo_client, "云和县", "景宁畲族自治县").json()
    assert body["common_sample_size"] == 3  # min(5, 3)
    res = results_by_county(body)
    assert res["云和县"]["valid_plots"] == 5
    assert res["景宁畲族自治县"]["valid_plots"] == 3
    # 云和：秋沙鸭 n=3 → 必现；白颈长尾雉 n=2 → 1 - C(3,3)/C(5,3) = 0.9
    assert res["云和县"]["expected_species"] == pytest.approx(1.9, abs=1e-6)
    assert res["景宁畲族自治县"]["expected_species"] == pytest.approx(2.0, abs=1e-6)
