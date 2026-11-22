"""名录登记核心逻辑：批次接收/撤回、按（县, 鸟种）组合复核、证据签发与失效。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .db import Database

EVIDENCE_COUNTER = "evidence"


class NotFoundError(Exception):
    """资源不存在。"""


class ConflictError(Exception):
    """与已有状态冲突（如批号相同但内容不同）。"""


class ValidationError(Exception):
    """领域校验失败（如批内观测号重复）。"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def payload_hash(batch_no: str, observations: list[dict[str, Any]]) -> str:
    """批次内容的规范化哈希，用于识别“原样重报”与“异内容重报”。"""
    canon = json.dumps(
        {
            "batch_no": batch_no,
            "observations": sorted(observations, key=lambda o: o["obs_id"]),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 内部工具


def _next_evidence_no(conn: sqlite3.Connection) -> str:
    conn.execute(
        "INSERT INTO counters (name, value) VALUES (?, 0) ON CONFLICT (name) DO NOTHING",
        (EVIDENCE_COUNTER,),
    )
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (EVIDENCE_COUNTER,))
    value = conn.execute(
        "SELECT value FROM counters WHERE name = ?", (EVIDENCE_COUNTER,)
    ).fetchone()["value"]
    return f"EV-{value:06d}"


def _find_triple(
    conn: sqlite3.Connection, county: str, species: str
) -> list[sqlite3.Row] | None:
    """从有效批次中确定性选出三笔：调查队两两不同、样地两两不同。"""
    rows = conn.execute(
        """
        SELECT o.batch_no, o.obs_id, o.team, o.plot
        FROM observations o
        JOIN batches b ON b.batch_no = o.batch_no
        WHERE b.status = 'active' AND o.county = ? AND o.species = ?
        ORDER BY b.received_at, o.batch_no, o.obs_id
        """,
        (county, species),
    ).fetchall()
    used_teams: set[str] = set()
    used_plots: set[str] = set()
    picked: list[sqlite3.Row] = []
    for row in rows:
        if row["team"] in used_teams or row["plot"] in used_plots:
            continue
        picked.append(row)
        used_teams.add(row["team"])
        used_plots.add(row["plot"])
        if len(picked) == 3:
            return picked
    return None


def _issue_evidence(
    conn: sqlite3.Connection, county: str, species: str, triple: list[sqlite3.Row]
) -> str:
    evidence_no = _next_evidence_no(conn)
    now = utcnow()
    conn.execute(
        "INSERT INTO evidence (evidence_no, county, species, status, created_at)"
        " VALUES (?, ?, ?, 'valid', ?)",
        (evidence_no, county, species, now),
    )
    conn.executemany(
        "INSERT INTO evidence_obs (evidence_no, batch_no, obs_id) VALUES (?, ?, ?)",
        [(evidence_no, t["batch_no"], t["obs_id"]) for t in triple],
    )
    return evidence_no


def _revalidate_combo(
    conn: sqlite3.Connection, county: str, species: str
) -> list[dict[str, Any]]:
    """复核单个（县, 鸟种）组合。只在批次变动波及的组合上调用。"""
    changes: list[dict[str, Any]] = []
    base = {"county": county, "species": species}
    reg = conn.execute(
        "SELECT evidence_no FROM registry WHERE county = ? AND species = ?",
        (county, species),
    ).fetchone()

    if reg is not None:
        old_no = reg["evidence_no"]
        sources = conn.execute(
            """
            SELECT b.status AS batch_status
            FROM evidence_obs eo
            JOIN batches b ON b.batch_no = eo.batch_no
            WHERE eo.evidence_no = ?
            """,
            (old_no,),
        ).fetchall()
        if all(s["batch_status"] == "active" for s in sources):
            # 原三笔都有效 → 保留原证据号
            return [{**base, "action": "kept", "evidence_no": old_no}]

        conn.execute("UPDATE evidence SET status = 'invalid' WHERE evidence_no = ?", (old_no,))
        triple = _find_triple(conn, county, species)
        if triple is not None:
            new_no = _issue_evidence(conn, county, species, triple)
            conn.execute(
                "UPDATE registry SET evidence_no = ?, listed_at = ?"
                " WHERE county = ? AND species = ?",
                (new_no, utcnow(), county, species),
            )
            conn.execute(
                "UPDATE evidence SET replaced_by = ? WHERE evidence_no = ?",
                (new_no, old_no),
            )
            changes.append(
                {**base, "action": "replaced", "old_evidence_no": old_no, "evidence_no": new_no}
            )
        else:
            conn.execute(
                "DELETE FROM registry WHERE county = ? AND species = ?", (county, species)
            )
            changes.append({**base, "action": "delisted", "old_evidence_no": old_no})
        return changes

    triple = _find_triple(conn, county, species)
    if triple is not None:
        evidence_no = _issue_evidence(conn, county, species, triple)
        conn.execute(
            "INSERT INTO registry (county, species, evidence_no, listed_at) VALUES (?, ?, ?, ?)",
            (county, species, evidence_no, utcnow()),
        )
        changes.append({**base, "action": "listed", "evidence_no": evidence_no})
    else:
        changes.append({**base, "action": "none"})
    return changes


# ---------------------------------------------------------------- 写操作


def submit_batch(
    db: Database, batch_no: str, observations: list[dict[str, Any]]
) -> dict[str, Any]:
    if not observations:
        raise ValidationError("观测清单不能为空")
    obs_ids = [o["obs_id"] for o in observations]
    if len(set(obs_ids)) != len(obs_ids):
        raise ValidationError("同一批次内观测号重复")

    digest = payload_hash(batch_no, observations)
    with db.lock:
        conn = db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT payload_hash, status FROM batches WHERE batch_no = ?", (batch_no,)
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] == digest:
                    # 同批号原样重报：不改状态（已撤回的保持撤回）
                    conn.execute("COMMIT")
                    return {
                        "batch_no": batch_no,
                        "status": existing["status"],
                        "accepted": len(observations),
                        "replayed": True,
                        "registry_changes": [],
                        "message": "同批号同内容重报，状态不变",
                    }
                raise ConflictError(f"批号 {batch_no} 已入账且内容不同，拒收")

            conn.execute(
                "INSERT INTO batches (batch_no, payload_hash, status, received_at)"
                " VALUES (?, ?, 'active', ?)",
                (batch_no, digest, utcnow()),
            )
            conn.executemany(
                "INSERT INTO observations (batch_no, obs_id, county, species, team, plot)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (batch_no, o["obs_id"], o["county"], o["species"], o["team"], o["plot"])
                    for o in observations
                ],
            )
            combos = sorted({(o["county"], o["species"]) for o in observations})
            changes: list[dict[str, Any]] = []
            for county, species in combos:
                changes.extend(_revalidate_combo(conn, county, species))
            conn.execute("COMMIT")
            return {
                "batch_no": batch_no,
                "status": "active",
                "accepted": len(observations),
                "replayed": False,
                "registry_changes": changes,
            }
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def withdraw_batch(db: Database, batch_no: str) -> dict[str, Any]:
    with db.lock:
        conn = db.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status FROM batches WHERE batch_no = ?", (batch_no,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"批号 {batch_no} 不存在")
            if row["status"] == "withdrawn":
                # 重复撤回：不改状态
                conn.execute("COMMIT")
                return {
                    "batch_no": batch_no,
                    "status": "withdrawn",
                    "replayed": True,
                    "registry_changes": [],
                    "message": "该批次此前已撤回，状态不变",
                }

            conn.execute(
                "UPDATE batches SET status = 'withdrawn', withdrawn_at = ? WHERE batch_no = ?",
                (utcnow(), batch_no),
            )
            combos = conn.execute(
                "SELECT DISTINCT county, species FROM observations WHERE batch_no = ?"
                " ORDER BY county, species",
                (batch_no,),
            ).fetchall()
            changes: list[dict[str, Any]] = []
            for combo in combos:
                changes.extend(_revalidate_combo(conn, combo["county"], combo["species"]))
            conn.execute("COMMIT")
            return {
                "batch_no": batch_no,
                "status": "withdrawn",
                "replayed": False,
                "registry_changes": changes,
            }
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


