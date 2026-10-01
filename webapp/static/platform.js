// Platform page (P2 generate-from-DB): seed -> generate (DB-backed run) -> poll ->
// render grids -> export -> history -> Live Multi-Objective Pareto Stream.
const $ = (id) => document.getElementById(id);

const TYPE_CLASS = { Theory: "theory", Practical: "lab", Tutorial: "tutorial", Break: "break" };

let currentGrids = null;
let currentViewMode = "divisions";  // "divisions" | "classrooms" | "labs" | "teachers"
let activeDivision = 0;
let currentRunId = null;
let pollTimer = null;
let originalGrids = null;          // the un-adjusted run grids, so "Restore original" can revert
let movedIds = new Set();          // session ids relocated by the last adjustment (for highlighting)

// Compare-solvers panel state.
let compareResults = null;
let compareViewMode = "divisions";
let compareActiveSolver = 0;
let compareActiveDivision = 0;

// Pareto streaming & multi-objective optimization state
let paretoPoints = [];
let paretoPayoffTables = {};
let paretoRecommendations = {};
let selectedParetoPointId = null;
let paretoGrids = null;
let paretoViewMode = "divisions";
let paretoActiveDivision = 0;
let activeProfile = "balanced";
let paretoStreamAbortController = null;
let paretoStopToken = null;  // server-issued token for explicit stop via /api/pareto/stop

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

on("generateBtn", "click", () => generate("selected"));
on("generateOddYearsBtn", "click", () => generate("odd"));
on("generateEvenYearsBtn", "click", () => generate("even"));
on("generateAllYearsBtn", "click", () => generate("all"));

// Pareto section button listeners
on("paretoStreamBtn", "click", openParetoScopeModal);
on("paretoOddSemBtn", "click", () => startParetoStreaming("odd"));
on("paretoEvenSemBtn", "click", () => startParetoStreaming("even"));
on("paretoSelectedBtn", "click", () => startParetoStreaming("selected"));
on("paretoStopBtn", "click", stopParetoStreaming);
on("streamBannerStopBtn", "click", stopParetoStreaming);
on("hudStopBtn", "click", stopParetoStreaming);
on("genStopBtn", "click", stopGenerate);

// Modal scope options
on("optOddSem", "click", () => startParetoStreaming("odd"));
on("optEvenSem", "click", () => startParetoStreaming("even"));
on("optSelectedClass", "click", () => startParetoStreaming("selected"));
on("optAllLoaded", "click", () => startParetoStreaming("all"));
on("closeParetoScopeModalBtn", "click", closeParetoScopeModal);

on("saveParetoRunBtn", "click", saveSelectedParetoAsRun);

// Profile buttons
on("btnFacultyFriendly", "click", () => setPriorityProfile("faculty_friendly"));
on("btnBalanced", "click", () => setPriorityProfile("balanced"));
on("btnStudentFriendly", "click", () => setPriorityProfile("student_friendly"));

on("compareBtn", "click", runCompare);
on("adjustBtn", "click", adjust);
on("restoreBtn", "click", restoreOriginal);

