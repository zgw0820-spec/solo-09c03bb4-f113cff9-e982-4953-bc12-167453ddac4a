"""巡检 CSV 与允许组合的解析、校验（纯函数，不依赖数据库）。"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import List, Tuple

from .schemas import MAX_HOUSES, MAX_ROWS, MAX_VARIETIES

REQUIRED_HEADERS = ["行号", "植株号", "托盘号", "品种", "温室", "株高"]
HEADER_ALIASES = {
    "行号": "line_no",
    "植株号": "plant_no",
    "托盘号": "tray_no",
    "品种": "variety",
    "温室": "house",
    "株高": "height",
}


class ValidationError(Exception):
    """面向用户的输入校验错误。"""


@dataclass
class InspectRow:
    row_id: int
    line_no: str
    plant_no: str
    tray_no: str
    variety: str
    house: str
    height: str


@dataclass
class ParsedInput:
    rows: List[InspectRow] = field(default_factory=list)
    allowed_pairs: List[Tuple[str, str]] = field(default_factory=list)


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8-sig", errors="strict")


def _read_dicts(raw: bytes) -> List[dict]:
    try:
        text = _decode(raw)
    except UnicodeDecodeError as exc:
        raise ValidationError(f"文件不是有效的 UTF-8 编码：{exc}") from exc
    reader = csv.reader(io.StringIO(text))
    records = [r for r in reader if any(cell.strip() for cell in r)]
    if not records:
        raise ValidationError("CSV 文件为空。")
    header = [h.strip() for h in records[0]]
    unknown = [h for h in header if h and h not in HEADER_ALIASES]
    missing = [h for h in REQUIRED_HEADERS if h not in header]
    if missing:
        raise ValidationError(f"CSV 缺少必需列：{'、'.join(missing)}")
    if unknown:
        raise ValidationError(f"CSV 含未知列：{'、'.join(unknown)}")
    idx = {name: header.index(name) for name in REQUIRED_HEADERS}
    out: List[dict] = []
    for raw_row in records[1:]:
        if len(raw_row) < len(header):
            raw_row = raw_row + [""] * (len(header) - len(raw_row))
        item = {}
        for name, pos in idx.items():
            item[HEADER_ALIASES[name]] = raw_row[pos].strip()
        out.append(item)
    return out


def parse_inspection_csv(raw: bytes) -> List[InspectRow]:
    """解析巡检 CSV，执行全部业务校验。"""
    items = _read_dicts(raw)
    if not items:
        raise ValidationError("CSV 没有数据行。")
    if len(items) > MAX_ROWS:
        raise ValidationError(f"巡检数据最多 {MAX_ROWS} 行，当前 {len(items)} 行。")
    rows: List[InspectRow] = []
    seen_line_no: set[str] = set()
    for i, item in enumerate(items, start=1):
        for key, label in [
            ("line_no", "行号"),
            ("plant_no", "植株号"),
            ("tray_no", "托盘号"),
            ("variety", "品种"),
            ("house", "温室"),
            ("height", "株高"),
        ]:
            if not item[key]:
                raise ValidationError(f"第 {i} 数据行的「{label}」为空。")
        if item["line_no"] in seen_line_no:
            raise ValidationError(f"行号重复：{item['line_no']}。")
        seen_line_no.add(item["line_no"])
        try:
            float(item["height"])
        except ValueError as exc:
            raise ValidationError(
                f"第 {i} 行株高「{item['height']}」不是数值。"
            ) from exc
        rows.append(
            InspectRow(
                row_id=i,
                line_no=item["line_no"],
                plant_no=item["plant_no"],
                tray_no=item["tray_no"],
                variety=item["variety"],
                house=item["house"],
                height=item["height"],
            )
        )
    return rows


def parse_allowed_csv(raw: bytes) -> List[Tuple[str, str]]:
    """解析允许组合 CSV（表头：品种,温室）。"""
    try:
        text = _decode(raw)
    except UnicodeDecodeError as exc:
        raise ValidationError(f"文件不是有效的 UTF-8 编码：{exc}") from exc
    reader = csv.reader(io.StringIO(text))
    records = [r for r in reader if any(cell.strip() for cell in r)]
    if len(records) < 2:
        raise ValidationError("允许组合文件没有数据行。")
    header = [h.strip() for h in records[0]]
    if header[:2] != ["品种", "温室"]:
        raise ValidationError("允许组合 CSV 表头必须是「品种,温室」。")
    pairs: List[Tuple[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for i, rec in enumerate(records[1:], start=1):
        if len(rec) < 2 or not rec[0].strip() or not rec[1].strip():
            raise ValidationError(f"允许组合第 {i} 行的品种或温室为空。")
        pair = (rec[0].strip(), rec[1].strip())
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    validate_allowed_dims(pairs)
    return pairs


def validate_allowed_dims(pairs: List[Tuple[str, str]]) -> None:
    varieties = sorted({p[0] for p in pairs})
    houses = sorted({p[1] for p in pairs})
    if len(varieties) > MAX_VARIETIES:
        raise ValidationError(
            f"允许组合中品种至多 {MAX_VARIETIES} 种，当前 {len(varieties)} 种。"
        )
    if len(houses) > MAX_HOUSES:
        raise ValidationError(
            f"允许组合中温室至多 {MAX_HOUSES} 座，当前 {len(houses)} 座。"
        )
    if not pairs:
        raise ValidationError("允许组合不能为空。")
