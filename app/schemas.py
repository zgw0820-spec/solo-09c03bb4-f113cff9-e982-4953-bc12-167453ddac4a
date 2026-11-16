"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

MAX_ROWS = 30
MAX_VARIETIES = 6
MAX_HOUSES = 6


class AllowedPair(BaseModel):
    variety: str = Field(..., min_length=1, max_length=64)
    house: str = Field(..., min_length=1, max_length=64)


class AllowedPairsPayload(BaseModel):
    pairs: List[AllowedPair] = Field(..., max_length=MAX_VARIETIES * MAX_HOUSES)


class LockIn(BaseModel):
    row_id: int = Field(..., ge=1)
    field: str = Field(..., pattern="^(variety|house)$")
    value: str = Field(..., min_length=1, max_length=64)
    source: str = Field("candidate", pattern="^(original|candidate)$")


class RowOut(BaseModel):
    row_id: int
    line_no: int
    plant_no: str
    tray_no: str
    variety_original: str
    house_original: str
    height: str


class CellOut(BaseModel):
    row_id: int
    field: str
    original: str
    proposed: Optional[str]
    category: str  # locked | fixed_keep | fixed_change | ambiguous_keep | ambiguous_change
    achievable: List[str]
    locked: bool
    locked_value: Optional[str]
    lock_source: Optional[str]


class ConflictItem(BaseModel):
    kind: str  # lock | rule
    label: str
    description: str
    row_id: Optional[int] = None
    field: Optional[str] = None


class SolutionOut(BaseModel):
    status: str  # optimal | infeasible | failed | pending
    optimal: bool
    total_changes: Optional[int]
    objective_proven: bool
    revision: int
    job_id: Optional[int]
    error: Optional[str] = None
    cells: List[CellOut]
    conflicts: List[ConflictItem]
    groups: List[dict]
    confirmed: bool


class SessionOut(BaseModel):
    id: int
    revision: int
    row_count: int
    variety_count: int
    house_count: int
    created_at: str
    locked_count: int
    job_status: Optional[str]
    solution_status: Optional[str]
    confirmed: bool
    rows: List[RowOut]
    allowed_pairs: List[AllowedPair]
