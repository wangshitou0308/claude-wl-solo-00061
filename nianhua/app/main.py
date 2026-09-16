"""FastAPI 路由：年画木版交接核对 API（纯后端）。

约定：
- 所有写操作都需要在路径中指明 acting side（handover/receive）或见证人 id；
  系统不做"谁在调用"的身份认证，调用方自行保证角色归属。
- 409 + 业务错误码表示状态/冲突阻断，details.minimal_conflict_clues 为最小冲突线索。
"""
from __future__ import annotations

import os
from typing import Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import service
from .db import init_db
from .schemas import (
    BatchCreate, DecisionIn, FreezeIn, GroupAssign, GroupConfirm, InventorySubmit,
    ItemCorrection, ObservationCreate, PartyRegister, Side, StatusConfirm,
    WitnessRegister,
)

# Pydantic v2 + postponed annotations：显式重建，确保 OpenAPI 能解析可选 body
FreezeIn.model_rebuild()
GroupConfirm.model_rebuild()

DB_PATH = os.environ.get("NIANHUA_DB")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db(DB_PATH)
    yield


app = FastAPI(
    title="年画木版交接核对 API",
    version="1.0.0",
    description="双方清点—套组—状态三阶段核对；硬冲突排除、证据不足保留候选、共同确认后冻结。",
    lifespan=lifespan,
)


@app.exception_handler(service.ApiError)
async def _api_error(_: Request, exc: service.ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": exc.message,
                           "details": exc.details}},
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------------- 批次与人员

@app.post("/batches", status_code=201)
def create_batch(body: BatchCreate) -> dict:
    return service.create_batch(body.model_dump(exclude_none=True), DB_PATH)


@app.get("/batches/{batch_id}")
def get_batch(batch_id: str) -> dict:
    return service.get_batch(batch_id, DB_PATH)


@app.post("/batches/{batch_id}/parties", status_code=201)
def register_party(batch_id: str, body: PartyRegister) -> dict:
    return service.register_party(
        batch_id, body.model_dump(exclude_none=True), DB_PATH)


@app.post("/batches/{batch_id}/witnesses", status_code=201)
def register_witness(batch_id: str, body: WitnessRegister) -> dict:
    return service.register_witness(
        batch_id, body.model_dump(exclude_none=True), DB_PATH)


# ---------------- 核对报告

@app.get("/batches/{batch_id}/report")
def report(batch_id: str) -> dict:
    return service.build_report(batch_id, DB_PATH)


@app.get("/batches/{batch_id}/events")
def events(batch_id: str, limit: int = 200) -> dict:
    return service.get_events(batch_id, limit, DB_PATH)


# ---------------- 阶段一：清点

@app.put("/batches/{batch_id}/sides/{side}/inventory")
def submit_inventory(batch_id: str, side: Side, body: InventorySubmit) -> dict:
    data = body.model_dump(exclude_none=True)
    # gaps 元组已被 dump 成 list；service/recon 均兼容
    return service.submit_inventory(batch_id, side, data, DB_PATH)


@app.post("/batches/{batch_id}/sides/{side}/inventory/confirm")
def confirm_inventory(batch_id: str, side: Side) -> dict:
    return service.confirm_inventory(batch_id, side, DB_PATH)


@app.put("/batches/{batch_id}/sides/{side}/items/{code}")
def correct_item(batch_id: str, side: Side, code: str, body: ItemCorrection) -> dict:
    data = body.model_dump(exclude_none=True)
    return service.correct_item(batch_id, side, code, data, DB_PATH)


# ---------------- 阶段二：套组

@app.put("/batches/{batch_id}/sides/{side}/groups")
def assign_groups(batch_id: str, side: Side, body: GroupAssign) -> dict:
    return service.assign_groups(
        batch_id, side, body.model_dump(exclude_none=True), DB_PATH)


@app.post("/batches/{batch_id}/sides/{side}/groups/confirm")
def confirm_grouping(batch_id: str, side: Side, body: Optional[GroupConfirm] = None) -> dict:
    data = body.model_dump(exclude_none=True) if body is not None else {}
    return service.confirm_grouping(batch_id, side, data, DB_PATH)


# ---------------- 阶段三：状态

@app.post("/batches/{batch_id}/sides/{side}/status/confirm")
def confirm_status(batch_id: str, side: Side, body: StatusConfirm) -> dict:
    return service.confirm_status(
        batch_id, side, body.model_dump(exclude_none=True), DB_PATH)


@app.post("/batches/{batch_id}/sides/{side}/withdraw")
def withdraw_last(batch_id: str, side: Side) -> dict:
    return service.withdraw_last(batch_id, side, DB_PATH)


# ---------------- 见证

@app.post("/batches/{batch_id}/witnesses/{witness_id}/observations", status_code=201)
def add_observation(batch_id: str, witness_id: str, body: ObservationCreate) -> dict:
    return service.add_observation(
        batch_id, witness_id, body.model_dump(exclude_none=True), DB_PATH)


@app.post("/batches/{batch_id}/observations/{observation_id}/sides/{side}/decision")
def decide_observation(batch_id: str, observation_id: str, side: Side,
                       body: DecisionIn) -> dict:
    return service.decide_observation(
        batch_id, observation_id, side, body.model_dump(exclude_none=True), DB_PATH)


# ---------------- 冻结

@app.post("/batches/{batch_id}/freeze")
def freeze(batch_id: str, body: Optional[FreezeIn] = None) -> dict:
    data = body.model_dump(exclude_none=True) if body is not None else {}
    return service.freeze(batch_id, data, DB_PATH)


@app.get("/batches/{batch_id}/freeze")
def get_freeze(batch_id: str) -> dict:
    return service.get_freeze_record(batch_id, DB_PATH)
