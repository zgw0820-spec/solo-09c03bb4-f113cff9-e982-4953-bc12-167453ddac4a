"""求解器核心逻辑测试：最优性、逐格分类、锁定、无解解释。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.solver import LockSpec, solve  # noqa: E402


def make_rows(specs):
    """specs: (row_id, plant, tray, variety, house, height)。"""
    return [
        {
            "row_id": rid,
            "line_no": str(rid),
            "plant_no": plant,
            "tray_no": tray,
            "variety": v,
            "house": h,
            "height": height,
        }
        for rid, plant, tray, v, h, height in specs
    ]


PAIRS = [("V1", "H1"), ("V1", "H2"), ("V2", "H1"), ("V2", "H2")]


def cell(result, rid, fld):
    return next(c for c in result.cells if c.row_id == rid and c.fld == fld)


def test_all_valid_zero_changes():
    rows = make_rows(
        [
            (1, "P1", "T1", "V1", "H1", "10"),
            (2, "P2", "T2", "V2", "H2", "12"),
        ]
    )
    res = solve(rows, PAIRS, [])
    assert res.status == "optimal"
    assert res.optimal and res.objective_proven
    assert res.total_changes == 0
    for c in res.cells:
        assert c.category == "fixed_keep"
        assert c.proposed == c.original


def test_illegal_pair_requires_minimal_change():
    # 允许组合仅 (V1,H1)/(V2,H2)。行2 抄成 (V2,H1) 为非法组合；
    # 同株 P1 品种必须一致：行2 品种改 V1（1 格）即可，改温室则连锁更多。
    strict_pairs = [("V1", "H1"), ("V2", "H2")]
    rows = make_rows(
        [
            (1, "P1", "T1", "V1", "H1", "10"),
            (2, "P1", "T2", "V2", "H1", "11"),
        ]
    )
    res = solve(rows, strict_pairs, [])
    assert res.status == "optimal"
    assert res.total_changes == 1
    c = cell(res, 2, "variety")
    assert c.category == "fixed_change"
    assert c.proposed == "V1"
    assert c.achievable == ["V1"]
    # 行1 全部保留
    assert cell(res, 1, "variety").category == "fixed_keep"
    assert cell(res, 1, "house").category == "fixed_keep"


def test_tray_house_consistency():
    # 同托盘 T1 的温室必须一致；两行分别 H1、H2，品种 V1 对 H1/H2 都合法。
    rows = make_rows(
        [
            (1, "P1", "T1", "V1", "H1", "10"),
            (2, "P2", "T1", "V1", "H2", "11"),
        ]
    )
    res = solve(rows, PAIRS, [])
    assert res.status == "optimal"
    assert res.total_changes == 1
    h1 = cell(res, 1, "house")
    h2 = cell(res, 2, "house")
    # 温室改成 H1 或 H2 都只花 1 格 -> 两个同组格均歧义，且可保留各自原值
    assert h1.category == "ambiguous_keep"
    assert h2.category == "ambiguous_keep"
    assert set(h1.achievable) == {"H1", "H2"}
    assert h1.proposed == h2.proposed  # 同组同值


def test_ambiguous_change_classification():
    # 原温室 HX 不在取值域（必须改），且 H1/H2 均为最优 -> ambiguous_change
    rows = make_rows([(1, "P1", "T1", "V1", "HX", "10")])
    res = solve(rows, PAIRS, [])
    assert res.status == "optimal"
    h = cell(res, 1, "house")
    assert h.category == "ambiguous_change"
    assert "HX" not in h.achievable
    assert set(h.achievable) == {"H1", "H2"}
    v = cell(res, 1, "variety")
    assert v.category == "fixed_keep"


def test_lock_forces_different_optimum():
    # 严格允许组合 (V1,H1)/(V2,H2)。无锁时行2品种 V2->V1 花 1 格；
    # 锁定行2品种必须为 V2 后，同株 P1 行1品种被迫改 V2，行1温室被迫改 H2（连锁 2 格）。
    strict_pairs = [("V1", "H1"), ("V2", "H2")]
    rows = make_rows(
        [
            (1, "P1", "T1", "V1", "H1", "10"),
            (2, "P1", "T2", "V2", "H1", "11"),
        ]
    )
    res_free = solve(rows, strict_pairs, [])
    assert res_free.total_changes == 1
    locked = solve(rows, strict_pairs, [LockSpec(2, "variety", "V2", "candidate")])
    assert locked.status == "optimal"
    # 行1品种 V1->V2、行1温室 H1->H2（同株连锁）、行2温室 H1->H2（组合合法）共 3 格
    assert locked.total_changes == 3
    assert cell(locked, 2, "variety").category == "locked"
    assert cell(locked, 2, "variety").locked_value == "V2"
    assert cell(locked, 1, "variety").proposed == "V2"
    assert cell(locked, 1, "house").proposed == "H2"
    assert cell(locked, 2, "house").proposed == "H2"
    # 锁定值不出现在任何改动里
    assert all(
        not (c.locked and c.proposed != c.locked_value) for c in locked.cells
    )


def test_lock_original_value():
    rows = make_rows([(1, "P1", "T1", "V1", "HX", "10")])
    res = solve(rows, PAIRS, [LockSpec(1, "variety", "V1", "original")])
    assert res.status == "optimal"
    assert cell(res, 1, "variety").category == "locked"


def test_infeasible_conflict_explanation():
    # 行1 锁 V1/H2? 构造：单行锁品种 V1 且锁温室 H... 需要与允许组合冲突。
    # 允许组合只含 (V1,H1)，锁 H2 -> 无解，核心应包含锁与 allowed 规则。
    pairs = [("V1", "H1")]
    rows = make_rows([(1, "P1", "T1", "V1", "H1", "10")])
    res = solve(rows, pairs, [LockSpec(1, "house", "H2", "candidate")])
    assert res.status == "infeasible"
    assert res.total_changes is None
    kinds = {c.kind for c in res.conflicts}
    assert "lock" in kinds
    # 核心至少能解释冲突（规则或一致性补充说明）
    assert len(res.conflicts) >= 1
    assert any("H2" in c.description for c in res.conflicts)


def test_infeasible_consistency_locks():
    # 同株两行分别锁不同品种 -> 同株品种一致冲突
    rows = make_rows(
        [
            (1, "P1", "T1", "V1", "H1", "10"),
            (2, "P1", "T2", "V1", "H1", "11"),
        ]
    )
    res = solve(
        rows,
        PAIRS,
        [
            LockSpec(1, "variety", "V1", "candidate"),
            LockSpec(2, "variety", "V2", "candidate"),
        ],
    )
    assert res.status == "infeasible"
    assert any(c.kind == "lock" for c in res.conflicts)


def test_lock_out_of_domain_returns_infeasible():
    rows = make_rows([(1, "P1", "T1", "V1", "H1", "10")])
    res = solve(rows, PAIRS, [LockSpec(1, "house", "HX", "candidate")])
    assert res.status == "infeasible"
    assert res.conflicts[0].fld == "house"


def test_locked_values_never_modified():
    rows = make_rows(
        [
            (1, "P1", "T1", "V1", "HX", "10"),
            (2, "P2", "T2", "V2", "HY", "11"),
        ]
    )
    res = solve(rows, PAIRS, [LockSpec(1, "variety", "V1", "original")])
    assert res.status == "optimal"
    for c in res.cells:
        if c.locked:
            assert c.proposed == c.locked_value
    # 株高/植株号/托盘号不在 cells 中（不可改字段不建模）
    assert all(c.fld in ("variety", "house") for c in res.cells)


def test_achievable_values_only_from_allowed_domain():
    rows = make_rows([(1, "P1", "T1", "V1", "HX", "10")])
    res = solve(rows, PAIRS, [])
    for c in res.cells:
        domain = {"V1", "V2"} if c.fld == "variety" else {"H1", "H2"}
        assert set(c.achievable) <= domain


def test_30_rows_performance_and_optimal():
    specs = []
    for i in range(1, 31):
        # 制造约一半非法组合
        v = "V1" if i % 2 else "V2"
        h = "H1" if i % 3 else "H3"  # H3 不在允许组合
        specs.append((i, f"P{i}", f"T{i}", v, h, str(100 + i)))
    rows = make_rows(specs)
    res = solve(rows, PAIRS, [])
    assert res.status == "optimal"
    assert res.objective_proven
    assert res.total_changes is not None
    assert len(res.cells) == 60
