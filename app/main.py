"""FastAPI 应用：上传、锁定/解锁、求解作业、确认、下载。"""
from __future__ import annotations

import csv
import io
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import ValidationError as PydanticValidationError

from . import db
from .parsing import (
    ValidationError as CsvValidationError,
    parse_allowed_csv,
    parse_inspection_csv,
    validate_allowed_dims,
)
from .schemas import AllowedPairsPayload, LockIn
from .solver import LockSpec, solve


# 串行后台求解器：单线程保证求解作业按序执行，作业内部自行做版本检查。
# 每个应用实例（lifespan）持有独立池，避免测试中多实例互相关闭。
_job_pool: Optional[ThreadPoolExecutor] = None


def _pool() -> ThreadPoolExecutor:
    global _job_pool
    if _job_pool is None:
        _job_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="solver")
    return _job_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _job_pool
    _job_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="solver")
    db.init_db()
    yield
    _job_pool.shutdown(wait=False)
    _job_pool = None


app = FastAPI(title="育种巡检数据修复", version="1.0.0", lifespan=lifespan)
STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------- 上传新建会话
@app.post("/api/sessions")
async def create_session(
    inspection: UploadFile = File(...),
    allowed: Optional[UploadFile] = File(None),
    allowed_json: Optional[str] = Form(None),
) -> JSONResponse:
    raw = await inspection.read()
    try:
        parsed_rows = parse_inspection_csv(raw)
        if allowed is not None:
            pairs = parse_allowed_csv(await allowed.read())
        elif allowed_json:
            payload = AllowedPairsPayload.model_validate_json(allowed_json)
            pairs = [(p.variety, p.house) for p in payload.pairs]
            validate_allowed_dims(pairs)
        else:
            raise CsvValidationError("必须提供允许组合（CSV 文件或表单 JSON）。")
    except CsvValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PydanticValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors()) from exc

    rows = [r.__dict__ for r in parsed_rows]
    sid = db.create_session(rows, pairs)
    revision = db.current_revision(sid)
    job_id = db.create_job(sid, revision)
    _pool().submit(_run_job, sid, job_id, revision)
    return JSONResponse(
        status_code=201,
        content={"session_id": sid, "revision": revision, "job_id": job_id},
    )


# ---------------------------------------------------------------- 会话详情
@app.get("/api/sessions/{sid}")
def session_detail(sid: int) -> dict:
    data = db.get_session(sid)
    if data is None:
        raise HTTPException(status_code=404, detail="会话不存在。")
    return _serialize_session(sid, data)


def _serialize_session(sid: int, data: dict) -> dict:
    s, rows, pairs = data["session"], data["rows"], data["pairs"]
    locks = {(l["row_id"], l["field"]): l for l in data["locks"]}
    solution = db.get_solution(sid)
    confirmation = db.get_confirmation(sid)
    current_rev = s["revision"]

    cells = []
    conflicts_out: List[dict] = []
    groups: List[dict] = []
    sol_status: Optional[str] = None
    sol_revision: Optional[int] = None
    total_changes = None
    optimal = False
    objective_proven = False
    stale = True

    if solution is not None:
        sol_status = solution["status"]
        sol_revision = solution["revision"]
        stale = sol_revision != current_rev
        optimal = bool(solution["optimal"])
        objective_proven = bool(solution["objective_proven"])
        total_changes = solution["total_changes"]
        groups = solution["payload"].get("groups", [])
        for c in solution["payload"]["cells"]:
            cell = dict(c)
            cell["field"] = cell.pop("fld")
            lk = locks.get((c["row_id"], c["fld"]))
            if lk is not None:  # 锁可能在方案生成后变化，以当前锁为准展示
                cell["locked"] = True
                cell["locked_value"] = lk["value"]
                cell["lock_source"] = lk["source"]
                cell["category"] = "locked"
            cells.append(cell)
        conflicts_out = solution["conflicts"]

    confirmed = (
        confirmation is not None
        and not stale
        and solution is not None
        and confirmation["solution_id"] == solution["id"]
        and sol_status == "optimal"
        and objective_proven
    )

    return {
        "id": sid,
        "revision": current_rev,
        "row_count": len(rows),
        "variety_count": len({v for v, _ in pairs}),
        "house_count": len({h for _, h in pairs}),
        "created_at": s["created_at"],
        "locked_count": len(locks),
        "job_status": data["job"]["status"] if data["job"] else None,
        "job_id": data["job"]["id"] if data["job"] else None,
        "solution_status": sol_status,
        "solution_stale": stale,
        "total_changes": total_changes,
        "optimal": optimal,
        "objective_proven": objective_proven,
        "confirmed": confirmed,
        "rows": [
            {
                "row_id": r["row_id"],
                "line_no": r["line_no"],
                "plant_no": r["plant_no"],
                "tray_no": r["tray_no"],
                "variety_original": r["variety"],
                "house_original": r["house"],
                "height": r["height"],
            }
            for r in rows
        ],
        "allowed_pairs": [{"variety": v, "house": h} for v, h in pairs],
        "locks": data["locks"],
        "cells": cells,
        "conflicts": conflicts_out,
        "groups": groups,
    }


