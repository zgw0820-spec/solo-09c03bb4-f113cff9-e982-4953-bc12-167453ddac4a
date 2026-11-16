# 育种巡检数据修复平台

修复巡检 CSV 中抄错的「品种」「温室」，避免株高分析分组错误。以**改动单元格数最少**为目标
求最优修复方案，支持把原值或候选值锁定为核实值后重算，并对无解情况给出一组冲突解释。

## 技术栈

- **FastAPI**：HTTP API 与静态页面托管
- **SQLite**（标准库 `sqlite3`，WAL 模式）：会话、原表、允许组合、核实值、作业、方案、确认持久化
- **OR-Tools CP-SAT**：约束建模、最小改动优化、逐格最优解枚举、UNSAT 核心冲突解释
- 原生 HTML/CSS/JS 单页前端，无构建步骤
- Docker / Docker Compose 一键启动

## 快速启动（Docker Compose）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

数据通过命名卷 `breeder_data` 持久化（容器内 `/app/data/app.db`）。

## 本地开发启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 可选：自定义数据库路径
BREEDER_DB_PATH=/path/to/app.db uvicorn app.main:app --port 8000
```

## 输入文件格式

巡检 CSV（≤ 30 行，UTF-8，首行表头）：

| 行号 | 植株号 | 托盘号 | 品种 | 温室 | 株高 |
|---|---|---|---|---|---|
| 1 | P001 | T01 | 番茄A | G1 | 42.5 |

允许组合 CSV（品种 ≤ 6 种、温室 ≤ 6 座）：表头 `品种,温室`，每行一个合法组合。
也可在页面上手工添加允许组合。

## 业务规则

1. 植株号、托盘号、株高**永不修改**；只有品种、温室两列参与修复。
2. 品种/温室的新值**只能取自允许组合**的取值域。
3. 同「植株号」的所有行品种必须一致；同「托盘号」的所有行温室必须一致。
4. 每行（品种, 温室）必须是允许组合之一。
5. 核实值（锁定）不可被求解器改动。
6. 目标：改动单元格数最少；**仅当最优性被证明（CP-SAT OPTIMAL）时方案可确认**。

## 逐格分类（基于全部最优解的候选值枚举）

| 分类 | 含义 |
|---|---|
| `fixed_change` 必须改·值唯一 | 所有最优解都取同一个新值，且与原值不同 |
| `ambiguous_change` 必须改·新值不唯一 | 原值不可保留，但存在多个最优新值 |
| `ambiguous_keep` 可保留·值不唯一 | 存在保留原值的最优解，也存在其他最优取值 |
| `fixed_keep` 保留原值 | 所有最优解中原值均不变 |
| `locked` 已锁定核实值 | 用户锁定，求解器不得改动 |

## 核实值（锁定）与版本控制

- 每格可「锁定原值」或从该格全部最优候选值中「锁定候选值」；可随时解锁。
- 锁定变化会使修订号 +1 并**立即作废旧确认、禁止确认旧方案**（页面显示过期横幅）。
- 求解在后台线程执行，期间仍可修改核实值；作业写入前双重校验修订号，
  **迟到结果只标记为 superseded，绝不替换新方案**。
- 无解时不写修复方案，列出一组足以导致冲突的核实值与规则（CP-SAT 假设求解的 UNSAT 核心），
  原表与核实值全部保留。

## 确认与下载

- 仅「已证最优 + 当前修订号 + 完整逐格方案」可确认；无解、未证最优、过期方案均返回 409。
- 仅确认后可下载修复 CSV（与原表同列、UTF-8 BOM，Excel 可直接打开）。
- 求解失败不会删除原表与核实值，可重试。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/sessions` | 上传巡检 CSV + 允许组合（CSV 或 JSON），自动触发首次求解 |
| GET | `/api/sessions/{sid}` | 会话详情：原表、锁、逐格方案、冲突、版本/确认状态 |
| POST | `/api/sessions/{sid}/locks` | 锁定原值或候选值（body: row_id/field/value/source） |
| DELETE | `/api/sessions/{sid}/locks/{row_id}/{field}` | 解除核实值 |
| POST | `/api/sessions/{sid}/solve` | 按当前核实值重新求解 |
| GET | `/api/jobs/{job_id}` | 作业状态（pending/running/done/error/superseded） |
| POST | `/api/sessions/{sid}/confirm` | 确认当前最优完整方案 |
| GET | `/api/sessions/{sid}/download` | 下载已确认的修复 CSV |
| GET | `/api/health` | 健康检查 |

交互式文档：`http://localhost:8000/docs`。

## 测试

```bash
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest tests/ -q
node tests/smoke_frontend.js   # 前端 jsdom 冒烟测试（需 npm install jsdom）
```

- `tests/test_solver.py`：最优性、五类逐格分类、锁定语义、UNSAT 冲突解释、30 行性能。
- `tests/test_api.py`：上传校验、端到端求解、版本失效、迟到作业、确认/下载门控、冲突无解。
