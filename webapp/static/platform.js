// Platform page (P2 generate-from-DB): seed -> generate (DB-backed run) -> poll ->
// render grids -> export -> history.
const $ = (id) => document.getElementById(id);

const TYPE_CLASS = { Theory: "theory", Practical: "lab", Tutorial: "tutorial", Break: "break" };

let currentGrids = null;
let currentViewMode = "divisions";  // "divisions" | "classrooms" | "labs" | "teachers"
let activeDivision = 0;
let currentRunId = null;
let pollTimer = null;
let originalGrids = null;          // the un-adjusted run grids, so "Restore original" can revert
let movedIds = new Set();          // session ids relocated by the last adjustment (for highlighting)
let dragMoveIds = new Set();       // session ids moved by drag-and-drop (for highlight badge)

// Compare-solvers panel state.
let compareResults = null;
let compareViewMode = "divisions";
let compareActiveSolver = 0;
let compareActiveDivision = 0;

// Safe element listener helper
function on(id, event, handler) {
  const el = $(id);
  if (el) el.addEventListener(event, handler);
}

// Event listeners
on("loadSeedBtn", "click", loadSeed);
on("seedBtn", "click", loadSeed);
on("refreshBranchesBtn", "click", () => loadBranches());
on("branchRefreshBtn", "click", () => loadBranches());

on("deptSelect", "change", () => { rebuildClassSelectors(); checkReadiness(); });
on("yearSelect", "change", () => { rebuildClassSelectors(); checkReadiness(); });
on("semSelect", "change", () => { renderResolvedBranch(); checkReadiness(); });

on("generateTimetableBtn", "click", openGenScopeModal);
on("closeGenScopeModalBtn", "click", closeGenScopeModal);
on("genOptOddSem", "click", () => { closeGenScopeModal(); generate("odd"); });
on("genOptEvenSem", "click", () => { closeGenScopeModal(); generate("even"); });
on("genSpecificBtn", "click", () => {
  const selVal = parseInt($("genSpecificBranchSelect").value);
  closeGenScopeModal();
  generate("specific", selVal);
});
on("genOptAllLoaded", "click", () => { closeGenScopeModal(); generate("all"); });
on("compareBtn", "click", runCompare);
on("adjustBtn", "click", adjust);
on("restoreBtn", "click", restoreOriginal);
on("solver", "change", () => {
  const isCpsat = $("solver") && $("solver").value === "cpsat";
  const wrap = $("optimizationModeWrap");
  if (wrap) wrap.style.display = isCpsat ? "" : "none";
});


on("adjScope", "change", () => {
  const fromWrap = $("adjFromWrap");
  if (fromWrap) fromWrap.style.display = $("adjScope").value === "from" ? "" : "none";
});

