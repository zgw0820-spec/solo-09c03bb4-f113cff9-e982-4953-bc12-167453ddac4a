"""OR-Tools CP-SAT 修复求解。

规则：
1. 植株号/托盘号/株高不可改；品种、温室只可从允许组合的取值域内选择。
2. 同株品种一致（按植株号共享品种变量）。
3. 同托盘温室一致（按托盘号共享温室变量）。
4. 每行（品种, 温室）必须属于允许组合。
5. 锁定（核实）值不可改。
目标：改动单元格数最少。

分析：对每个变量枚举其在所有最优解中的可取候选值，据此逐格分类：
- 所有最优解均取同一新值且与原值不同 -> fixed_change（必须改，值唯一）
- 所有最优解均取同一值且等于原值     -> fixed_keep（可保留原值）
- 可保留原值但还存在其他最优取值      -> ambiguous_keep（可保留，值不唯一）
- 不能保留原值且存在多个最优取值      -> ambiguous_change（必须改，新值不唯一）
- 锁定单元格统一标记 locked
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ortools.sat.python import cp_model

# 单次枚举求解的时间预算（秒）。
ENUM_TIME_LIMIT = 10.0
# 主优化求解的时间预算（秒）。
OPTIMIZE_TIME_LIMIT = 30.0


@dataclass
class LockSpec:
    row_id: int
    fld: str  # "variety" | "house"
    value: str
    source: str  # "original" | "candidate"


@dataclass
class CellResult:
    row_id: int
    fld: str
    original: str
    proposed: Optional[str]
    category: str
    achievable: List[str]
    locked: bool
    locked_value: Optional[str]
    lock_source: Optional[str]


@dataclass
class GroupInfo:
    kind: str  # "plant" | "tray"
    key: str
    row_ids: List[int]
    field: str


@dataclass
class Conflict:
    kind: str  # lock | rule
    label: str
    description: str
    row_id: Optional[int] = None
    fld: Optional[str] = None


@dataclass
class SolveResult:
    status: str  # optimal | infeasible | failed
    optimal: bool
    total_changes: Optional[int]
    objective_proven: bool
    cells: List[CellResult] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)
    groups: List[dict] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class _ModelArtifacts:
    model: cp_model.CpModel
    variety_vars: Dict[str, cp_model.IntVar]  # plant_no -> var
    house_vars: Dict[str, cp_model.IntVar]  # tray_no -> var
    change_bools: Dict[Tuple[int, str], cp_model.IntVar]
    total_changes: cp_model.IntVar
    labels: Dict[int, dict]  # literal proto index -> 冲突解释元数据


def solve(
    rows: Sequence[dict],
    allowed_pairs: Sequence[Tuple[str, str]],
    locks: Sequence[LockSpec],
) -> SolveResult:
    """求解并做逐格最优解枚举分析。"""
    varieties = sorted({v for v, _ in allowed_pairs})
    houses = sorted({h for _, h in allowed_pairs})
    v_id = {v: i for i, v in enumerate(varieties)}
    h_id = {h: i for i, h in enumerate(houses)}
    pair_tuples = [(v_id[v], h_id[h]) for v, h in allowed_pairs]

    # 行内锁校验：锁定值必须在取值域内，否则直接给出冲突。
    lock_by_cell: Dict[Tuple[int, str], LockSpec] = {}
    for lk in locks:
        target = v_id if lk.fld == "variety" else h_id
        if lk.value not in target:
            return SolveResult(
                status="infeasible",
                optimal=False,
                total_changes=None,
                objective_proven=False,
                conflicts=[
                    Conflict(
                        kind="lock",
                        label=f"lock:{lk.row_id}:{lk.fld}",
                        description=(
                            f"第 {lk.row_id} 行核实值「{lk.value}」不在允许"
                            f"{'品种' if lk.fld == 'variety' else '温室'}取值域内，"
                            "锁定值不得改动，无法满足。"
                        ),
                        row_id=lk.row_id,
                        fld=lk.fld,
                    )
                ],
            )
        lock_by_cell[(lk.row_id, lk.fld)] = lk

    try:
        artifacts = _build_model(
            rows, pair_tuples, v_id, h_id, lock_by_cell, with_objective=True
        )
    except _InfeasibleStructure as exc:
        return SolveResult(
            status="infeasible",
            optimal=False,
            total_changes=None,
            objective_proven=False,
            conflicts=exc.conflicts,
        )

    solver = cp_model.CpSolver()
    solver.parameters.random_seed = 20261116
    solver.parameters.num_search_workers = 8
    solver.parameters.max_time_in_seconds = OPTIMIZE_TIME_LIMIT
    status = solver.Solve(artifacts.model)

    if status == cp_model.INFEASIBLE:
        return SolveResult(
            status="infeasible",
            optimal=False,
            total_changes=None,
            objective_proven=False,
            conflicts=_explain_infeasible(
                rows, allowed_pairs, v_id, h_id, lock_by_cell
            ),
        )
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return SolveResult(
            status="failed",
            optimal=False,
            total_changes=None,
            objective_proven=False,
            error=f"求解器返回状态：{solver.StatusName(status)}",
        )

    proven_optimal = status == cp_model.OPTIMAL
    best_changes = int(round(solver.ObjectiveValue()))
    primary_values: Dict[Tuple[str, str], int] = {}
    for plant, var in artifacts.variety_vars.items():
        primary_values[("plant", plant)] = solver.Value(var)
    for tray, var in artifacts.house_vars.items():
        primary_values[("tray", tray)] = solver.Value(var)

    # ---- 逐变量枚举所有最优解下的可取值 ----
    group_defs = _group_definitions(rows)
    ach_index: Dict[Tuple[str, str], List[int]] = {}
    enum_tasks: List[Tuple[str, str, List[int], Optional[int]]] = []
    for kind, key, row_ids, fld in group_defs:
        name_to_id = v_id if fld == "variety" else h_id
        domain_names = varieties if fld == "variety" else houses
        locked_val: Optional[int] = None
        for rid in row_ids:
            lk = lock_by_cell.get((rid, fld))
            if lk is not None:
                locked_val = name_to_id[lk.value]
                break
        if locked_val is not None:
            # 锁定变量候选值唯一（主模型已保证可行性）。
            ach_index[(kind, key)] = [locked_val]
            continue
        enum_tasks.append((kind, key, [name_to_id[d] for d in domain_names], best_changes))

    def _enum(task):
        kind, key, domain, bound = task
        achievable: List[int] = []
        for val in domain:
            if _feasible_with_bound(
                rows, pair_tuples, v_id, h_id, lock_by_cell, kind, key, val, bound
            ):
                achievable.append(val)
        return (kind, key), achievable

    if enum_tasks:
        with ThreadPoolExecutor(max_workers=min(8, len(enum_tasks))) as pool:
            for ident, achievable in pool.map(_enum, enum_tasks):
                ach_index[ident] = achievable

    # ---- 组装逐格结果 ----
    cells: List[CellResult] = []
    id_to_v = {i: v for v, i in v_id.items()}
    id_to_h = {i: h for h, i in h_id.items()}
    for row in rows:
        rid = row["row_id"]
        for fld, kind, key, id_map in (
            ("variety", "plant", row["plant_no"], id_to_v),
            ("house", "tray", row["tray_no"], id_to_h),
        ):
            ident = (kind, key)
            achievable_ids = ach_index[ident]
            achievable = [id_map[i] for i in achievable_ids]
            lk = lock_by_cell.get((rid, fld))
            primary = id_map[primary_values[ident]]
            original = row[fld]
            if lk is not None:
                category = "locked"
                proposed = lk.value
            else:
                can_keep = original in achievable
                if len(achievable) == 1:
                    category = "fixed_keep" if can_keep else "fixed_change"
                else:
                    category = "ambiguous_keep" if can_keep else "ambiguous_change"
                proposed = primary
            cells.append(
                CellResult(
                    row_id=rid,
                    fld=fld,
                    original=original,
                    proposed=proposed,
                    category=category,
                    achievable=achievable,
                    locked=lk is not None,
                    locked_value=lk.value if lk else None,
                    lock_source=lk.source if lk else None,
                )
            )

    groups_out = [
        {
            "kind": kind,
            "key": key,
            "row_ids": row_ids,
            "field": fld,
            "achievable": [
                (varieties if fld == "variety" else houses)[i]
                for i in ach_index[(kind, key)]
            ],
        }
        for kind, key, row_ids, fld in group_defs
    ]
    return SolveResult(
        status="optimal" if proven_optimal else "feasible",
        optimal=proven_optimal,
        total_changes=best_changes,
        objective_proven=proven_optimal,
        cells=cells,
        groups=groups_out,
    )


class _InfeasibleStructure(Exception):
    def __init__(self, conflicts: List[Conflict]):
        super().__init__("structure infeasible")
        self.conflicts = conflicts


def _group_definitions(rows: Sequence[dict]):
    """返回 (kind, key, row_ids, field)：同株品种组、同托盘温室组。"""
    plant_map: Dict[str, List[int]] = {}
    tray_map: Dict[str, List[int]] = {}
    for row in rows:
        plant_map.setdefault(row["plant_no"], []).append(row["row_id"])
        tray_map.setdefault(row["tray_no"], []).append(row["row_id"])
    defs = []
    for plant, rids in plant_map.items():
        defs.append(("plant", plant, sorted(rids), "variety"))
    for tray, rids in tray_map.items():
        defs.append(("tray", tray, sorted(rids), "house"))
    return defs


def _build_model(
    rows: Sequence[dict],
    pair_tuples: Sequence[Tuple[int, int]],
    v_id: Dict[str, int],
    h_id: Dict[str, int],
    lock_by_cell: Dict[Tuple[int, str], LockSpec],
    with_objective: bool,
    extra_fix: Optional[Tuple[str, str, int]] = None,
    bound: Optional[int] = None,
    assumptions: bool = False,
) -> _ModelArtifacts:
    """构建 CP 模型。assumptions=True 时规则/锁以字面量假设形式加入，用于 UNSAT 解释。"""
    model = cp_model.CpModel()
    labels: Dict[int, dict] = {}
    n_v, n_h = len(v_id), len(h_id)

    variety_vars: Dict[str, cp_model.IntVar] = {}
    house_vars: Dict[str, cp_model.IntVar] = {}
    for plant in sorted({r["plant_no"] for r in rows}):
        variety_vars[plant] = model.NewIntVar(0, n_v - 1, f"v_{plant}")
    for tray in sorted({r["tray_no"] for r in rows}):
        house_vars[tray] = model.NewIntVar(0, n_h - 1, f"h_{tray}")

    if extra_fix is not None:
        kind, key, val = extra_fix
        (variety_vars if kind == "plant" else house_vars)[key]  # KeyError 即调用方 bug
        model.Add(
            (variety_vars if kind == "plant" else house_vars)[key] == val
        )

    change_bools: Dict[Tuple[int, str], cp_model.IntVar] = {}
    for row in rows:
        rid = row["row_id"]
        vv = variety_vars[row["plant_no"]]
        hv = house_vars[row["tray_no"]]

        # 每行允许组合（规则字面量，可用于 UNSAT 核心）。
        rule_lit = model.NewBoolVar(f"rule_allowed_row_{rid}")
        labels[rule_lit.Index()] = {
            "kind": "rule",
            "rule": "allowed",
            "row_id": rid,
        }
        ct = model.AddAllowedAssignments([vv, hv], list(pair_tuples))
        ct.OnlyEnforceIf(rule_lit)
        if assumptions:
            model.AddAssumption(rule_lit)
        else:
            # 正常模型中规则必须成立；仅 UNSAT 解释模型用假设代替。
            model.Add(rule_lit == 1)

        # 品种改动指示：原值不在取值域时该变量恒为 1（必须改）。
        vb = model.NewBoolVar(f"chg_v_{rid}")
        change_bools[(rid, "variety")] = vb
        if row["variety"] in v_id:
            model.Add(vv == v_id[row["variety"]]).OnlyEnforceIf(vb.Not())
            model.Add(vv != v_id[row["variety"]]).OnlyEnforceIf(vb)
        else:
            model.Add(vb == 1)

        hb = model.NewBoolVar(f"chg_h_{rid}")
        change_bools[(rid, "house")] = hb
        if row["house"] in h_id:
            model.Add(hv == h_id[row["house"]]).OnlyEnforceIf(hb.Not())
            model.Add(hv != h_id[row["house"]]).OnlyEnforceIf(hb)
        else:
            model.Add(hb == 1)

    # 锁定值（锁字面量，可用于 UNSAT 核心）。
    for (rid, fld), lk in sorted(lock_by_cell.items()):
        row = next(r for r in rows if r["row_id"] == rid)
        var = (
            variety_vars[row["plant_no"]]
            if fld == "variety"
            else house_vars[row["tray_no"]]
        )
        val = (v_id if fld == "variety" else h_id)[lk.value]
        lock_lit = model.NewBoolVar(f"lock_{rid}_{fld}")
        labels[lock_lit.Index()] = {
            "kind": "lock",
            "row_id": rid,
            "fld": fld,
            "value": lk.value,
            "source": lk.source,
        }
        model.Add(var == val).OnlyEnforceIf(lock_lit)
        if assumptions:
            model.AddAssumption(lock_lit)
        else:
            model.Add(lock_lit == 1)

    total = model.NewIntVar(0, len(rows) * 2, "total_changes")
    model.Add(total == sum(change_bools.values()))
    if bound is not None:
        model.Add(total <= bound)
    elif with_objective:
        model.Minimize(total)
    return _ModelArtifacts(
        model=model,
        variety_vars=variety_vars,
        house_vars=house_vars,
        change_bools=change_bools,
        total_changes=total,
        labels=labels,
    )


def _feasible_with_bound(
    rows, pair_tuples, v_id, h_id, lock_by_cell, kind, key, value, bound
) -> bool:
    """在「总改动 <= 最优值」且某变量固定为 value 时是否可行。"""
    try:
        art = _build_model(
            rows,
            pair_tuples,
            v_id,
            h_id,
            lock_by_cell,
            with_objective=False,
            extra_fix=(kind, key, value),
            bound=bound,
            assumptions=False,
        )
    except _InfeasibleStructure:
        return False
    solver = cp_model.CpSolver()
    solver.parameters.random_seed = 20261116
    solver.parameters.num_search_workers = 1
    solver.parameters.max_time_in_seconds = ENUM_TIME_LIMIT
    status = solver.Solve(art.model)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return True
    if status == cp_model.INFEASIBLE:
        return False
    # 超时未知：保守处理为不可取，调用方以 objective_proven 标记。
    return False


def _explain_infeasible(
    rows, allowed_pairs, v_id, h_id, lock_by_cell
) -> List[Conflict]:
    """用假设求解提取一组足以导致无解的核实值/规则冲突。"""
    pair_tuples = [(v_id[v], h_id[h]) for v, h in allowed_pairs]
    art = _build_model(
        rows,
        pair_tuples,
        v_id,
        h_id,
        lock_by_cell,
        with_objective=False,
        assumptions=True,
    )
    solver = cp_model.CpSolver()
    solver.parameters.random_seed = 20261116
    solver.parameters.num_search_workers = 1
    status = solver.Solve(art.model)
    conflicts: List[Conflict] = []
    if status == cp_model.INFEASIBLE:
        core = set(solver.SufficientAssumptionsForInfeasibility())
        for idx in core:
            meta = art.labels.get(idx)
            if meta is None:
                continue
            if meta["kind"] == "lock":
                conflicts.append(
                    Conflict(
                        kind="lock",
                        label=f"lock:{meta['row_id']}:{meta['fld']}",
                        description=(
                            f"第 {meta['row_id']} 行"
                            f"{'品种' if meta['fld'] == 'variety' else '温室'}"
                            f"核实值「{meta['value']}」"
                            f"（来源：{'原值锁定' if meta['source'] == 'original' else '候选值锁定'}）"
                        ),
                        row_id=meta["row_id"],
                        fld=meta["fld"],
                    )
                )
            else:
                rid = meta["row_id"]
                row = next(r for r in rows if r["row_id"] == rid)
                conflicts.append(
                    Conflict(
                        kind="rule",
                        label=f"rule:allowed:{rid}",
                        description=(
                            f"规则：第 {rid} 行（行号 {row['line_no']}）的品种-温室组合"
                            "必须属于允许组合。"
                        ),
                        row_id=rid,
                    )
                )
    if not conflicts:
        # 回退：无法提取核心时给出通用解释。
        conflicts.append(
            Conflict(
                kind="rule",
                label="rule:global",
                description=(
                    "在当前核实值下不存在满足全部规则（允许组合、同株品种一致、"
                    "同托盘温室一致、锁定值不可改）的修复方案。"
                ),
            )
        )

    # 若核心只含锁（隐含与一致性规则冲突），补充涉及的规则说明。
    lock_rows = {c.row_id for c in conflicts if c.kind == "lock"}
    if lock_rows and not any(c.kind == "rule" for c in conflicts):
        for rid in sorted(lock_rows):
            row = next(r for r in rows if r["row_id"] == rid)
            same_plant = [
                r["row_id"] for r in rows if r["plant_no"] == row["plant_no"]
            ]
            same_tray = [
                r["row_id"] for r in rows if r["tray_no"] == row["tray_no"]
            ]
            conflicts.append(
                Conflict(
                    kind="rule",
                    label=f"rule:consistency:{rid}",
                    description=(
                        f"规则：同株（植株号 {row['plant_no']}，行 "
                        f"{same_plant}）品种必须一致；同托盘（托盘号 "
                        f"{row['tray_no']}，行 {same_tray}）温室必须一致。"
                    ),
                    row_id=rid,
                )
            )
    return conflicts
