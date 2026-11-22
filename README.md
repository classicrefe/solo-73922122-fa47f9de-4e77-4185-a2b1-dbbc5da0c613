# 县域鸟类分布名录供数 API

FastAPI + SQLite 实现的县域鸟类分布名录服务：接收带批号的观测清单，满足
“同县同种三笔有效观测、三支不同调查队、三处不同样地”即入册并签发证据号；
下游可按证据号追溯原三笔的批号、观测号及证据有效性。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

服务启动后打开 <http://localhost:8000/docs>（Swagger UI）。
首次启动且库为空时自动写入两批演示数据（`DEMO_SEED=1`，见 `app/seed.py`）：

| 批号 | 内容 | 结果 |
|---|---|---|
| `LY-2026-041` | 云和县·中华秋沙鸭 3 笔（三队三样地）；景宁畲族自治县·黄腹角雉 3 笔 | 入册 `EV-000001`、`EV-000002` |
| `LY-2026-042` | 云和县·中华秋沙鸭 备选 3 笔；云和县·白颈长尾雉 3 笔（两笔同样地） | 秋沙鸭保留原证据号；白颈长尾雉不满足“三处不同样地”，不入册 |

### 在 /docs 演示取数与撤批

1. **取数**：`GET /registry` 查看名录；`GET /evidence/EV-000001` 查看原三笔的
   批号、观测号与证据有效性。
2. **撤批**：`POST /batches/LY-2026-041/withdraw`，返回的 `registry_changes` 显示：
   - 云和县·中华秋沙鸭：`replaced`，`EV-000001` → `EV-000003`（由 042 批补足三笔）；
   - 景宁畲族自治县·黄腹角雉：`delisted`，无替代观测，移出名录。
3. **追溯**：`GET /evidence/EV-000001` 仍可查，`status=invalid`，
   `replaced_by=EV-000003`；`GET /evidence/EV-000002` 标为失效。
4. **幂等**：再次 `POST /batches/LY-2026-041/withdraw` 返回 `replayed=true`，
   状态不变；同批号原样重报同样幂等；同批号异内容重报返回 **409**。

等价的 curl 命令：

```bash
curl http://localhost:8000/registry
curl http://localhost:8000/evidence/EV-000001
curl -X POST http://localhost:8000/batches/LY-2026-041/withdraw
curl http://localhost:8000/evidence/EV-000001   # 旧号仍可查，标为失效
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/batches` | 上报一批观测（批号 + 观测清单：观测号/县/鸟种/调查队/样地） |
| POST | `/batches/{batch_no}/withdraw` | 撤回整批，复核受影响的（县, 鸟种）组合 |
| GET | `/registry` | 当前名录：县 × 鸟种 × 证据号 |
| GET | `/evidence/{evidence_no}` | 证据追溯：原三笔的批号、观测号、有效性 |
| GET | `/batches` / `/batches/{batch_no}` | 批次列表 / 明细 |

## 规则实现要点

- **入册**：同县同种在有效批次中选出三笔，调查队两两不同且样地两两不同
  （按上报时间确定性贪心选取，见 `app/logic.py:_find_triple`）。
- **复核范围**：批次上报/撤回只重算该批涉及的（县, 鸟种）组合。
- **保留原号**：复核时原三笔仍全部有效 → 保留原证据号，不换发。
- **失效换发**：原证据任一来源批次被撤回 → 旧证据号标 `invalid`；
  找到替代三笔则签发新号（`replaced_by` 串联新旧号），找不到则移出名录。
  旧证据号永久可查。
- **幂等**：批次内容做规范化 SHA-256 哈希。同批号同内容重报、重复撤回均为
  无操作；同批号异内容重报返回 409 拒收。
- **事务一致**：所有写操作在进程内写锁 + `BEGIN IMMEDIATE` 事务中完成，
  批次、观测、名录、证据同事务提交；任一步失败整体回滚，保留原状态。

## 本地开发与测试

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest            # 16 个用例
.venv/bin/uvicorn app.main:app --reload   # DEMO_SEED=1 可带演示数据
```

## 已知限制

- 单进程单写者（SQLite + 进程内锁）；多副本水平扩展需换服务端数据库
  （如 PostgreSQL），事务与幂等逻辑可直接平移。
- 证据号在单库内单调递增，不回收。
- 未做认证鉴权，内网供数场景假设。
