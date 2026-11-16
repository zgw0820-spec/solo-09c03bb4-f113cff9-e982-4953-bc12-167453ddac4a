// 前端冒烟测试：jsdom 加载页面，桩掉 fetch，模拟上传/求解/锁定渲染。
const fs = require("fs");
const { JSDOM } = require("/tmp/node_modules/jsdom");

const html = fs.readFileSync("/workspace/app/static/index.html", "utf-8");
const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true });
const { window } = dom;
const { document } = window;

let state = {
  revision: 1,
  lockedCount: 0,
  job_status: "done",
  solution_status: "optimal",
  solution_stale: false,
  total_changes: 1,
  optimal: true,
  objective_proven: true,
  confirmed: false,
  cells: [],
  conflicts: [],
  groups: [],
};
function sessionBody() {
  return {
    id: 1, revision: state.revision, row_count: 2, variety_count: 2, house_count: 2,
    created_at: "t", locked_count: state.lockedCount, job_status: state.job_status,
    job_id: 1, solution_status: state.solution_status, solution_stale: state.solution_stale,
    total_changes: state.total_changes, optimal: state.optimal,
    objective_proven: state.objective_proven, confirmed: state.confirmed,
    rows: [
      { row_id: 1, line_no: "1", plant_no: "P1", tray_no: "T1", variety_original: "V1", house_original: "H1", height: "10" },
      { row_id: 2, line_no: "2", plant_no: "P1", tray_no: "T2", variety_original: "V2", house_original: "H1", height: "11" },
    ],
    allowed_pairs: [{ variety: "V1", house: "H1" }, { variety: "V2", house: "H2" }],
    locks: [],
    cells: state.cells,
    conflicts: state.conflicts,
    groups: state.groups,
  };
}
window.fetch = async (url, opts = {}) => {
  const method = opts.method || "GET";
  if (url === "/api/sessions" && method === "POST") {
    return { ok: true, status: 201, json: async () => ({ session_id: 1, revision: 1, job_id: 1 }) };
  }
  if (url.startsWith("/api/sessions/1/locks") && method === "POST") {
    const body = JSON.parse(opts.body);
    state.cells = state.cells.map(c =>
      c.row_id === body.row_id && c.field === body.field
        ? { ...c, locked: true, locked_value: body.value, lock_source: body.source, category: "locked" }
        : c);
    state.revision += 1; state.solution_stale = true; state.confirmed = false; state.lockedCount += 1;
    return { ok: true, status: 200, json: async () => ({ revision: state.revision, locked: true }) };
  }
  if (url.match(/locks\/\d+\/\w+$/) && method === "DELETE") {
    state.revision += 1; state.solution_stale = true;
    return { ok: true, status: 200, json: async () => ({ revision: state.revision, locked: false }) };
  }
  if (url.endsWith("/solve") && method === "POST") {
    state.solution_stale = false;
    return { ok: true, status: 200, json: async () => ({ job_id: 2, revision: state.revision }) };
  }
  if (url.endsWith("/confirm") && method === "POST") {
    if (state.solution_stale || !state.objective_proven)
      return { ok: false, status: 409, json: async () => ({ detail: "blocked" }) };
    state.confirmed = true;
    return { ok: true, status: 200, json: async () => ({ confirmed: true }) };
  }
  if (url.match(/\/api\/sessions\/1$/)) {
    return { ok: true, status: 200, json: async () => sessionBody() };
  }
  throw new Error("unmocked " + method + " " + url);
};

const assert = (cond, msg) => { if (!cond) { console.error("FAIL:", msg); process.exit(1); } };

(async () => {
  // 初始：上传卡片可见，会话卡片隐藏
  assert(document.getElementById("sessionCard").classList.contains("hidden"), "session hidden initially");

  state.cells = [
    { row_id: 1, field: "variety", original: "V1", proposed: "V1", category: "fixed_keep", achievable: ["V1"], locked: false, locked_value: null, lock_source: null },
    { row_id: 1, field: "house", original: "H1", proposed: "H1", category: "fixed_keep", achievable: ["H1"], locked: false, locked_value: null, lock_source: null },
    { row_id: 2, field: "variety", original: "V2", proposed: "V1", category: "fixed_change", achievable: ["V1"], locked: false, locked_value: null, lock_source: null },
    { row_id: 2, field: "house", original: "H1", proposed: "H1", category: "ambiguous_keep", achievable: ["H1", "H2"], locked: false, locked_value: null, lock_source: null },
  ];

  // 注入假文件以通过前端文件校验（jsdom 未实现 DataTransfer）
  const fakeFile = new window.File(["csv-content"], "insp.csv", { type: "text/csv" });
  Object.defineProperty(document.getElementById("inspectionFile"), "files", {
    value: [fakeFile], configurable: true,
  });
  Object.defineProperty(document.getElementById("allowedFile"), "files", {
    value: [new window.File(["品种,温室"], "allow.csv", { type: "text/csv" })],
    configurable: true,
  });

  document.getElementById("uploadBtn").click();
  await new Promise(r => setTimeout(r, 50));
  assert(!document.getElementById("sessionCard").classList.contains("hidden"), "session shown after upload");
  assert(document.querySelectorAll("#tableBody tr").length === 2, "two rows rendered");
  assert(document.querySelector(".b-fixed_change").textContent.includes("必须改"), "fixed_change badge");
  assert(document.querySelector(".b-ambiguous_keep").textContent.includes("可保留"), "ambiguous badge");
  // 可确认（最优、未过期）
  assert(document.getElementById("confirmBtn").disabled === false, "confirm enabled when optimal & fresh");
  assert(document.getElementById("downloadBtn").disabled === true, "download disabled before confirm");

  // 模拟锁定行2品种候选值
  const sel = document.querySelector('select[data-act="lock-candidate"][data-row="2"]');
  sel.value = "V1";
  sel.dispatchEvent(new window.Event("change"));
  await new Promise(r => setTimeout(r, 50));
  assert(state.revision === 2, "revision bumped after lock");
  assert(document.getElementById("staleBanner").style.display === "block", "stale banner shown");
  assert(document.getElementById("confirmBtn").disabled === true, "confirm blocked when stale");
  assert(document.querySelector(".b-locked").textContent.includes("已锁定"), "locked badge rendered");

  // 重算后过期解除
  state.solution_stale = false;
  document.getElementById("recalcBtn").click();
  await new Promise(r => setTimeout(r, 50));
  assert(document.getElementById("staleBanner").style.display === "none", "stale banner cleared after recalc");

  // 确认 -> 下载可用
  document.getElementById("confirmBtn").click();
  await new Promise(r => setTimeout(r, 50));
  assert(state.confirmed === true, "confirmation recorded");
  assert(document.getElementById("downloadBtn").disabled === false, "download enabled after confirm");

  // 无解冲突面板
  state.solution_status = "infeasible";
  state.conflicts = [
    { kind: "lock", label: "lock:1:variety", description: "第 1 行品种核实值「V1」", row_id: 1, field: "variety" },
    { kind: "rule", label: "rule:allowed:1", description: "规则：第 1 行组合必须合法。", row_id: 1 },
  ];
  await window.fetch("/api/sessions/1").then(r => r.json());
  // 手动触发渲染（轮询在 jsdom 下未启动，直接调用内部 loadSession）
  document.getElementById("recalcBtn").click();
  await new Promise(r => setTimeout(r, 50));
  assert(document.querySelector(".conflict-box").textContent.includes("无解"), "conflict panel rendered");
  assert(document.getElementById("confirmBtn").disabled === true, "confirm blocked when infeasible");

  console.log("frontend smoke test: ALL PASSED");
})();