// View mode tabs for single result timetable
["divisions", "classrooms", "labs", "teachers"].forEach((mode) => {
  const btn = $(`viewMode${mode.charAt(0).toUpperCase() + mode.slice(1)}`);
  if (btn) {
    btn.addEventListener("click", () => {
      currentViewMode = mode;
      document.querySelectorAll("#viewTypeTabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
      activeDivision = 0;
      renderTabs();
      renderGrid();
    });
  }
  const cmpBtn = $(`cmpViewMode${mode.charAt(0).toUpperCase() + mode.slice(1)}`);
  if (cmpBtn) {
    cmpBtn.addEventListener("click", () => {
      compareViewMode = mode;
      document.querySelectorAll("#compareViewTypeTabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
      compareActiveDivision = 0;
      renderCompareDivisionTabs();
      renderCompareGrid();
    });
  }

});

// ---------------------------------------------------------------- 1. starter data
async function loadSeedDatasets() {
  try {
    const res = await fetch("/api/seed/datasets");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const datasets = await res.json();
    const sel = $("seedDataset");
    if (sel) sel.innerHTML = datasets.map((d) => `<option value="${d.name}">${d.name} — ${d.branch_code}</option>`).join("");
    const statusEl = $("seedStatus");
    if (statusEl) statusEl.textContent = `${datasets.length} dataset(s) available to load.`;
  } catch (e) {
    const statusEl = $("seedStatus");
    if (statusEl) statusEl.textContent = "backend not reachable — start the server on port 8750.";
  }
}

async function loadSeed() {
  const btn = $("loadSeedBtn") || $("seedBtn");
  const sel = $("seedDataset");
  const dataset = sel ? sel.value : "";
  const statusEl = $("seedStatus");
  if (!dataset) {
    if (statusEl) statusEl.textContent = "no dataset selected.";
    return;
  }
  if (btn) btn.disabled = true;
  if (statusEl) statusEl.textContent = `loading "${dataset}"…`;
  try {
    const res = await fetch(`/api/seed/${encodeURIComponent(dataset)}`, { method: "POST" });
    if (res.status === 409) {
      if (statusEl) statusEl.textContent = `"${dataset}" already loaded — continuing.`;
    } else if (!res.ok) {
      const text = await res.text();
      if (statusEl) statusEl.textContent = `error (HTTP ${res.status}) — ${text.slice(0, 200)}`;
    } else {
      const data = await res.json();
      if (statusEl) {
        statusEl.textContent =
          `loaded: ${data.divisions} divisions, ${data.faculty} faculty, ${data.courses} courses, ` +
          `${data.rooms} rooms, ${data.slots} slots (branch ${data.branch_code}).`;
      }
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = "backend not reachable — start the server on port 8750.";
  } finally {
    if (btn) btn.disabled = false;
    await loadBranches();
    checkReadiness();
  }
}

// ---------------------------------------------------------------- 2. class selection
let allBranches = [];
let branchesById = {};

async function loadBranches() {
  try {
    const res = await fetch("/api/branches");
    if (!res.ok) throw new Error("HTTP " + res.status);
    allBranches = await res.json();
    branchesById = Object.fromEntries(allBranches.map((b) => [b.id, b]));
    rebuildClassSelectors();
  } catch (e) {
    // best-effort UI sugar
  }
}

function fillOptions(sel, values, labelFor, keep) {
  const prev = keep && values.includes(keep) ? keep : values[0];
  sel.innerHTML = values.map((v) => `<option value="${esc(v)}">${esc(labelFor(v))}</option>`).join("");
  if (prev !== undefined) sel.value = prev;
  sel.disabled = values.length <= 1;
}

function rebuildClassSelectors() {
  const dept = $("deptSelect"), year = $("yearSelect"), sem = $("semSelect");

  const depts = [...new Set(allBranches.map((b) => b.department || "—"))].sort();
  fillOptions(dept, depts, (d) => {
    const row = allBranches.find((b) => (b.department || "—") === d);
    return row && row.department_name ? `${d} — ${row.department_name}` : d;
  }, dept.value);

  const inDept = allBranches.filter((b) => (b.department || "—") === dept.value);
  const years = [...new Set(inDept.map((b) => b.year_label || "—"))]
    .sort((a, x) => minSem(inDept, a) - minSem(inDept, x));
  fillOptions(year, years, (y) => {
    const row = inDept.find((b) => (b.year_label || "—") === y);
    return row && row.year_name ? `${y} — ${row.year_name}` : y;
  }, year.value);

  const inYear = inDept.filter((b) => (b.year_label || "—") === year.value);
  const sems = [...new Set(inYear.map((b) => b.semester || 0))].sort((a, x) => a - x);
  fillOptions(sem, sems.map(String), (s) => `Semester ${roman(Number(s))}`, sem.value);

  renderResolvedBranch();
}

function minSem(rows, yearLabel) {
  return Math.min(...rows.filter((b) => (b.year_label || "—") === yearLabel).map((b) => b.semester || 99));
}

function roman(n) {
  return ({ 1: "I", 2: "II", 3: "III", 4: "IV", 5: "V", 6: "VI", 7: "VII", 8: "VIII" })[n] || String(n);
}

function selectedBranch() {
  return allBranches.find((b) =>
    (b.department || "—") === $("deptSelect").value &&
    (b.year_label || "—") === $("yearSelect").value &&
    String(b.semester || 0) === $("semSelect").value) || null;
}

function renderResolvedBranch() {
  const b = selectedBranch();
  const line = $("branchResolved");
  if (line) {
    line.textContent = b
      ? `${b.code} — ${(b.semester_label || "").split("(")[0].trim() || b.name}`
      : "no dataset loaded for this combination — load one in step 1.";
  }
  renderBranchNotices();
}

function renderBranchNotices() {
  const box = $("branchNotices");
  if (!box) return;
  const chosen = selectedBranch();
  const withNotice = chosen && chosen.notice ? [chosen] : [];
  box.innerHTML = withNotice
    .map((b) => {
      const [lead, ...rest] = String(b.notice).split("OPEN QUESTION:");
      const open = rest.length ? `<br><b>Open question:</b> ${esc(rest.join("OPEN QUESTION:"))}` : "";
      return `<div class="branch-alert">
        <span class="alert-head">&#9888; ${esc(b.code)}</span>${esc(lead.trim())}${open}
      </div>`;
    })
    .join("");
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function getSelectedBranchIds() {
  const b = selectedBranch();
  return b ? [b.id] : null;
}

function branchQuery() {
  const ids = getSelectedBranchIds();
  return ids ? "?" + ids.map((id) => `branch_ids=${id}`).join("&") : "";
}

async function checkReadiness() {
  try {
    const res = await fetch("/api/readiness" + branchQuery());
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    return data.ready;
  } catch (e) {
    return false;
  }
}

// ---------------------------------------------------------------- 4. generate timetable modal & runner
function openGenScopeModal() {
  const modal = $("genScopeModal");
  if (!modal) return;
  const sel = $("genSpecificBranchSelect");
  if (sel) {
    if (allBranches && allBranches.length > 0) {
      sel.innerHTML = allBranches.map(b => {
        const semTxt = b.semester ? ` (Sem ${b.semester})` : "";
        const nameTxt = b.name ? ` — ${b.name}` : "";
        return `<option value="${b.id}">${b.code}${semTxt}${nameTxt}</option>`;
      }).join("");
      const curr = selectedBranch();
      if (curr && allBranches.some(b => b.id === curr.id)) {
        sel.value = curr.id;
      }
    } else {
      sel.innerHTML = `<option value="">No classes loaded — load in step 1</option>`;
    }
  }
  modal.style.display = "flex";
}

function closeGenScopeModal() {
  const modal = $("genScopeModal");
  if (modal) modal.style.display = "none";
}

async function generate(mode = "selected", specificBranchId = null) {
  const btn = $("generateTimetableBtn");
  const stopBtn = $("genStopBtn");
  const statusEl = $("genStatus");

  if (btn) btn.disabled = true;
  clearTimeout(pollTimer);

  let branch_ids = null;
  let label = "All Loaded Datasets";

  if (mode === "specific" && specificBranchId) {
    const b = branchesById[specificBranchId] || allBranches.find(x => x.id === specificBranchId);
    if (!b) {
      statusEl.textContent = "Selected class not found.";
      if (btn) btn.disabled = false;
      return;
    }
    branch_ids = [b.id];
    label = b.code;
  } else if (mode === "selected") {
    const b = selectedBranch();
    if (!b) {
      statusEl.textContent = "no class selected — pick one in step 2 or select from modal.";
      if (btn) btn.disabled = false;
      return;
    }
    branch_ids = [b.id];
    label = b.code;
  } else if (mode === "odd") {
    const oddBranches = allBranches.filter(b => [1, 3, 5, 7].includes(b.semester));
    if (oddBranches.length === 0) {
      statusEl.textContent = "no odd-semester datasets loaded.";
      if (btn) btn.disabled = false;
      return;
    }
    branch_ids = oddBranches.map(b => b.id);
    label = `All Odd Semesters (Sem ${[...new Set(oddBranches.map(b => b.semester))].sort().join(" + ")})`;
  } else if (mode === "even") {
    const evenBranches = allBranches.filter(b => [2, 4, 6, 8].includes(b.semester));
    if (evenBranches.length === 0) {
      statusEl.textContent = "no even-semester datasets loaded.";
      if (btn) btn.disabled = false;
      return;
    }
    branch_ids = evenBranches.map(b => b.id);
    label = `All Even Semesters (Sem ${[...new Set(evenBranches.map(b => b.semester))].sort().join(" + ")})`;
  } else {
    branch_ids = null;
    label = `All Loaded (${allBranches.length} classes)`;
  }

  const payload = {
    solver: $("solver").value,
    optimization_mode: $("optimizationMode") ? $("optimizationMode").value : "baseline",
    time_limit: parseFloat($("timeLimit").value) || 180,
    label,
    branch_ids,
  };

  if (stopBtn) stopBtn.style.display = "inline-flex";

  statusEl.className = "status-line active-gen";
  statusEl.innerHTML = `<div class="gen-spinner"></div> <span>Generating timetable for <b>${label}</b> (${payload.solver}). Solving simultaneous constraints…</span>`;

  try {
    const res = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (res.status === 409) {
      statusEl.className = "status-line";
      statusEl.textContent = "a solve is already in progress — wait for it to finish.";
      if (btn) btn.disabled = false;
      if (stopBtn) stopBtn.style.display = "none";
      return;
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      const msg = body.detail ? (Array.isArray(body.detail) ? body.detail.join("; ") : String(body.detail)) : ("HTTP " + res.status);
      statusEl.className = "status-line";
      statusEl.textContent = "generation failed: " + msg;
      if (btn) btn.disabled = false;
      if (stopBtn) stopBtn.style.display = "none";
      return;
    }
    const data = await res.json();
    currentRunId = data.run_id;
    pollRun(currentRunId, label);
  } catch (e) {
    statusEl.className = "status-line";
    statusEl.textContent = "error submitting run: " + (e.message || e);
    if (btn) btn.disabled = false;
    if (stopBtn) stopBtn.style.display = "none";
  }
}

function stopGenerate() {
  clearTimeout(pollTimer);
  const btn = $("generateTimetableBtn");
  const stopBtn = $("genStopBtn");
  if (btn) btn.disabled = false;
  if (stopBtn) stopBtn.style.display = "none";
  const statusEl = $("genStatus");
  if (statusEl) {
    statusEl.className = "status-line";
    statusEl.textContent = "Generation polling stopped by user.";
  }
}

function pollRun(runId, label) {
  const btn = $("generateTimetableBtn");
  const stopBtn = $("genStopBtn");
  const statusEl = $("genStatus");

  fetch(`/api/runs/${runId}`)
    .then((res) => res.json())
    .then((run) => {
      if (run.status === "queued" || run.status === "running") {
        statusEl.className = "status-line active-gen";
        statusEl.innerHTML = `<div class="gen-spinner"></div> <span>Run #${runId} for <b>${label}</b> is ${run.status}…</span>`;
        pollTimer = setTimeout(() => pollRun(runId, label), 1500);
        return;
      }
      if (btn) btn.disabled = false;
      if (stopBtn) stopBtn.style.display = "none";
      statusEl.className = "status-line";
      if (run.status === "done") {
        statusEl.textContent = `loaded run #${runId} (${label}).`;
        const badge = $("activeRunBadge");
        if (badge) badge.innerHTML = `<span class="badge badge-success">Showing Run #${runId}</span> <b>${label}</b> (${run.solver})`;
        renderSummary(run);
        renderStages(run.stage_reports);
        currentGrids = run.grids;
        originalGrids = run.grids;
        movedIds = new Set();
        dragMoveIds = new Set();
        activeDivision = 0;
        renderTabs();
        renderGrid();
        $("legend").style.display = "flex";
        if ($("viewModeBar")) $("viewModeBar").style.display = "flex";
        enableExport(runId);
        $("adjustSection").style.display = "";
        $("adjustRunId").textContent = "#" + runId;
        $("restoreBtn").style.display = "none";
        $("adjStatus").textContent = "";
        loadHistory();
      } else {
        statusEl.textContent = `run #${runId} failed: ${run.error || "unknown error"}`;
        $("solveWarning").style.display = "block";
        $("solveWarning").textContent = `Solve failed: ${run.error || "unknown"}`;
      }
    })
    .catch((e) => {
      statusEl.className = "status-line";
      statusEl.textContent = "error polling run: " + (e.message || e);
      if (btn) btn.disabled = false;
      if (stopBtn) stopBtn.style.display = "none";
    });
}

function renderSummary(run) {
  $("summary").style.display = "flex";
  $("statSolver").textContent = run.solver;
  const statStatus = $("statStatus");
  statStatus.textContent = run.status;
  statStatus.className = "stat-val " + (run.status === "done" ? "good" : "bad");

  const statHard = $("statHard");
  statHard.textContent = run.hard ?? "—";
  statHard.className = "stat-val " + (run.hard === 0 ? "good" : (run.hard > 0 ? "bad" : ""));

  $("statSoft").textContent = run.soft !== null && run.soft !== undefined ? Number(run.soft).toFixed(1) : "—";
  $("statWall").textContent = run.wall_clock !== null && run.wall_clock !== undefined ? `${Number(run.wall_clock).toFixed(1)}s` : "—";

  const box = $("solveWarning");
  if (run.hard === -1 || (run.status === "done" && run.hard === null)) {
    box.style.display = "";
    box.className = "readiness-banner not-ready";
    box.innerHTML = "<b>Incomplete solve.</b> Solver timed out before finding a complete schedule.";
  } else if (run.hard > 0) {
    box.style.display = "";
    box.className = "readiness-banner not-ready";
    box.innerHTML = `<b>Partial timetable.</b> ${run.hard} hard constraint violation(s) remain.`;
  } else {
    box.style.display = "none";
  }

  const adaptCard = $("adaptiveMetaCard");
  const extra = run.solution && run.solution.extra_data;
  if (adaptCard && extra && extra.iterations) {
    adaptCard.style.display = "block";
    const iterEl = $("adaptIterVal");
    const convEl = $("adaptConvVal");
    const scoreEl = $("adaptScoreVal");
    const weightsEl = $("adaptWeightsVal");
    if (iterEl) iterEl.textContent = `${extra.iterations} iteration(s)`;
    if (convEl) convEl.textContent = extra.converged ? `Converged (${extra.converged_reason})` : "Completed max iterations";
    if (scoreEl) {
      const bScore = extra.best_score || {};
      scoreEl.textContent = `${bScore.hard_violations ?? 0} hard, ${(bScore.soft_cost ?? run.soft ?? 0).toFixed(1)} soft`;
    }
    if (weightsEl && extra.final_weights) {
      const wStrs = Object.entries(extra.final_weights)
        .map(([k, v]) => `<b>${k}</b>: ${Number(v).toFixed(2)}`);
      weightsEl.innerHTML = `<span><b>Adapted Weights:</b> ${wStrs.join(" &bull; ")}</span>`;
    }
  } else if (adaptCard) {
    adaptCard.style.display = "none";
  }
}

function renderStages(stages) {
  if (!stages || stages.length === 0) { $("stagesWrap").style.display = "none"; return; }
  $("stagesWrap").style.display = "block";
  const track = $("stageTrack");
  track.innerHTML = "";
  stages.forEach((s, i) => {
    const el = document.createElement("div");
    el.className = "stage" + (i === stages.length - 1 ? " best" : "");
    el.innerHTML = `
      <div class="stage-name">${s.name}</div>
      <div class="stage-row"><span>status</span><b>${s.status}</b></div>
      <div class="stage-row"><span>hard</span><b>${s.hard}</b></div>
      <div class="stage-row"><span>soft</span><b>${s.soft}</b></div>
      <div class="stage-row"><span>time</span><b>${s.wall_clock_s}s</b></div>
      <div class="stage-row"><span>running best</span><b>${s.best_hard}h / ${s.best_soft}</b></div>
      ${s.improved ? '<span class="improved-badge">&#9650; improved best</span>' : ''}
    `;
    track.appendChild(el);
    if (i < stages.length - 1) {
      const arrow = document.createElement("div");
      arrow.className = "stage-arrow";
      arrow.textContent = "→";
      track.appendChild(arrow);
    }
  });
}

// ---------------------------------------------------------------- Grid View Builders
function getEntitiesForMode(grids, mode) {
  if (!grids) return [];
  if (mode === "classrooms") return grids.classrooms || [];
  if (mode === "labs") return grids.labs || [];
  if (mode === "teachers") return grids.teachers || [];
  return grids.divisions || [];
}

function renderModeTabs(container, grids, mode, activeIdx, onSelect) {
  const items = getEntitiesForMode(grids, mode);
  if (!items || items.length === 0) {
    container.style.display = "none";
    container.innerHTML = "";
    return;
  }
  container.style.display = "flex";
  container.innerHTML = "";
  items.forEach((item, i) => {
    const t = document.createElement("div");
    t.className = "tab" + (i === activeIdx ? " active" : "");
    if (mode === "classrooms" || mode === "labs") {
      t.textContent = item.name ? `${item.name} (${item.id})` : item.id;
    } else if (mode === "teachers") {
      t.textContent = item.name ? `${item.name} [${item.code || item.id}]` : (item.code || item.id);
    } else {
      t.textContent = item.class_label && item.class_label !== "—" ? item.class_label : "Division " + (item.name || item.id);
    }
    t.onclick = () => onSelect(i);
    container.appendChild(t);
  });
}

function renderTabs() {
  renderModeTabs($("tabs"), currentGrids, currentViewMode, activeDivision, (i) => {
    activeDivision = i;
    renderTabs();
    renderGrid();
  });
}

// ---------------------------------------------------------------- Toast notification
function showToast(message, type = "success") {
  let container = document.getElementById("toastContainer");
  if (!container) {
    container = document.createElement("div");
    container.id = "toastContainer";
    container.style.cssText = [
      "position:fixed", "bottom:24px", "right:24px", "z-index:99999",
      "display:flex", "flex-direction:column", "gap:8px", "pointer-events:none",
    ].join(";");
    document.body.appendChild(container);
  }
  const toast = document.createElement("div");
  toast.style.cssText = [
    "background:" + (type === "success" ? "#1e293b" : "#991b1b"),
    "color:#fff",
    "padding:11px 18px",
    "border-radius:10px",
    "font-size:13.5px",
    "font-weight:600",
    "box-shadow:0 4px 18px rgba(0,0,0,0.25)",
    "pointer-events:all",
    "opacity:0",
    "transform:translateY(8px)",
    "transition:opacity .25s, transform .25s",
    "max-width:380px",
    "border-left:4px solid " + (type === "success" ? "#35d0a5" : "#ef4444"),
  ].join(";");
  toast.textContent = message;
  container.appendChild(toast);
  // Animate in
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      toast.style.opacity = "1";
      toast.style.transform = "translateY(0)";
    });
  });
  // Remove after 3s
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(8px)";
    setTimeout(() => toast.remove(), 300);
  }, 3200);
}

