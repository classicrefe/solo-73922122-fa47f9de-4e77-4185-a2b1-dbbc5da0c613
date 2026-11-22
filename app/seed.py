"""演示数据：DEMO_SEED=1 且数据库为空时写入，便于在 /docs 直接演示取数与撤批。

演示剧情：
- LY-2026-041：云和县·中华秋沙鸭 三笔（三队三样地）→ 入册 EV-000001；
  景宁畲族自治县·黄腹角雉 三笔 → 入册 EV-000002。
- LY-2026-042：云和县·中华秋沙鸭 备选三笔（原证据仍有效，保留 EV-000001）；
  云和县·白颈长尾雉 三笔但两笔同样地 → 不满足条件，不入册。
- 在 /docs 撤回 LY-2026-041：中华秋沙鸭换发新证据号（由 042 批补足），
  黄腹角雉无替代 → 移出名录，旧证据号仍可查、标为失效。
"""

from __future__ import annotations

from .db import Database
from . import logic

DEMO_BATCHES: list[dict] = [
    {
        "batch_no": "LY-2026-041",
        "observations": [
            {"obs_id": "OBS-001", "county": "云和县", "species": "中华秋沙鸭",
             "team": "丽水调查一队", "plot": "浮云溪样地"},
            {"obs_id": "OBS-002", "county": "云和县", "species": "中华秋沙鸭",
             "team": "云和调查队", "plot": "规溪样地"},
            {"obs_id": "OBS-003", "county": "云和县", "species": "中华秋沙鸭",
             "team": "高校联合队", "plot": "长汀样地"},
            {"obs_id": "OBS-004", "county": "景宁畲族自治县", "species": "黄腹角雉",
             "team": "丽水调查一队", "plot": "望东垟样地"},
            {"obs_id": "OBS-005", "county": "景宁畲族自治县", "species": "黄腹角雉",
             "team": "景宁调查队", "plot": "上山头样地"},
            {"obs_id": "OBS-006", "county": "景宁畲族自治县", "species": "黄腹角雉",
             "team": "高校联合队", "plot": "大漈样地"},
        ],
    },
    {
        "batch_no": "LY-2026-042",
        "observations": [
            {"obs_id": "OBS-101", "county": "云和县", "species": "中华秋沙鸭",
             "team": "野生动物保护协会", "plot": "梅源样地"},
            {"obs_id": "OBS-102", "county": "云和县", "species": "中华秋沙鸭",
             "team": "丽水调查二队", "plot": "石塘样地"},
            {"obs_id": "OBS-103", "county": "云和县", "species": "中华秋沙鸭",
             "team": "志愿者观测队", "plot": "崇头样地"},
            {"obs_id": "OBS-104", "county": "云和县", "species": "白颈长尾雉",
             "team": "丽水调查一队", "plot": "雾溪样地"},
            {"obs_id": "OBS-105", "county": "云和县", "species": "白颈长尾雉",
             "team": "云和调查队", "plot": "雾溪样地"},
            {"obs_id": "OBS-106", "county": "云和县", "species": "白颈长尾雉",
             "team": "高校联合队", "plot": "沙铺样地"},
        ],
    },
]


def seed_if_empty(db: Database) -> list[str]:
    """数据库为空时写入演示批次，返回已写入的批号列表。"""
    with db.lock:
        count = db.conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"]
        if count:
            return []
    seeded: list[str] = []
    for batch in DEMO_BATCHES:
        logic.submit_batch(db, batch["batch_no"], batch["observations"])
        seeded.append(batch["batch_no"])
    return seeded