# ---------------------------------------------------------------- 查询


def get_registry(db: Database) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        """
        SELECT r.county, r.species, r.evidence_no, r.listed_at, e.status AS evidence_status
        FROM registry r
        JOIN evidence e ON e.evidence_no = r.evidence_no
        ORDER BY r.county, r.species
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_evidence(db: Database, evidence_no: str) -> dict[str, Any]:
    ev = db.conn.execute(
        "SELECT * FROM evidence WHERE evidence_no = ?", (evidence_no,)
    ).fetchone()
    if ev is None:
        raise NotFoundError(f"证据号 {evidence_no} 不存在")
    obs = db.conn.execute(
        """
        SELECT eo.batch_no, eo.obs_id, o.county, o.species, o.team, o.plot,
               b.status AS batch_status
        FROM evidence_obs eo
        JOIN observations o ON o.batch_no = eo.batch_no AND o.obs_id = eo.obs_id
        JOIN batches b ON b.batch_no = eo.batch_no
        WHERE eo.evidence_no = ?
        ORDER BY eo.batch_no, eo.obs_id
        """,
        (evidence_no,),
    ).fetchall()
    return {
        "evidence_no": ev["evidence_no"],
        "county": ev["county"],
        "species": ev["species"],
        "status": ev["status"],
        "replaced_by": ev["replaced_by"],
        "created_at": ev["created_at"],
        "observations": [dict(o) for o in obs],
        "all_source_batches_active": all(o["batch_status"] == "active" for o in obs),
    }


def get_batch(db: Database, batch_no: str) -> dict[str, Any]:
    batch = db.conn.execute(
        "SELECT batch_no, status, received_at, withdrawn_at FROM batches WHERE batch_no = ?",
        (batch_no,),
    ).fetchone()
    if batch is None:
        raise NotFoundError(f"批号 {batch_no} 不存在")
    obs = db.conn.execute(
        "SELECT obs_id, county, species, team, plot FROM observations"
        " WHERE batch_no = ? ORDER BY obs_id",
        (batch_no,),
    ).fetchall()
    return {**dict(batch), "observations": [dict(o) for o in obs]}


def list_batches(db: Database) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        """
        SELECT b.batch_no, b.status, b.received_at, b.withdrawn_at,
               COUNT(o.obs_id) AS observation_count
        FROM batches b
        LEFT JOIN observations o ON o.batch_no = b.batch_no
        GROUP BY b.batch_no
        ORDER BY b.received_at, b.batch_no
        """
    ).fetchall()
    return [dict(r) for r in rows]