// ---------------------------------------------------------------- Drag-and-drop state
let _dragData = null;  // {session_id, original_room_id, required_room_type}

function buildGridTable(grids, mode, activeIdx, movedSet) {
  const items = getEntitiesForMode(grids, mode);
  if (!items || items.length === 0 || !items[activeIdx]) return null;
  const g = grids;
  const item = items[activeIdx];
  const table = document.createElement("table");
  table.className = "tt";

  const thead = document.createElement("thead");
  let hrow = "<tr><th class='time-col'>Period / Time</th>";
  g.days.forEach((d) => (hrow += `<th>${d}</th>`));
  hrow += "</tr>";
  thead.innerHTML = hrow;
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  g.periods.forEach((p) => {
    const tr = document.createElement("tr");
    tr.className = "period-row";
    tr.dataset.period = p.period;
    tr.innerHTML = `<td class="time-col" title="Period ${p.period + 1} (${p.start} - ${p.end})">
      <div class="period-badge">Period ${p.period + 1}</div>
      <div class="time-range">
        <span class="time-start">${p.start}</span>
        <span class="time-sep">-</span>
        <span class="time-end">${p.end}</span>
      </div>
    </td>`;
    g.days.forEach((_, dayIdx) => {
      const key = `${dayIdx}_${p.period}`;
      const entries = (item.cells && item.cells[key]) || [];
      const td = document.createElement("td");

      // ── Drop target handling ──────────────────────────────────────────────
      td.ondragenter = (event) => {
        event.preventDefault();
        if (!_dragData) return;
        // Check compatibility: room type must match what the dragged session needs
        const targetRoomId = (mode === "divisions") ? null : item.id;
        const compatible = !targetRoomId || !_dragData.required_room_type ||
          _dragData.required_room_type === "none" || !item.room_type ||
          item.room_type === _dragData.required_room_type;
        td.classList.add(compatible ? "drag-over" : "drag-over-invalid");
      };
      td.ondragover = (event) => {
        event.preventDefault();
      };
      td.ondragleave = (e) => {
        // Only remove if we actually left the td (not just moved to a child)
        if (!td.contains(e.relatedTarget)) {
          td.classList.remove("drag-over", "drag-over-invalid");
        }
      };
      td.ondrop = async (event) => {
        event.preventDefault();
        td.classList.remove("drag-over", "drag-over-invalid");
        const dragInfo = _dragData;
        if (!dragInfo || !currentRunId) return;

        const draggedSessionId = dragInfo.session_id;
        const originalRoomId = dragInfo.original_room_id;

        // Determine target_room_id: in room/lab view it's the room item's id;
        // in division/teacher view we keep the session's original room.
        let targetRoomId = originalRoomId;
        if (mode === "classrooms" || mode === "labs") {
          targetRoomId = String(item.id);
        }

        try {
          const response = await fetch(`/api/runs/${currentRunId}/move-session`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              session_id: draggedSessionId,
              target_day: dayIdx,
              target_period: p.period,
              target_room_id: targetRoomId,
            }),
          });

          if (response.ok) {
            const data = await response.json();
            // Re-render with the updated grids returned by the server
            currentGrids = data.grids;
            dragMoveIds.add(draggedSessionId);
            renderTabs();
            renderGrid();
            showToast(
              `✓ Moved session to ${g.days[dayIdx]} period ${p.period + 1}` +
              (data.hard > 0 ? ` (${data.hard} conflict${data.hard > 1 ? "s" : ""})` : " (no conflicts)"),
              data.hard > 0 ? "warn" : "success"
            );
          } else {
            const err = await response.json().catch(() => ({}));
            showToast("✗ Move rejected: " + (err.detail || "Unknown error"), "error");
          }
        } catch (e) {
          showToast("✗ Network error: " + e.message, "error");
        }
      };

      if (entries.length === 0) {
        td.innerHTML = `<div class="cell empty"></div>`;
      } else {
        const cell = document.createElement("div");
        cell.className = "cell";
        entries.forEach((e) => {
          const s = document.createElement("div");
          s.className = "session " + (TYPE_CLASS[e.type] || "theory") +
            ((movedSet && movedSet.has(e.session_id)) || dragMoveIds.has(e.session_id) ? " moved" : "");
          if (e.is_break) {
            s.innerHTML = `<div class="s-course">BREAK</div>`;
          } else {
            // Drag source
            s.draggable = true;
            s.dataset.sessionId = e.session_id;

            s.ondragstart = (event) => {
              _dragData = {
                session_id: e.session_id,          // engine string id
                original_room_id: e.room || "",   // engine room id string
                required_room_type: e.type === "Practical" ? "lab" : "classroom",
              };
              event.dataTransfer.effectAllowed = "move";
              event.dataTransfer.setData("text/plain", e.session_id);
              s.classList.add("dragging");
            };
            s.ondragend = () => {
              s.classList.remove("dragging");
              _dragData = null;
              // Clear any leftover drag-over styles on the whole table
              document.querySelectorAll(".drag-over, .drag-over-invalid").forEach((el) => {
                el.classList.remove("drag-over", "drag-over-invalid");
              });
            };

            const batch = e.batch ? ` · ${e.batch}` : "";
            const courseCode = e.course_code || e.course;
            let metaHtml = "";
            if (mode === "classrooms" || mode === "labs") {
              const divLabel = e.class_label || e.division_name || e.division_id;
              const facLabel = e.faculty ? ` · ${e.faculty}` : "";
              metaHtml = `<div class="s-meta">${divLabel}${batch}${facLabel}</div>`;
            } else if (mode === "teachers") {
              const divLabel = e.class_label || e.division_name || e.division_id;
              const rmLabel = e.room ? ` · @${e.room}` : "";
              metaHtml = `<div class="s-meta">${divLabel}${batch}${rmLabel}</div>`;
            } else {
              metaHtml = `<div class="s-meta">${e.faculty ? e.faculty : ""}${e.room ? " · @" + e.room : ""}${batch}</div>`;
            }
            s.innerHTML =
              `<div class="s-course">${courseCode}${e.type === "Practical" ? " (Lab)" : ""}</div>` +
              metaHtml;
            s.title = `${courseCode} — ${e.type}\nFaculty: ${e.faculty_name || e.faculty || "—"}\nRoom: ${e.room_name || e.room || "—"}\nClass: ${e.class_label || e.division_name || e.division_id}\n\nDrag to move`;
          }
          cell.appendChild(s);
        });
        td.appendChild(cell);
      }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  return table;
}

function renderGrid() {
  const area = $("gridArea");
  area.innerHTML = "";
  const table = buildGridTable(currentGrids, currentViewMode, activeDivision, movedIds);
  if (table) area.appendChild(table);
}

function enableExport(runId) {
  $("exportRow").style.display = "flex";
  $("exportXlsx").href = `/api/runs/${runId}/export.xlsx`;
  $("exportPdf").href = `/api/runs/${runId}/export.pdf`;
}

// ---------------------------------------------------------------- 6. compare solvers
async function runCompare() {
  const btn = $("compareBtn");
  const solvers = [];
  if ($("cmpGreedy").checked) solvers.push("greedy");
  if ($("cmpMip").checked) solvers.push("mip");
  if ($("cmpGa").checked) solvers.push("ga");
  if ($("cmpCpsat").checked) solvers.push("cpsat");
  if ($("cmpPipeline").checked) solvers.push("pipeline");

  if (solvers.length < 2) {
    $("compareStatus").textContent = "pick at least 2 solvers to compare.";
    return;
  }
  btn.disabled = true;
  $("compareStatus").textContent = `comparing ${solvers.join(", ")}…`;
  $("compareResultWrap").style.display = "none";

  const payload = {
    solvers,
    time_limit: parseFloat($("compareTimeLimit").value) || 20,
    branch_ids: getSelectedBranchIds(),
  };

  try {
    const res = await fetch("/api/compare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    compareResults = await res.json();
    $("compareStatus").textContent = "comparison complete.";
    renderCompareResults();
  } catch (e) {
    $("compareStatus").textContent = "error: " + (e.message || e);
  } finally {
    btn.disabled = false;
  }
}

function renderCompareResults() {
  if (!compareResults || compareResults.length === 0) return;
  const tbody = $("compareTableBody");
  tbody.innerHTML = "";

  const valid = compareResults.filter((r) => r.hard !== null && r.soft !== null);
  const minHard = Math.min(...valid.map((r) => r.hard));
  const minSoft = Math.min(...valid.filter((r) => r.hard === minHard).map((r) => r.soft));

  compareResults.forEach((r, i) => {
    const tr = document.createElement("tr");
    const isWinner = r.hard === minHard && r.soft === minSoft;
    if (isWinner) tr.className = "compare-winner";

    tr.innerHTML = `
      <td><b>${r.solver}</b> ${isWinner ? '<span class="compare-badge">WINNER</span>' : ''}</td>
      <td><span class="badge ${r.status === "done" ? 'badge-success' : 'badge-warning'}">${r.status}</span></td>
      <td><b>${r.hard ?? "—"}</b></td>
      <td>${r.soft !== null ? Number(r.soft).toFixed(1) : "—"}</td>
      <td>${Number(r.wall_clock).toFixed(1)}s</td>
    `;
    tbody.appendChild(tr);
  });

  const tabs = $("compareTabs");
  tabs.innerHTML = "";
  compareResults.forEach((r, i) => {
    const t = document.createElement("div");
    t.className = "tab" + (i === compareActiveSolver ? " active" : "");
    t.textContent = `${r.solver} (${r.hard}h / ${r.soft !== null ? Number(r.soft).toFixed(0) : '—'})`;
    t.onclick = () => {
      compareActiveSolver = i;
      compareActiveDivision = 0;
      renderCompareResults();
    };
    tabs.appendChild(t);
  });

  renderCompareDivisionTabs();
  renderCompareGrid();
  $("compareResultWrap").style.display = "block";
}

function renderCompareDivisionTabs() {
  const r = compareResults && compareResults[compareActiveSolver];
  renderModeTabs($("compareDivTabs"), r && r.grids, compareViewMode, compareActiveDivision, (i) => {
    compareActiveDivision = i;
    renderCompareDivisionTabs();
    renderCompareGrid();
  });
}

function renderCompareGrid() {
  const area = $("compareGridArea");
  area.innerHTML = "";
  const r = compareResults && compareResults[compareActiveSolver];
  const table = buildGridTable(r && r.grids, compareViewMode, compareActiveDivision, null);
  if (table) area.appendChild(table);
}

// ---------------------------------------------------------------- 7. holiday / rain adjustment
async function adjust() {
  const btn = $("adjustBtn");
  btn.disabled = true;
  $("adjStatus").textContent = "re-solving week around disruption…";

  const relaxDays = Array.from(document.querySelectorAll("#adjRelaxDays input:checked")).map((cb) => parseInt(cb.value, 10));
  const payload = {
    day: parseInt($("adjDay").value, 10),
    scope: $("adjScope").value,
    from_period: parseInt($("adjFrom").value, 10),
    solver: $("adjSolver").value,
    time_limit: parseFloat($("adjTimeLimit").value) || 60,
    relaxed_days: relaxDays,
  };

  try {
    const res = await fetch(`/api/runs/${currentRunId}/adjust`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    currentGrids = data.grids;
    movedIds = new Set(data.moved_sessions || []);
    renderTabs();
    renderGrid();
    $("restoreBtn").style.display = "";
    $("adjStatus").textContent = `adjusted: ${data.moved_count} session(s) rescheduled, cost=${Number(data.soft).toFixed(1)}.`;
  } catch (e) {
    $("adjStatus").textContent = "error adjusting: " + (e.message || e);
  } finally {
    btn.disabled = false;
  }
}

function restoreOriginal() {
  if (!originalGrids) return;
  currentGrids = originalGrids;
  movedIds = new Set();
  dragMoveIds = new Set();
  renderTabs();
  renderGrid();
  $("adjStatus").textContent = "restored original timetable.";
  $("restoreBtn").style.display = "none";
}

// ---------------------------------------------------------------- 8. history
let cachedSingleRuns = [];

function formatIST(dateVal) {
  if (!dateVal) return "—";
  let s = String(dateVal);
  if (s.includes("T") && !s.endsWith("Z") && !s.includes("+") && !s.includes("-", 10)) {
    s += "Z";
  }
  const d = new Date(s);
  if (isNaN(d.getTime())) return dateVal;
  return d.toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
  }) + " IST";
}

async function loadSavedRun(runId, autoScroll = true) {
  try {
    const res = await fetch(`/api/runs/${runId}`);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const run = await res.json();
    if (run.status === "done" && run.grids) {
      const badge = $("activeRunBadge");
      const istTime = formatIST(run.created_at);
      if (badge) badge.innerHTML = `<span class="badge badge-success">Showing Run #${runId}</span> <b>${run.label || "Saved Timetable"}</b> (${run.solver}) &middot; <span style="color:#64748b; font-size:12px;">🕒 Generated: ${istTime}</span>`;
      renderSummary(run);
      renderStages(run.stage_reports);
      currentGrids = run.grids;
      originalGrids = run.grids;
      movedIds = new Set();
      dragMoveIds = new Set();
      activeDivision = 0;
      renderTabs();
      renderGrid();
      $("legend").style.display = "flex";
      if ($("viewModeBar")) $("viewModeBar").style.display = "flex";
      enableExport(runId);
      $("adjustSection").style.display = "";
      $("adjustRunId").textContent = "#" + runId;
      $("restoreBtn").style.display = "none";
      $("adjStatus").textContent = "";
      if (autoScroll) {
        $("resultSection").scrollIntoView({ behavior: "smooth" });
      }
    }
  } catch (e) {
    // ignore load errors
  }
}
async function loadHistory() {
  try {
    const response = await fetch("/api/runs");
    cachedSingleRuns = response.ok ? await response.json() : [];
    renderHistoryTable();
    if (!currentGrids && cachedSingleRuns.length > 0) {
      const bestRun = cachedSingleRuns.find(r => r.status === "done" && r.hard === 0) || cachedSingleRuns.find(r => r.status === "done");
      if (bestRun) loadSavedRun(bestRun.id, false);
    }
  } catch (_error) {
    // History is best-effort.
  }
}
function renderHistoryTable() {
  const body = $("historyBody");
  if (!body) return;
  body.innerHTML = "";
  if (cachedSingleRuns.length === 0) {
    body.innerHTML = `<tr><td colspan="8" style="text-align:center; color:var(--muted); padding:20px;">No runs in history.</td></tr>`;
    return;
  }
  cachedSingleRuns.forEach(run => {
    const tr = document.createElement("tr");
    tr.style.cursor = "pointer";
    tr.title = `Click to load Run #${run.id} timetable`;
    tr.innerHTML = `
      <td><b>#${run.id}</b></td>
      <td><span class="badge" style="background:var(--panel-2); color:var(--text); font-weight:600;">Timetable</span> <span style="font-weight:600; margin-left:4px;">${esc(run.label || "Timetable")}</span></td>
      <td>${esc(run.solver)}</td>
      <td><span class="badge ${run.status === "done" ? "badge-success" : (run.status === "queued" ? "badge-warning" : "badge-danger")}">${run.status}</span></td>
      <td><b>${run.hard ?? "—"}</b></td>
      <td>${run.soft !== null && run.soft !== undefined ? Number(run.soft).toFixed(1) : "—"}</td>
      <td>${formatIST(run.created_at)}</td>
      <td><button type="button" class="dash-edit-btn" style="color:var(--accent); font-weight:600; cursor:pointer; padding:3px 10px;">Load</button></td>
    `;
    tr.addEventListener("click", () => loadSavedRun(run.id, true));
    body.appendChild(tr);
  });
}
// ---------------------------------------------------------------- 3. Room & Lab Availability Finder
function initAvailabilityDate() {
  const dateInput = $("availDateInput");
  if (dateInput && !dateInput.value) {
    const today = new Date().toISOString().split("T")[0];
    dateInput.value = today;
  }
}

on("availDateInput", "change", checkRoomAvailability);

["availTimeStart", "availTimeEnd"].forEach((id) => {
  on(id, "keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      checkRoomAvailability();
    }
  });
});