# ---------------------------------------------------------------- 锁定/解锁
@app.post("/api/sessions/{sid}/locks")
def add_lock(sid: int, body: LockIn) -> dict:
    data = db.get_session(sid)
    if data is None:
        raise HTTPException(status_code=404, detail="会话不存在。")
    row = next((r for r in data["rows"] if r["row_id"] == body.row_id), None)
    if row is None:
        raise HTTPException(status_code=400, detail="row_id 不存在。")
    if body.source == "original":
        expected = row[body.field]
        if body.value != expected:
            raise HTTPException(
                status_code=400,
                detail=f"原值锁定的值必须等于原表值「{expected}」。",
            )
    else:
        varieties = {v for v, _ in data["pairs"]}
        houses = {h for _, h in data["pairs"]}
        domain = varieties if body.field == "variety" else houses
        if body.value not in domain:
            raise HTTPException(
                status_code=400,
                detail=f"候选值「{body.value}」不在允许取值域内。",
            )
    db.upsert_lock(sid, body.row_id, body.field, body.value, body.source)
    revision = db.bump_revision(sid)
    return {"revision": revision, "locked": True}


@app.delete("/api/sessions/{sid}/locks/{row_id}/{field}")
def remove_lock(sid: int, row_id: int, field: str) -> dict:
    if field not in ("variety", "house"):
        raise HTTPException(status_code=400, detail="field 必须是 variety 或 house。")
    if db.get_session(sid) is None:
        raise HTTPException(status_code=404, detail="会话不存在。")
    removed = db.delete_lock(sid, row_id, field)
    if not removed:
        raise HTTPException(status_code=404, detail="该核实值不存在。")
    revision = db.bump_revision(sid)
    return {"revision": revision, "locked": False}


# ---------------------------------------------------------------- 触发求解
@app.post("/api/sessions/{sid}/solve")
def trigger_solve(sid: int) -> dict:
    data = db.get_session(sid)
    if data is None:
        raise HTTPException(status_code=404, detail="会话不存在。")
    revision = data["session"]["revision"]
    job_id = db.create_job(sid, revision)
    _pool().submit(_run_job, sid, job_id, revision)
    return {"job_id": job_id, "revision": revision}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: int) -> dict:
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="作业不存在。")
    return job


# ---------------------------------------------------------------- 确认方案
@app.post("/api/sessions/{sid}/confirm")
def confirm_solution(sid: int) -> dict:
    data = db.get_session(sid)
    if data is None:
        raise HTTPException(status_code=404, detail="会话不存在。")
    solution = db.get_solution(sid)
    if solution is None:
        raise HTTPException(status_code=409, detail="尚无方案，无法确认。")
    current_rev = data["session"]["revision"]
    if solution["revision"] != current_rev:
        raise HTTPException(
            status_code=409,
            detail="方案基于旧版本（核实值已变化），请重新求解后再确认。",
        )
    if solution["status"] == "infeasible":
        raise HTTPException(
            status_code=409,
            detail="当前核实值下无解，不存在可确认的修复方案。",
        )
    if solution["status"] != "optimal" or not solution["objective_proven"]:
        raise HTTPException(
            status_code=409,
            detail="未证明最优的方案不可确认，请重新求解。",
        )
    db.add_confirmation(sid, solution["id"], current_rev)
    return {"confirmed": True, "revision": current_rev}


# ---------------------------------------------------------------- 下载修复 CSV
@app.get("/api/sessions/{sid}/download")
def download_csv(sid: int) -> Response:
    data = db.get_session(sid)
    if data is None:
        raise HTTPException(status_code=404, detail="会话不存在。")
    confirmation = db.get_confirmation(sid)
    solution = db.get_solution(sid)
    if confirmation is None or solution is None:
        raise HTTPException(status_code=409, detail="方案尚未确认，无法下载。")
    current_rev = data["session"]["revision"]
    if (
        confirmation["revision"] != current_rev
        or confirmation["solution_id"] != solution["id"]
        or not solution["objective_proven"]
    ):
        raise HTTPException(
            status_code=409, detail="确认已失效，请重新求解并确认后再下载。"
        )

    cells = {
        (c["row_id"], c["fld"]): c["proposed"]
        for c in solution["payload"]["cells"]
    }
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["行号", "植株号", "托盘号", "品种", "温室", "株高"])
    for r in data["rows"]:
        writer.writerow(
            [
                r["line_no"],
                r["plant_no"],
                r["tray_no"],
                cells[(r["row_id"], "variety")],
                cells[(r["row_id"], "house")],
                r["height"],
            ]
        )
    return Response(
        content="﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="repaired_session_{sid}.csv"'
            )
        },
    )


# ---------------------------------------------------------------- 后台作业
def _run_job(sid: int, job_id: int, revision: int) -> None:
    """后台求解。迟到结果（版本已变/作业被取代）直接丢弃，绝不替换新方案。"""
    if db.current_revision(sid) != revision:
        db.mark_job(job_id, "superseded")
        return
    db.mark_job(job_id, "running")
    try:
        data = db.get_session(sid)
        if data is None:
            db.mark_job(job_id, "error", "会话不存在。")
            return
        if data["session"]["revision"] != revision:
            db.mark_job(job_id, "superseded")
            return
        locks = [
            LockSpec(
                row_id=l["row_id"],
                fld=l["field"],
                value=l["value"],
                source=l["source"],
            )
            for l in data["locks"]
        ]
        result = solve(data["rows"], data["pairs"], locks)
        # 迟到结果保护：写入前再次核对版本与作业状态。
        latest = db.get_job(job_id)
        if (
            db.current_revision(sid) != revision
            or latest is None
            or latest["status"] == "superseded"
        ):
            db.mark_job(job_id, "superseded")
            return
        db.save_solution(sid, job_id, revision, result)
        db.mark_job(job_id, "done")
    except Exception as exc:  # 失败保留原表与核实值
        db.mark_job(job_id, "error", str(exc))