on("histTabAll", "click", () => setHistoryFilter("all"));
on("histTabPareto", "click", () => setHistoryFilter("pareto"));
on("histTabSingle", "click", () => setHistoryFilter("single"));

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
  const parBtn = $(`parViewMode${mode.charAt(0).toUpperCase() + mode.slice(1)}`);
  if (parBtn) {
    parBtn.addEventListener("click", () => {
      paretoViewMode = mode;
      document.querySelectorAll("#paretoViewTypeTabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
      paretoActiveDivision = 0;
      renderParetoTabs();
      renderParetoGrid();
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

// ---------------------------------------------------------------- 4. generate timetable
async function generate(mode = "selected") {
  const btn = $("generateBtn");
  const oddB = $("generateOddYearsBtn");
  const evenB = $("generateEvenYearsBtn");
  const allB = $("generateAllYearsBtn");
  const statusEl = $("genStatus");

  [btn, oddB, evenB, allB].forEach(b => { if (b) b.disabled = true; });
  clearTimeout(pollTimer);

  let branch_ids = null;
  let label = "All Loaded Datasets";

  if (mode === "selected") {
    const b = selectedBranch();
    if (!b) {
      statusEl.textContent = "no class selected — pick one in step 2.";
      [btn, oddB, evenB, allB].forEach(btnEl => { if (btnEl) btnEl.disabled = false; });
      return;
    }
    branch_ids = [b.id];
    label = b.code;
  } else if (mode === "odd") {
    const oddBranches = allBranches.filter(b => [1, 3, 5, 7].includes(b.semester));
    if (oddBranches.length === 0) {
      statusEl.textContent = "no odd-semester datasets loaded.";
      [btn, oddB, evenB, allB].forEach(btnEl => { if (btnEl) btnEl.disabled = false; });
      return;
    }
    branch_ids = oddBranches.map(b => b.id);
    label = `All Odd Semesters (Sem ${[...new Set(oddBranches.map(b => b.semester))].sort().join(" + ")})`;
  } else if (mode === "even") {
    const evenBranches = allBranches.filter(b => [2, 4, 6, 8].includes(b.semester));
    if (evenBranches.length === 0) {
      statusEl.textContent = "no even-semester datasets loaded.";
      [btn, oddB, evenB, allB].forEach(btnEl => { if (btnEl) btnEl.disabled = false; });
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
    time_limit: parseFloat($("timeLimit").value) || 180,
    label,
    branch_ids,
  };

  const stopBtn = $("genStopBtn");
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
      [btn, oddB, evenB, allB].forEach(btnEl => { if (btnEl) btnEl.disabled = false; });
      if (stopBtn) stopBtn.style.display = "none";
      return;
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      const msg = body.detail ? (Array.isArray(body.detail) ? body.detail.join("; ") : String(body.detail)) : ("HTTP " + res.status);
      statusEl.className = "status-line";
      statusEl.textContent = "generation failed: " + msg;
      [btn, oddB, evenB, allB].forEach(btnEl => { if (btnEl) btnEl.disabled = false; });
      if (stopBtn) stopBtn.style.display = "none";
      return;
    }
    const data = await res.json();
    currentRunId = data.run_id;
    pollRun(currentRunId, label);
  } catch (e) {
    statusEl.className = "status-line";
    statusEl.textContent = "error submitting run: " + (e.message || e);
    [btn, oddB, evenB, allB].forEach(btnEl => { if (btnEl) btnEl.disabled = false; });
    if (stopBtn) stopBtn.style.display = "none";
  }
}

function stopGenerate() {
  clearTimeout(pollTimer);
  const btn = $("generateBtn");
  const oddB = $("generateOddYearsBtn");
  const evenB = $("generateEvenYearsBtn");
  const allB = $("generateAllYearsBtn");
  const stopBtn = $("genStopBtn");
  [btn, oddB, evenB, allB].forEach(b => { if (b) b.disabled = false; });
  if (stopBtn) stopBtn.style.display = "none";
  const statusEl = $("genStatus");
  if (statusEl) {
    statusEl.className = "status-line";
    statusEl.textContent = "Generation polling stopped by user.";
  }
}

function pollRun(runId, label) {
  const btn = $("generateBtn");
  const oddB = $("generateOddYearsBtn");
  const evenB = $("generateEvenYearsBtn");
  const allB = $("generateAllYearsBtn");
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
      [btn, oddB, evenB, allB].forEach(b => { if (b) b.disabled = false; });
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
      [btn, oddB, evenB, allB].forEach(b => { if (b) b.disabled = false; });
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

function buildGridTable(grids, mode, activeIdx, movedSet) {
  const items = getEntitiesForMode(grids, mode);
  if (!items || items.length === 0 || !items[activeIdx]) return null;
  const g = grids;
  const item = items[activeIdx];
  const table = document.createElement("table");
  table.className = "tt";

  const thead = document.createElement("thead");
  let hrow = "<tr><th class='time-col'>Time</th>";
  g.days.forEach((d) => (hrow += `<th>${d}</th>`));
  hrow += "</tr>";
  thead.innerHTML = hrow;
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  g.periods.forEach((p) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="time-col">${p.start}<br>${p.end}</td>`;
    g.days.forEach((_, dayIdx) => {
      const key = `${dayIdx}_${p.period}`;
      const entries = (item.cells && item.cells[key]) || [];
      const td = document.createElement("td");
      if (entries.length === 0) {
        td.innerHTML = `<div class="cell empty"></div>`;
      } else {
        const cell = document.createElement("div");
        cell.className = "cell";
        entries.forEach((e) => {
          const s = document.createElement("div");
          s.className = "session " + (TYPE_CLASS[e.type] || "theory") +
            (movedSet && movedSet.has(e.session_id) ? " moved" : "");
          if (e.is_break) {
            s.innerHTML = `<div class="s-course">BREAK</div>`;
          } else {
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
            s.title = `${courseCode} — ${e.type}\nFaculty: ${e.faculty_name || e.faculty || "—"}\nRoom: ${e.room_name || e.room || "—"}\nClass: ${e.class_label || e.division_name || e.division_id}`;
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

// ---------------------------------------------------------------- 5. Multi-Objective Pareto & Epsilon Streaming
function selectedParetoPairs() {
  const pairs = [];
  if ($("parFacStu") && $("parFacStu").checked) pairs.push(["faculty", "students"]);
  if ($("parStuFac") && $("parStuFac").checked) pairs.push(["students", "faculty"]);
  if ($("parLabStu") && $("parLabStu").checked) pairs.push(["labs", "students"]);
  return pairs;
}

function updateStepper(activeStepNum) {
  for (let i = 1; i <= 8; i++) {
    const node = $(`stepNode${i}`);
    if (!node) continue;
    const icon = node.querySelector(".step-icon");
    if (i < activeStepNum) {
      node.className = "step-node done";
      if (icon) icon.textContent = "✓";
    } else if (i === activeStepNum) {
      node.className = "step-node active";
      if (icon) icon.textContent = "▶";
    } else {
      node.className = "step-node";
      if (icon) icon.textContent = "○";
    }
  }
}

function openParetoScopeModal() {
  const modal = $("paretoScopeModal");
  if (modal) modal.style.display = "flex";
}

function closeParetoScopeModal() {
  const modal = $("paretoScopeModal");
  if (modal) modal.style.display = "none";
}

let paretoTimerInterval = null;

async function startParetoStreaming(mode = "odd") {
  closeParetoScopeModal();

  const pairs = selectedParetoPairs();
  if (pairs.length === 0) {
    $("paretoStatus").textContent = "pick at least one objective pair.";
    return;
  }

  const sweepPoints = parseInt($("parSweepPoints").value, 10) || 7;
  const timeLimitPerSolve = parseFloat($("parTimeLimit").value) || 240;

  // Resolve target branches
  let branch_ids = null;
  let label = "All Loaded Datasets";

  if (mode === "selected") {
    const b = selectedBranch();
    if (!b) {
      $("paretoStatus").textContent = "no class selected — pick one in step 2.";
      return;
    }
    branch_ids = [b.id];
    label = `${b.code} (${b.semester_label || 'Sem ' + b.semester})`;
  } else if (mode === "odd") {
    const oddBranches = allBranches.filter(b => [1, 3, 5, 7].includes(b.semester));
    if (oddBranches.length === 0) {
      $("paretoStatus").textContent = "no odd-semester datasets loaded. Please load odd semester datasets in step 1.";
      return;
    }
    branch_ids = oddBranches.map(b => b.id);
    const sems = [...new Set(oddBranches.map(b => b.semester))].sort();
    label = `All Odd Semesters (Sem ${sems.join(" + ")})`;
  } else if (mode === "even") {
    const evenBranches = allBranches.filter(b => [2, 4, 6, 8].includes(b.semester));
    if (evenBranches.length === 0) {
      $("paretoStatus").textContent = "no even-semester datasets loaded. Please load even semester datasets in step 1.";
      return;
    }
    branch_ids = evenBranches.map(b => b.id);
    const sems = [...new Set(evenBranches.map(b => b.semester))].sort();
    label = `All Even Semesters (Sem ${sems.join(" + ")})`;
  } else {
    branch_ids = null;
    label = `All Loaded (${allBranches.length} classes)`;
  }

  // Calculate estimated solve time
  const estSeconds = Math.max(30, Math.round(pairs.length * sweepPoints * Math.min(timeLimitPerSolve, 10)));
  const startTime = Date.now();

  const allBtns = [
    $("paretoStreamBtn"), $("paretoOddSemBtn"), $("paretoEvenSemBtn"), $("paretoSelectedBtn")
  ];
  allBtns.forEach(b => { if (b) b.disabled = true; });

  const stopBtns = [$("paretoStopBtn"), $("streamBannerStopBtn"), $("hudStopBtn")];
  stopBtns.forEach(b => { if (b) b.style.display = "inline-flex"; });

  $("paretoLiveBadge").style.display = "inline-flex";
  $("paretoHud").style.display = "block";
  $("paretoLiveTimetableWrap").style.display = "block";
  $("paretoResultWrap").style.display = "block";
  $("paretoRecBox").style.display = "none";
  $("paretoStreamBanner").style.display = "flex";

  $("streamBannerTitle").innerHTML = `⚡ Live Multi-Objective Pareto Optimization &middot; <b>${label}</b>`;
  $("streamBannerSub").textContent = `Solving simultaneous CP-SAT multi-objective model (Time limit: ${timeLimitPerSolve}s/point for optimality). Streaming candidate assignments live across ${pairs.length} tradeoff pair(s)…`;
  $("streamEtaText").textContent = `Solving with ${timeLimitPerSolve}s limit per point`;
  $("paretoStatus").textContent = `Initializing live multi-objective stream for ${label}…`;

  if (paretoTimerInterval) clearInterval(paretoTimerInterval);
  paretoTimerInterval = setInterval(() => {
    const elapsedSec = ((Date.now() - startTime) / 1000).toFixed(1);
    $("hudElapsedVal").textContent = `${elapsedSec}s`;
    $("streamEtaText").textContent = `Elapsed: ${elapsedSec}s (Target: Optimal Convergence @ ${timeLimitPerSolve}s/point)`;
  }, 200);

  paretoPoints = [];
  paretoPayoffTables = {};
  paretoRecommendations = {};
  selectedParetoPointId = null;
  paretoStopToken = null;
  updateStepper(1);

  const payload = {
    pairs,
    time_limit_s: timeLimitPerSolve,
    sweep_points: sweepPoints,
    branch_ids,
  };

  try {
    if (paretoStreamAbortController) paretoStreamAbortController.abort();
    paretoStreamAbortController = new AbortController();

    const response = await fetch("/api/pareto/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: paretoStreamAbortController.signal,
    });

    if (response.status === 400) {
      const err = await response.json();
      $("paretoStatus").textContent = "not ready: " + (err.detail ? (Array.isArray(err.detail) ? err.detail.join("; ") : err.detail) : "Validation failed");
      allBtns.forEach(b => { if (b) b.disabled = false; });
      stopBtns.forEach(b => { if (b) b.style.display = "none"; });
      $("paretoLiveBadge").style.display = "none";
      $("paretoStreamBanner").style.display = "none";
      if (paretoTimerInterval) clearInterval(paretoTimerInterval);
      return;
    }
    if (!response.ok) throw new Error("HTTP " + response.status);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() || "";

      for (const chunk of chunks) {
        const line = chunk.trim();
        if (!line.startsWith("data:")) continue;
        const rawJson = line.replace(/^data:\s*/, "");
        if (!rawJson) continue;

        try {
          const event = JSON.parse(rawJson);
          handleParetoStreamEvent(event);
        } catch (err) {
          console.warn("SSE JSON parse error:", err);
        }
      }
    }

    if (paretoTimerInterval) clearInterval(paretoTimerInterval);
    const finalElapsed = ((Date.now() - startTime) / 1000).toFixed(1);
    $("paretoStatus").textContent = `Multi-Objective Pareto sweep complete in ${finalElapsed}s! Non-dominated frontier identified.`;
    $("streamEtaText").textContent = `Finished in ${finalElapsed}s`;
    allBtns.forEach(b => { if (b) b.disabled = false; });
    stopBtns.forEach(b => { if (b) b.style.display = "none"; });
    $("paretoLiveBadge").style.display = "none";
    updateStepper(8);
  } catch (err) {
    if (paretoTimerInterval) clearInterval(paretoTimerInterval);
    if (err.name !== "AbortError") {
      $("paretoStatus").textContent = "Stream error: " + (err.message || err);
      allBtns.forEach(b => { if (b) b.disabled = false; });
      stopBtns.forEach(b => { if (b) b.style.display = "none"; });
      $("paretoLiveBadge").style.display = "none";
      $("paretoStreamBanner").style.display = "none";
    }
  }
}

function stopParetoStreaming() {
  // 1. Abort the fetch (closes SSE connection from browser side)
  if (paretoStreamAbortController) {
    paretoStreamAbortController.abort();
    paretoStreamAbortController = null;
  }
  // 2. Call the server-side stop endpoint using the session token (reliable kill)
  if (paretoStopToken) {
    fetch("/api/pareto/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: paretoStopToken }),
    }).catch(() => {});
    paretoStopToken = null;
  }
  if (paretoTimerInterval) {
    clearInterval(paretoTimerInterval);
    paretoTimerInterval = null;
  }
  const allBtns = [
    $("paretoStreamBtn"), $("paretoOddSemBtn"), $("paretoEvenSemBtn"), $("paretoSelectedBtn")
  ];
  allBtns.forEach(b => { if (b) b.disabled = false; });
  const stopBtns = [$("paretoStopBtn"), $("streamBannerStopBtn"), $("hudStopBtn")];
  stopBtns.forEach(b => { if (b) b.style.display = "none"; });

  $("paretoLiveBadge").style.display = "none";
  $("streamBannerTitle").innerHTML = `🛑 Multi-Objective Optimization Stopped`;
  $("streamBannerSub").textContent = `Sweep halted by user. Preserving all candidate timetables and Pareto points explored so far.`;
  $("streamEtaText").textContent = `Stopped`;
  $("paretoStatus").textContent = `Multi-objective optimization stopped. ${paretoPoints.length} point(s) available to inspect.`;
  $("hudStatusBadge").textContent = "Stopped by User";

  if (paretoPoints.length > 0) {
    renderParetoUnifiedResults();
    renderRecommendationBox();
  }
}

function handleParetoStreamEvent(ev) {
  if (ev.type === "token") {
    // Store server-issued stop token for explicit cancellation
    paretoStopToken = ev.token;
    return;
  } else if (ev.type === "step") {
    updateStepper(ev.step_num);
    $("hudStepDesc").textContent = ev.step_name;
    $("paretoStatus").textContent = `Step ${ev.step_num}/8: ${ev.step_name}…`;
  } else if (ev.type === "payoff_start") {
    $("hudTradeoffTitle").textContent = `Current Tradeoff: ${ev.pair}`;
    $("hudStatusBadge").textContent = "Calculating Payoff Table (Tight & Loose End)";
    const rangeBox = $("hudEpsRangeVal");
    if (rangeBox) rangeBox.textContent = `Solving bounds for ${ev.pair}...`;
  } else if (ev.type === "payoff_progress") {
    $("hudStepDesc").textContent = ev.desc || "Solving Payoff bounds...";
  } else if (ev.type === "payoff_done") {
    paretoPayoffTables[ev.pair] = { tight_end: ev.tight_end, loose_end: ev.loose_end, grid: ev.grid };
    $("hudStepDesc").textContent = `Payoff bounds: Tight=${ev.tight_end}, Loose=${ev.loose_end}. Grid: [${(ev.grid || []).join(", ")}]`;
    const rangeBox = $("hudEpsRangeVal");
    if (rangeBox) rangeBox.textContent = `[Tight: ${ev.tight_end} ... Loose: ${ev.loose_end}]`;
    const gridBox = $("hudEpsGridVal");
    if (gridBox) gridBox.textContent = `[${(ev.grid || []).join(", ")}]`;
  } else if (ev.type === "point_start") {
    $("hudEpsilonVal").textContent = ev.epsilon;
    $("hudRemainingVal").textContent = ev.remaining;
    $("hudTradeoffTitle").textContent = `Current Tradeoff: ${ev.pair}`;
    $("hudStatusBadge").textContent = `Testing ε = ${ev.epsilon} (Point ${ev.point_index}/${ev.total_points})`;

    const runEps = $("hudRunningEpsBadge");
    if (runEps) runEps.textContent = `ε = ${ev.epsilon} (Point ${ev.point_index}/${ev.total_points})`;

    const remList = $("hudRemainingListBadge");
    if (remList) {
      remList.textContent = ev.remaining_list && ev.remaining_list.length > 0 ? `[${ev.remaining_list.join(", ")}] (${ev.remaining} left)` : "Final point";
    }
    const compList = $("hudCompletedListBadge");
    if (compList) {
      compList.textContent = ev.completed_list && ev.completed_list.length > 0 ? `[${ev.completed_list.join(", ")}] (${ev.completed_list.length} done)` : "None yet";
    }

    if (ev.tight_end !== undefined && ev.loose_end !== undefined) {
      const rangeBox = $("hudEpsRangeVal");
      if (rangeBox) rangeBox.textContent = `[Tight: ${ev.tight_end} ... Loose: ${ev.loose_end}]`;
    }
  } else if (ev.type === "progress") {
    $("hudElapsedVal").textContent = `${ev.wall_elapsed_s}s`;
    $("hudEpsilonVal").textContent = ev.current_epsilon ?? "—";
    $("hudRemainingVal").textContent = ev.remaining_in_pair ?? "—";
  } else if (ev.type === "intermediate") {
    if (ev.faculty_score !== null && ev.faculty_score !== undefined) $("hudFacVal").textContent = ev.faculty_score;
    if (ev.student_score !== null && ev.student_score !== undefined) $("hudStuVal").textContent = ev.student_score;
    if (ev.resource_score !== null && ev.resource_score !== undefined) $("hudResVal").textContent = ev.resource_score;

    $("hudStatusBadge").textContent = `⚡ Live CP-SAT Candidate (Sol #${ev.count || 1} • ${ev.stage || 'Searching'})`;

    if (ev.remaining_list && $("hudRemainingListBadge")) {
      $("hudRemainingListBadge").textContent = `[${ev.remaining_list.join(", ")}]`;
    }

    if (ev.grids) {
      paretoGrids = ev.grids;
      $("paretoLiveTimetableWrap").style.display = "block";
      $("paretoTtTitle").innerHTML = `⚡ Live Streaming Timetable (${ev.pair || ''} &middot; ε = <b>${ev.epsilon || '—'}</b> &middot; Fac: ${ev.faculty_score ?? '—'}, Stu: ${ev.student_score ?? '—'})`;
      renderParetoTabs();
      renderParetoGrid();
    }
  } else if (ev.type === "point_done" || ev.type === "bypass") {
    const pt = ev.point;
    if (pt) {
      if (!pt.id) pt.id = paretoPoints.length + 1;
      paretoPoints.push(pt);
      $("hudFacVal").textContent = pt.faculty_score ?? "—";
      $("hudStuVal").textContent = pt.student_score ?? "—";
      $("hudResVal").textContent = pt.resource_score ?? "—";

      if (ev.remaining_list && $("hudRemainingListBadge")) {
        $("hudRemainingListBadge").textContent = ev.remaining_list.length > 0 ? `[${ev.remaining_list.join(", ")}]` : "Done";
      }
      if (ev.completed_list && $("hudCompletedListBadge")) {
        $("hudCompletedListBadge").textContent = `[${ev.completed_list.join(", ")}] (${ev.completed_list.length} done)`;
      }

      if (pt.grids) {
        paretoGrids = pt.grids;
        selectedParetoPointId = pt.id;
        $("paretoLiveTimetableWrap").style.display = "block";
        $("paretoTtTitle").innerHTML = `⚡ Epsilon Timetable <b>Point #${pt.id}</b> (ε = ${pt.epsilon}, Fac: ${pt.faculty_score}, Stu: ${pt.student_score}, Res: ${pt.resource_score})`;
        renderParetoTabs();
        renderParetoGrid();
      }
      renderParetoUnifiedResults();
    }
  } else if (ev.type === "complete") {
    paretoPoints = ev.points || paretoPoints;
    paretoPayoffTables = ev.payoff_tables || paretoPayoffTables;
    paretoRecommendations = ev.recommendations || {};
    renderParetoUnifiedResults();
    renderRecommendationBox();
    updateStepper(8);
  }
}

function renderParetoTabs() {
  renderModeTabs($("paretoTabs"), paretoGrids, paretoViewMode, paretoActiveDivision, (i) => {
    paretoActiveDivision = i;
    renderParetoTabs();
    renderParetoGrid();
  });
}

function renderParetoGrid() {
  const area = $("paretoGridArea");
  area.innerHTML = "";
  const table = buildGridTable(paretoGrids, paretoViewMode, paretoActiveDivision, null);
  if (table) area.appendChild(table);
}

function renderParetoUnifiedResults() {
  const wrap = $("paretoResultWrap");
  wrap.innerHTML = "";
  if (paretoPoints.length === 0) return;

  const card = document.createElement("div");
  card.className = "scatter-plot-card";

  const nonDomCount = paretoPoints.filter(p => !p.dominated && p.hard_violations === 0).length;
  card.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:12px;">
      <h3 style="margin:0;">Interactive Pareto Frontier (${nonDomCount} Non-Dominated Solutions, ${paretoPoints.length} Total Explored)</h3>
      <span class="notes">Solid orange points = Pareto optimal frontier &middot; Hollow grey = Dominated</span>
    </div>
    <div class="scatter-wrap" id="paretoScatterBox"></div>
    <div style="margin-top:16px;">
      <h4>Solutions Comparison &amp; Audit Table</h4>
      <table class="tt history-table" id="paretoTable">
        <thead>
          <tr>
            <th>Point</th>
            <th>Tradeoff Pair</th>
            <th>&epsilon; Bound</th>
            <th>Faculty Penalty</th>
            <th>Student Penalty</th>
            <th>Resource Penalty</th>
            <th>Violations</th>
            <th>Status</th>
            <th>Frontier</th>
            <th>Wall (s)</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody id="paretoTableBody"></tbody>
      </table>
    </div>
  `;
  wrap.appendChild(card);

  const scatterBox = $("paretoScatterBox");
  if (scatterBox && paretoPoints.length >= 1) {
    scatterBox.appendChild(buildUnifiedParetoScatter(paretoPoints));
  }

  const tbody = $("paretoTableBody");
  tbody.innerHTML = "";
  paretoPoints.forEach((p) => {
    const tr = document.createElement("tr");
    tr.style.cursor = "pointer";
    if (p.id === selectedParetoPointId) tr.className = "point-selected-row";
    if (p.dominated) tr.style.opacity = "0.6";

    const isWinner = paretoRecommendations.profiles && (
      paretoRecommendations.profiles.balanced === p.id ||
      paretoRecommendations.profiles.faculty_friendly === p.id ||
      paretoRecommendations.profiles.student_friendly === p.id
    );

    tr.innerHTML = `
      <td><b>#${p.id}</b> ${isWinner ? '⭐' : ''}</td>
      <td>${esc(p.pair || "—")}</td>
      <td><b>${p.epsilon ?? "—"}</b></td>
      <td>${p.faculty_score ?? "—"}</td>
      <td>${p.student_score ?? "—"}</td>
      <td>${p.resource_score ?? "—"}</td>
      <td>${p.hard_violations === 0 ? '<span style="color:var(--good); font-weight:700;">0 (Clash-Free)</span>' : p.hard_violations}</td>
      <td><span class="badge ${p.optimal ? 'badge-success' : 'badge-warning'}">${esc(p.status_name || (p.optimal ? 'OPTIMAL' : 'FEASIBLE'))}</span></td>
      <td><b>${p.dominated ? '<span style="color:var(--muted);">Dominated</span>' : '<span style="color:var(--accent-2);">Non-Dominated</span>'}</b></td>
      <td>${p.skipped_by_bypass ? 'Bypassed' : (Number(p.wall_s || 0).toFixed(1) + 's')}</td>
      <td><button type="button" class="dash-edit-btn" style="color:var(--accent); font-weight:700; cursor:pointer;">👁️ Inspect</button></td>
    `;
    tr.addEventListener("click", () => selectParetoPoint(p.id));
    tbody.appendChild(tr);
  });
}

function selectParetoPoint(pointId) {
  selectedParetoPointId = pointId;
  const pt = paretoPoints.find(p => p.id === pointId);
  if (!pt) return;

  if (pt.grids) {
    paretoGrids = pt.grids;
    $("paretoLiveTimetableWrap").style.display = "block";
    $("paretoTtTitle").innerHTML = `⭐ Selected Epsilon Timetable <b>Point #${pt.id}</b> (&epsilon; = ${pt.epsilon}, Fac: ${pt.faculty_score}, Stu: ${pt.student_score}, Res: ${pt.resource_score})`;
    renderParetoTabs();
    renderParetoGrid();
    $("paretoLiveTimetableWrap").scrollIntoView({ behavior: "smooth" });
  }
  renderParetoUnifiedResults();
}

function buildUnifiedParetoScatter(points) {
  const W = 520, H = 260, PAD = 48;
  const feasible = points.filter(p => p.hard_violations === 0 && p.faculty_score !== null && p.student_score !== null);
  if (feasible.length === 0) {
    const div = document.createElement("div");
    div.textContent = "No feasible points yet to plot.";
    return div;
  }

  const xs = feasible.map(p => p.faculty_score);
  const ys = feasible.map(p => p.student_score);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMin = Math.min(...ys), yMax = Math.max(...ys);

  const sx = (v) => PAD + (xMax === xMin ? (W - 2 * PAD) / 2 : ((v - xMin) / (xMax - xMin)) * (W - 2 * PAD));
  const sy = (v) => H - PAD - (yMax === yMin ? (H - 2 * PAD) / 2 : ((v - yMin) / (yMax - yMin)) * (H - 2 * PAD));

  const dots = feasible.map((p) => {
    const cx = sx(p.faculty_score);
    const cy = sy(p.student_score);
    const isSelected = p.id === selectedParetoPointId;
    if (p.dominated) {
      return `<circle cx="${cx}" cy="${cy}" r="${isSelected ? 6 : 4}" fill="none" stroke="var(--muted, #999)" stroke-width="${isSelected ? 2.5 : 1.5}"><title>Point #${p.id} (Dominated) - Fac: ${p.faculty_score}, Stu: ${p.student_score}</title></circle>`;
    } else {
      return `<circle cx="${cx}" cy="${cy}" r="${isSelected ? 7 : 5}" fill="var(--accent-2, #f26d21)" stroke="${isSelected ? '#003877' : '#fff'}" stroke-width="2"><title>Point #${p.id} (Pareto Optimal) - Fac: ${p.faculty_score}, Stu: ${p.student_score}, Res: ${p.resource_score}</title></circle>`;
    }
  }).join("");

  const sortedNonDom = feasible.filter(p => !p.dominated).sort((a, b) => a.faculty_score - b.faculty_score);
  const line = sortedNonDom.map(p => `${sx(p.faculty_score)},${sy(p.student_score)}`).join(" ");

  const svg = document.createElement("div");
  svg.innerHTML = `
    <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" style="background:#fff; border-radius:10px; border:1px solid var(--line);">
      <!-- Grid lines -->
      <line x1="${PAD}" y1="${H - PAD}" x2="${W - PAD}" y2="${H - PAD}" stroke="var(--line, #ccc)" stroke-width="1.5" />
      <line x1="${PAD}" y1="${PAD}" x2="${PAD}" y2="${H - PAD}" stroke="var(--line, #ccc)" stroke-width="1.5" />

      <!-- Labels -->
      <text x="${W / 2}" y="${H - 10}" text-anchor="middle" font-size="12" font-weight="700" fill="var(--muted)">Faculty Penalty Score (Lower is better &rarr;)</text>
      <text x="14" y="${H / 2}" text-anchor="middle" transform="rotate(-90 14 ${H / 2})" font-size="12" font-weight="700" fill="var(--muted)">Student Penalty (Lower is better &uarr;)</text>

      <text x="${PAD}" y="${H - PAD + 16}" font-size="11" fill="var(--muted)">${xMin}</text>
      <text x="${W - PAD}" y="${H - PAD + 16}" text-anchor="end" font-size="11" fill="var(--muted)">${xMax}</text>
      <text x="${PAD - 6}" y="${H - PAD}" text-anchor="end" font-size="11" fill="var(--muted)">${yMin}</text>
      <text x="${PAD - 6}" y="${PAD + 10}" text-anchor="end" font-size="11" fill="var(--muted)">${yMax}</text>

      <!-- Frontier Polyline -->
      <polyline points="${line}" fill="none" stroke="var(--accent, #003877)" stroke-width="2" stroke-dasharray="3,3" />
      ${dots}
    </svg>`;
  return svg;
}

function renderRecommendationBox() {
  const box = $("paretoRecBox");
  if (!box || !paretoRecommendations.profiles) return;
  box.style.display = "flex";

  const recId = paretoRecommendations.profiles[activeProfile] || paretoRecommendations.recommended_id;
  const bestPt = paretoPoints.find(p => p.id === recId);

  $("recPointIdBadge").textContent = bestPt ? `#${bestPt.id}` : "#—";
  if (bestPt) {
    $("recDesc").innerHTML = `Profile: <b>${activeProfile.replace('_', ' ').toUpperCase()}</b> &middot; Epsilon: <b>${bestPt.epsilon}</b> &middot; Faculty Penalty: <b>${bestPt.faculty_score}</b> &middot; Student Penalty: <b>${bestPt.student_score}</b> &middot; Resource Penalty: <b>${bestPt.resource_score}</b>`;
    $("recScoreVal").textContent = `${bestPt.faculty_score + bestPt.student_score + bestPt.resource_score} total penalty`;
    if (!selectedParetoPointId) {
      selectParetoPoint(bestPt.id);
    }
  }
}

function setPriorityProfile(profileName) {
  activeProfile = profileName;
  document.querySelectorAll(".priority-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.profile === profileName);
  });
  renderRecommendationBox();
}

async function saveSelectedParetoAsRun() {
  const pt = paretoPoints.find(p => p.id === selectedParetoPointId) || paretoPoints[0];
  if (!pt || !pt.grids) {
    alert("No valid Pareto point selected to save.");
    return;
  }

  const payload = {
    label: `Pareto Point #${pt.id} (ε=${pt.epsilon}, Fac:${pt.faculty_score}, Stu:${pt.student_score})`,
    solver: "cpsat (pareto optimal)",
    solution: pt.solution_dict || {},
    grids: pt.grids,
    hard: pt.hard_violations,
    soft: Number(pt.total_penalty || 0),
    wall_clock: pt.wall_s || 0.0,
    branch_ids: getSelectedBranchIds(),
  };

  try {
    const res = await fetch("/api/pareto/save-point-as-run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    alert(`Success: Timetable saved as Run #${data.run_id}! It is now available in History and active.`);
    loadSavedRun(data.run_id, true);
    loadHistory();
  } catch (e) {
    alert("Failed to save run: " + (e.message || e));
  }
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
  renderTabs();
  renderGrid();
  $("adjStatus").textContent = "restored original timetable.";
  $("restoreBtn").style.display = "none";
}

// ---------------------------------------------------------------- 8. history
let historyFilterMode = "all"; // "all" | "pareto" | "single"
let cachedSingleRuns = [];
let cachedParetoRuns = [];

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

function setHistoryFilter(mode) {
  historyFilterMode = mode;
  $("histTabAll").classList.toggle("active", mode === "all");
  $("histTabPareto").classList.toggle("active", mode === "pareto");
  $("histTabSingle").classList.toggle("active", mode === "single");
  renderHistoryTable();
}

async function loadSavedParetoRun(runId, autoScroll = true) {
  try {
    const res = await fetch(`/api/pareto/${runId}`);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    if (data.points && data.points.length > 0) {
      paretoPoints = data.points;
      paretoPayoffTables = data.payoff_tables || {};
      paretoRecommendations = data.recommendations || {};

      const istTime = formatIST(data.created_at);
      $("paretoHud").style.display = "block";
      $("paretoLiveTimetableWrap").style.display = "block";
      $("paretoResultWrap").style.display = "block";
      if ($("hudTradeoffTitle")) $("hudTradeoffTitle").textContent = `Loaded Pareto Sweep #${runId} · ${data.label || ""} (${istTime})`;
      if ($("hudStatusBadge")) $("hudStatusBadge").textContent = `Loaded Run #${runId} (${paretoPoints.length} points)`;
      if ($("paretoStatus")) $("paretoStatus").textContent = `Loaded Pareto Sweep #${runId} generated at ${istTime}: ${paretoPoints.length} frontier solutions available to explore and inspect.`;

      const recId = paretoRecommendations.profiles ? paretoRecommendations.profiles.balanced : paretoPoints[0].id;
      selectParetoPoint(recId || paretoPoints[0].id);
      renderParetoUnifiedResults();
      renderRecommendationBox();
      updateStepper(8);

      if (autoScroll) {
        $("paretoSection").scrollIntoView({ behavior: "smooth" });
      }
    } else {
      alert(`Pareto Run #${runId} contains no solution points.`);
    }
  } catch (e) {
    alert("Failed to load Pareto run: " + (e.message || e));
  }
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
    const [runsRes, paretoRes] = await Promise.all([
      fetch("/api/runs").catch(() => null),
      fetch("/api/pareto/runs").catch(() => null),
    ]);

    cachedSingleRuns = runsRes && runsRes.ok ? await runsRes.json() : [];
    cachedParetoRuns = paretoRes && paretoRes.ok ? await paretoRes.json() : [];

    renderHistoryTable();

    if (!currentGrids && cachedSingleRuns.length > 0) {
      const bestRun = cachedSingleRuns.find(r => r.status === "done" && r.hard === 0) || cachedSingleRuns.find(r => r.status === "done");
      if (bestRun) loadSavedRun(bestRun.id, false);
    }
  } catch (e) {
    // history best-effort
  }
}

function renderHistoryTable() {
  const body = $("historyBody");
  if (!body) return;
  body.innerHTML = "";

  const items = [];
  if (historyFilterMode === "all" || historyFilterMode === "single") {
    cachedSingleRuns.forEach(r => {
      items.push({
        kind: "single",
        id: r.id,
        label: r.label || "Single Timetable",
        solver: r.solver,
        status: r.status,
        hard: r.hard,
        soft: r.soft !== null && r.soft !== undefined ? Number(r.soft).toFixed(1) : "—",
        created_at: r.created_at,
      });
    });
  }

  if (historyFilterMode === "all" || historyFilterMode === "pareto") {
    cachedParetoRuns.forEach(r => {
      items.push({
        kind: "pareto",
        id: r.id,
        label: r.label || "Multi-Objective Pareto Sweep",
        solver: `CP-SAT Multi-Objective (${r.points_count || 0} solutions)`,
        status: r.status,
        hard: "0 (Clash-Free)",
        soft: `${r.points_count || 0} Frontier Pts`,
        created_at: r.created_at,
      });
    });
  }

  items.sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0));

  if (items.length === 0) {
    body.innerHTML = `<tr><td colspan="8" style="text-align:center; color:var(--muted); padding:20px;">No runs in history.</td></tr>`;
    return;
  }

  items.forEach(r => {
    const tr = document.createElement("tr");
    tr.style.cursor = "pointer";
    tr.title = r.kind === "pareto" ? `Click to inspect Pareto Run #${r.id}` : `Click to load Run #${r.id} timetable`;
    const created = formatIST(r.created_at);

    const isPareto = r.kind === "pareto";
    const typePill = isPareto
      ? `<span class="badge" style="background:linear-gradient(135deg,#003877,#1e40af); color:#fff; font-weight:700;">⚡ Multi-Objective Sweep</span>`
      : `<span class="badge" style="background:var(--panel-2); color:var(--text); font-weight:600;">Single Timetable</span>`;

    const actionBtn = isPareto
      ? `<button type="button" class="dash-edit-btn" style="background:linear-gradient(135deg,#f97316,#ea580c); color:#fff; font-weight:700; cursor:pointer; padding:4px 10px; border-radius:6px; border:none;">⚡ View Pareto (${r.soft})</button>`
      : `<button type="button" class="dash-edit-btn" style="color:var(--accent); font-weight:600; cursor:pointer; padding:3px 10px;">👁️ Load</button>`;

    tr.innerHTML = `
      <td><b>#${r.id}</b></td>
      <td>${typePill} <span style="font-weight:600; margin-left:4px;">${esc(r.label)}</span></td>
      <td>${esc(r.solver)}</td>
      <td><span class="badge ${r.status === "done" ? "badge-success" : (r.status === "queued" ? "badge-warning" : "badge-danger")}">${r.status}</span></td>
      <td><b>${r.hard ?? "—"}</b></td>
      <td>${r.soft}</td>
      <td>${created}</td>
      <td>${actionBtn}</td>
    `;

    tr.addEventListener("click", () => {
      if (isPareto) {
        loadSavedParetoRun(r.id, true);
      } else {
        loadSavedRun(r.id, true);
      }
    });

    body.appendChild(tr);
  });
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
loadSeedDatasets();
loadBranches();
loadHistory();
