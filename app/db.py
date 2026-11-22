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

CREATE INDEX IF NOT EXISTS idx_obs_combo ON observations (county, species);
CREATE INDEX IF NOT EXISTS idx_evidence_obs ON evidence_obs (evidence_no);
"""


class Database:
    """进程内单例连接。isolation_level=None 表示自动提交，
    写操作由逻辑层用 BEGIN IMMEDIATE / COMMIT / ROLLBACK 显式包裹，
    并由同一把可重入锁串行化，保证批次、名录、证据三者同生共死。"""

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

    def close(self) -> None:
        self.conn.close()
