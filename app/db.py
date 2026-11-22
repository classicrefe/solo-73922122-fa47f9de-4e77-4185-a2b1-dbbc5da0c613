"""SQLite 存储层：单连接 + 写锁，事务由逻辑层显式控制。"""

from __future__ import annotations

import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_no     TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('active', 'withdrawn')),
    received_at  TEXT NOT NULL,
    withdrawn_at TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    batch_no TEXT NOT NULL REFERENCES batches (batch_no),
    obs_id   TEXT NOT NULL,
    county   TEXT NOT NULL,
    species  TEXT NOT NULL,
    team     TEXT NOT NULL,
    plot     TEXT NOT NULL,
    PRIMARY KEY (batch_no, obs_id)
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_no TEXT PRIMARY KEY,
    county      TEXT NOT NULL,
    species     TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('valid', 'invalid')),
    created_at  TEXT NOT NULL,
    replaced_by TEXT
);

CREATE TABLE IF NOT EXISTS evidence_obs (
    evidence_no TEXT NOT NULL REFERENCES evidence (evidence_no),
    batch_no    TEXT NOT NULL,
    obs_id      TEXT NOT NULL,
    PRIMARY KEY (evidence_no, batch_no, obs_id)
);

CREATE TABLE IF NOT EXISTS registry (
    county      TEXT NOT NULL,
    species     TEXT NOT NULL,
    evidence_no TEXT NOT NULL REFERENCES evidence (evidence_no),
    listed_at   TEXT NOT NULL,
    PRIMARY KEY (county, species)
);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

-- 统计摘要：有效批次贡献的 (县, 样地, 鸟种) 去重台账。
-- batch_count = 当前包含该组合的有效批次数（同批内重复只算一次），
-- 上报 +1 / 撤批 -1，归零即删；比对查询只读本表，不回扫观测明细。
CREATE TABLE IF NOT EXISTS summary_plot_species (
    county      TEXT NOT NULL,
    plot        TEXT NOT NULL,
    species     TEXT NOT NULL,
    batch_count INTEGER NOT NULL CHECK (batch_count > 0),
    PRIMARY KEY (county, plot, species)
);

CREATE INDEX IF NOT EXISTS idx_obs_combo ON observations (county, species);
CREATE INDEX IF NOT EXISTS idx_evidence_obs ON evidence_obs (evidence_no);
"""

# 旧库一次性回填：按有效批次的观测明细重建整份摘要。
# 与 logic._apply_batch_to_summary 的增量口径一致：
# 每批对同一 (县, 样地, 鸟种) 只贡献 1。
SUMMARY_BACKFILL = """
INSERT INTO summary_plot_species (county, plot, species, batch_count)
SELECT o.county, o.plot, o.species, COUNT(DISTINCT o.batch_no)
FROM observations o
JOIN batches b ON b.batch_no = o.batch_no
WHERE b.status = 'active'
GROUP BY o.county, o.plot, o.species
"""

SUMMARY_MARKER = "summary_built"


class Database:
    """进程内单例连接。isolation_level=None 表示自动提交，
    写操作由逻辑层用 BEGIN IMMEDIATE / COMMIT / ROLLBACK 显式包裹，
    并由同一把可重入锁串行化，保证批次、名录、证据、统计摘要同生共死。"""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._migrate_summary()

    def _migrate_summary(self) -> None:
        """旧库回填统计摘要：无标记时按有效批次重建并同事务打标。

        重建与标记在同一事务提交，崩溃重开自动重试；已有标记则跳过，
        重启不会重复累计。回填前先清空，保证中途失败不留半份数据。
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            marker = self.conn.execute(
                "SELECT value FROM counters WHERE name = ?", (SUMMARY_MARKER,)
            ).fetchone()
            if marker is None:
                self.conn.execute("DELETE FROM summary_plot_species")
                self.conn.execute(SUMMARY_BACKFILL)
                self.conn.execute(
                    "INSERT INTO counters (name, value) VALUES (?, 1)", (SUMMARY_MARKER,)
                )
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.conn.close()