on("checkAvailBtn", "click", checkRoomAvailability);

on("toggleOccupiedBtn", "click", () => {
  const grid = $("occupiedRoomsGrid");
  const btn = $("toggleOccupiedBtn");
  if (!grid || !btn) return;
  const isHidden = grid.style.display === "none";
  grid.style.display = isHidden ? "grid" : "none";
  btn.textContent = isHidden ? "Hide Occupied Details" : "Show Occupied Details";
});

async function checkRoomAvailability() {
  const dateVal = $("availDateInput")?.value || "";
  const startTime = $("availTimeStart")?.value?.trim() || "8:00 AM";
  const endTime = $("availTimeEnd")?.value?.trim() || "10:00 AM";

  const alertEl = $("availStatusAlert");
  const resultsEl = $("availResultsContainer");
  const btn = $("checkAvailBtn");

  if (btn) btn.disabled = true;

  try {
    const res = await fetch("/api/rooms/availability", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        date: dateVal || undefined,
        start_time: startTime || undefined,
        end_time: endTime || undefined,
      }),
    });

    const data = await res.json();

    if (!res.ok || !data.valid) {
      if (alertEl) {
        alertEl.style.display = "flex";
        alertEl.className = "room-avail-status-alert invalid";
        alertEl.innerHTML = `<span>⚠️ <b>Invalid timeslot:</b> ${esc(data.message || "Invalid timeslot. Operating hours are 8:00 AM to 7:00 PM.")}</span>`;
      }
      if (resultsEl) resultsEl.style.display = "none";
      return;
    }

    // Valid response
    const dateLabel = data.date ? `${data.date} (${data.day})` : data.day;
    if (alertEl) {
      alertEl.style.display = "flex";
      alertEl.className = "room-avail-status-alert valid";
      alertEl.innerHTML = `<span>✅ <b>Timeslot Verified:</b> ${esc(dateLabel)} ${esc(data.time_range_display)} &mdash; <b>${data.summary.available_classrooms_count} Classrooms</b> &amp; <b>${data.summary.available_labs_count} Labs</b> available.</span>`;
    }

    if (resultsEl) resultsEl.style.display = "block";

    // Summary stats
    if ($("statAvailClassrooms")) $("statAvailClassrooms").textContent = data.summary.available_classrooms_count;
    if ($("statAvailLabs")) $("statAvailLabs").textContent = data.summary.available_labs_count;
    if ($("statOccupiedRooms")) $("statOccupiedRooms").textContent = data.summary.total_occupied_count;
    if ($("statTotalRooms")) $("statTotalRooms").textContent = data.summary.total_rooms;

    if ($("availClassroomsCount")) $("availClassroomsCount").textContent = data.summary.available_classrooms_count;
    if ($("availLabsCount")) $("availLabsCount").textContent = data.summary.available_labs_count;
    if ($("occupiedRoomsCount")) $("occupiedRoomsCount").textContent = data.summary.total_occupied_count;

    // Render Available Classrooms
    const cGrid = $("availClassroomsGrid");
    if (cGrid) {
      if (data.available_classrooms.length === 0) {
        cGrid.innerHTML = `<div style="grid-column: 1/-1; padding: 14px; color: var(--muted); font-size: 13px;">No classrooms are available in this timeslot.</div>`;
      } else {
        cGrid.innerHTML = data.available_classrooms.map((r) => `
          <div class="room-item-card free">
            <div class="room-card-head">
              <span class="room-card-code">${esc(r.code)}</span>
              <span class="room-badge free">Available</span>
            </div>
            <div class="room-card-name">${esc(r.name)}</div>
            <div class="room-card-meta">
              <span>👥 Cap: <b>${r.capacity}</b></span>
              <span>🏢 ${esc(r.building || "Main")} · ${esc(r.floor || "Floor 1")}</span>
            </div>
          </div>
        `).join("");
      }
    }

    // Render Available Labs
    const lGrid = $("availLabsGrid");
    if (lGrid) {
      if (data.available_labs.length === 0) {
        lGrid.innerHTML = `<div style="grid-column: 1/-1; padding: 14px; color: var(--muted); font-size: 13px;">No labs are available in this timeslot.</div>`;
      } else {
        lGrid.innerHTML = data.available_labs.map((r) => `
          <div class="room-item-card free">
            <div class="room-card-head">
              <span class="room-card-code" style="color:#0f766e;">${esc(r.code)}</span>
              <span class="room-badge free" style="background:#ccfbf1; color:#0f766e;">Available Lab</span>
            </div>
            <div class="room-card-name">${esc(r.name)}</div>
            <div class="room-card-meta">
              <span>👥 Cap: <b>${r.capacity}</b></span>
              <span>🔬 ${esc(r.building || "Lab Wing")} · ${esc(r.floor || "Floor 2")}</span>
            </div>
          </div>
        `).join("");
      }
    }

    // Render Occupied Rooms
    const oGrid = $("occupiedRoomsGrid");
    if (oGrid) {
      const occupiedList = [...data.occupied_classrooms, ...data.occupied_labs];
      if (occupiedList.length === 0) {
        oGrid.innerHTML = `<div style="grid-column: 1/-1; padding: 14px; color: var(--good); font-size: 13px; font-weight:600;">✨ All rooms and labs are completely free during this period!</div>`;
      } else {
        oGrid.innerHTML = occupiedList.map((r) => {
          const occItems = r.occupancy.map((occ) => `
            <div class="room-occupancy-item">
              <b>${esc(occ.period_time)}</b>: ${esc(occ.course)} (${esc(occ.type)}) &middot; <i>${esc(occ.faculty)}</i> ${occ.class_label ? `[${esc(occ.class_label)}]` : ""}
            </div>
          `).join("");
          return `
            <div class="room-item-card busy">
              <div class="room-card-head">
                <span class="room-card-code">${esc(r.code)}</span>
                <span class="room-badge busy">Occupied</span>
              </div>
              <div class="room-card-name">${esc(r.name)} (${esc(r.room_type)})</div>
              <div class="room-occupancy-info">
                ${occItems}
              </div>
            </div>
          `;
        }).join("");
      }
    }

  } catch (err) {
    if (alertEl) {
      alertEl.style.display = "flex";
      alertEl.className = "room-avail-status-alert invalid";
      alertEl.innerHTML = `<span>⚠️ <b>Network / Server Error:</b> ${esc(err.message || String(err))}</span>`;
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ---------------------------------------------------------------- Auth nav
async function loadNavUser() {
  try {
    const res = await fetch("/api/auth/me");
    if (!res.ok) return;
    const me = await res.json();
    $("navName").textContent = me.name;
  } catch (e) {
    // cosmetic
  }
}

$("logoutBtn").addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" });
  window.location.href = "/login";
});

// ---------------------------------------------------------------- init
loadNavUser();
initAvailabilityDate();
loadSeedDatasets();
loadBranches();
loadHistory();
checkRoomAvailability();
