"""FastAPI 入口：县域鸟类分布名录供数 API。"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import logic, models, seed
from .db import Database

DESCRIPTION = """\
县域鸟类分布名录供数 API。

* **POST /batches** 上报一批观测（带批号）；同县同种凑齐三笔有效观测
  （三支不同调查队、三处不同样地）即入册并签发证据号。
* **POST /batches/{batch_no}/withdraw** 撤回整批；只复核该批涉及的县与鸟种组合，
  原证据失效时另找三笔换发新号，无替代则移出名录。
* **GET /registry** 当前名录（县、鸟种、证据号）。
* **GET /evidence/{evidence_no}** 按证据号追溯原三笔的批号、观测号及证据有效性；
  已失效的旧证据号仍可查询。
* **GET /compare?county=甲县&county=乙县** 2～8 个县按共同抽样量比对鸟种丰富度：
  只取有效批次，同县同样地同种只算一次（未入册鸟种也计入），
  以各县有效样地数的最小值为共同抽样量，返回各县等概率、不放回抽样时
  不同鸟种数的期望；有县缺少有效样地时返回 422 并列出缺数的县。
"""


def create_app() -> FastAPI:
    db_path = os.environ.get("DB_PATH", str(Path.cwd() / "registry.db"))
    db = Database(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if os.environ.get("DEMO_SEED") == "1":
            seeded = seed.seed_if_empty(db)
            if seeded:
                print(f"[demo] 已写入演示批次: {', '.join(seeded)}")
        yield
        db.close()

    app = FastAPI(
        title="县域鸟类分布名录供数 API",
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.db = db

    @app.exception_handler(logic.NotFoundError)
    async def not_found_handler(_: Request, exc: logic.NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(logic.ConflictError)
    async def conflict_handler(_: Request, exc: logic.ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(logic.ValidationError)
    async def validation_handler(_: Request, exc: logic.ValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(logic.InsufficientDataError)
    async def insufficient_handler(
        _: Request, exc: logic.InsufficientDataError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": str(exc), "missing_counties": exc.missing},
        )

    # ------------------------------------------------------------ 写接口

    @app.post(
        "/batches",
        response_model=models.BatchSubmitResponse,
        summary="上报一批观测清单",
    )
    def submit_batch(submission: models.BatchSubmission) -> dict:
        """接收带批号的观测清单。同批号原样重报幂等不改状态，异内容重报返回 409。"""
        return logic.submit_batch(
            db,
            submission.batch_no,
            [o.model_dump() for o in submission.observations],
        )

    @app.post(
        "/batches/{batch_no}/withdraw",
        response_model=models.BatchWithdrawResponse,
        summary="撤回整批观测",
    )
    def withdraw_batch(batch_no: str) -> dict:
        """撤回整批并复核受影响的（县, 鸟种）组合。重复撤回幂等不改状态。"""
        return logic.withdraw_batch(db, batch_no)

    # ------------------------------------------------------------ 供数接口

    @app.get(
        "/registry",
        response_model=list[models.RegistryEntry],
        summary="当前名录（县 × 鸟种 × 证据号）",
    )
    def get_registry() -> list[dict]:
        return logic.get_registry(db)

    @app.get(
        "/evidence/{evidence_no}",
        response_model=models.EvidenceDetail,
        summary="按证据号追溯原三笔观测",
    )
    def get_evidence(evidence_no: str) -> dict:
        """返回该证据对应三笔观测的批号、观测号，以及这组证据是否有效。"""
        return logic.get_evidence(db, evidence_no)

    @app.get(
        "/batches",
        response_model=list[models.BatchSummary],
        summary="批次列表",
    )
    def list_batches() -> list[dict]:
        return logic.list_batches(db)

    @app.get(
        "/batches/{batch_no}",
        response_model=models.BatchDetail,
        summary="批次明细",
    )
    def get_batch(batch_no: str) -> dict:
        return logic.get_batch(db, batch_no)

    @app.get(
        "/compare",
        response_model=models.CompareResponse,
        summary="多县鸟种丰富度比对（共同抽样量下的期望种数）",
    )
    def compare(
        counties: list[models.CountyName] = Query(
            alias="county",
            description="参与比对的县名（2～8 个、互不相同，"
            "需与上报时的县名完全一致），重复传入该参数",
        ),
    ) -> dict:
        """按共同抽样量比对：返回共同抽样量、各县有效样地数与期望鸟种数。

        只统计有效批次；同县同样地同种的重复观测只算一次，未入册鸟种也计入。
        有县缺少有效样地时返回 422 并在 missing_counties 中列出缺数的县。
        """
        return logic.compare_counties(db, counties)

    return app


app = create_app()
