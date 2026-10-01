// Platform page (P2 generate-from-DB): seed -> readiness -> generate (DB-backed run) -> poll ->
// render grids -> export -> history. Grid rendering (renderGrid/renderTabs/renderStages) is
// adapted from the legacy showcase's app.js; the `grids` JSON shape is identical, and the same
// CSS classes from style.css are reused so this page looks consistent with the showcase.
const $ = (id) => document.getElementById(id);

const TYPE_CLASS = { Theory: "theory", Practical: "lab", Tutorial: "tutorial", Break: "break" };

let currentGrids = null;
let currentViewMode = "divisions";  // "divisions" | "classrooms" | "labs" | "teachers"
let activeDivision = 0;
let currentRunId = null;
let pollTimer = null;
let originalGrids = null;          // the un-adjusted run grids, so "Restore original" can revert
let movedIds = new Set();          // session ids relocated by the last adjustment (for highlighting)

// Compare-solvers panel state. Kept entirely separate from currentGrids/activeDivision/movedIds
// above: /api/compare is a one-shot synchronous call that never creates a run, so it must never
// disturb the generated run that the adjust panel operates on.
let compareResults = null;         // results[] from the last successful /api/compare response
let compareViewMode = "divisions";  // "divisions" | "classrooms" | "labs" | "teachers"
let compareActiveSolver = 0;       // index into compareResults for the active solver tab
let compareActiveDivision = 0;     // entity tab index within the active solver's grids

$("seedBtn").addEventListener("click", loadSeed);
$("branchRefreshBtn").addEventListener("click", () => loadBranches());
// Branch/Year cascade downward (a new branch changes which years exist, and so on); Semester is
// the leaf, so it only needs to re-resolve.
$("deptSelect").addEventListener("change", () => { rebuildClassSelectors(); checkReadiness(); });
$("yearSelect").addEventListener("change", () => { rebuildClassSelectors(); checkReadiness(); });
$("semSelect").addEventListener("change", () => { renderResolvedBranch(); checkReadiness(); });
$("generateBtn").addEventListener("click", () => generate("selected"));
const oddBtn = $("generateOddYearsBtn");
if (oddBtn) oddBtn.addEventListener("click", () => generate("odd"));
const evenBtn = $("generateEvenYearsBtn");
if (evenBtn) evenBtn.addEventListener("click", () => generate("even"));
$("generateAllYearsBtn").addEventListener("click", () => generate("all"));
$("compareBtn").addEventListener("click", runCompare);
$("adjustBtn").addEventListener("click", adjust);
$("restoreBtn").addEventListener("click", restoreOriginal);
$("adjScope").addEventListener("change", () => {
  $("adjFromWrap").style.display = $("adjScope").value === "from" ? "" : "none";
});

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
    sel.innerHTML = datasets.map((d) => `<option value="${d.name}">${d.name} — ${d.branch_code}</option>`).join("");
    $("seedStatus").textContent = `${datasets.length} dataset(s) available to load.`;
  } catch (e) {
    $("seedStatus").textContent = "backend not reachable — start the server on port 8750.";
  }
}

async function loadSeed() {
  const btn = $("seedBtn");
  const dataset = $("seedDataset").value;
  if (!dataset) { $("seedStatus").textContent = "no dataset selected."; return; }
  btn.disabled = true;
  $("seedStatus").textContent = `loading "${dataset}"…`;
  try {
    const res = await fetch(`/api/seed/${encodeURIComponent(dataset)}`, { method: "POST" });
    if (res.status === 409) {
      $("seedStatus").textContent = `"${dataset}" already loaded — continuing.`;
    } else if (!res.ok) {
      const text = await res.text();
      $("seedStatus").textContent = `error (HTTP ${res.status}) — ${text.slice(0, 200)}`;
    } else {
      const data = await res.json();
      $("seedStatus").textContent =
        `loaded: ${data.divisions} divisions, ${data.faculty} faculty, ${data.courses} courses, ` +
        `${data.rooms} rooms, ${data.slots} slots (branch ${data.branch_code}).`;
    }
  } catch (e) {
    $("seedStatus").textContent = "backend not reachable — start the server on port 8750.";
  } finally {
    btn.disabled = false;
    await loadBranches();
    checkReadiness();
  }
}

// ---------------------------------------------------------------- 2. class selection
// A branch row is one (department, year, semester) triple. Rather than make the user read
// "CSE-DS-TY-SEM5" out of a single list, three cascading dropdowns narrow to exactly one row.
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
    // best-effort UI sugar; readiness/generate already surface backend-unreachable errors
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
  // order years by the semester they teach, so SY precedes TY precedes Final Year
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

// The single branch row the three dropdowns currently resolve to (or null if nothing matches).
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

// Show a red alert for every selected branch that carries a Branch.notice caveat. These flag
// known gaps in what is modelled (e.g. Sem VII's D1 absent on OJT) -- surfaced up-front rather
// than after a solve, because a clean-looking timetable is exactly when such a gap gets missed.
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
  return b ? [b.id] : null;   // null == no filter (whole institution)
}

function branchQuery() {
  const ids = getSelectedBranchIds();
  return ids ? "?" + ids.map((id) => `branch_ids=${id}`).join("&") : "";
}

// ---------------------------------------------------------------- 3. readiness
async function checkReadiness() {
  const banner = $("readyBanner");
  banner.textContent = "checking…";
  banner.className = "readiness-banner";
  try {
    const res = await fetch("/api/readiness" + branchQuery());
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    renderReadiness(data);
  } catch (e) {
    banner.textContent = "Could not reach the backend API. Is the server running on port 8750?";
    banner.className = "readiness-banner not-ready";
  }
}

function renderReadiness(data) {
  const banner = $("readyBanner");
  if (data.ready) {
    banner.className = "readiness-banner ready";
    banner.innerHTML = "&#10003; Ready to generate";
  } else {
    banner.className = "readiness-banner not-ready";
    const items = (data.issues || []).map((i) => `<li>${i}</li>`).join("");
    banner.innerHTML = `<b>Not ready yet:</b><ul>${items}</ul>`;
  }
}

// ---------------------------------------------------------------- 3. generate
// `allYears=true` ignores the Branch/Year/Semester picker and solves the whole institution
// (branch_ids: null) in one run, sharing rooms/faculty across every loaded year — this is what
// lets "My Timetable" show one teacher's lectures across SY/TY/Final Year, since that view just
// reads the most recent "done" run and a single-branch run can never contain another year's
// sessions.
let genStartTime = null;
let genTimerInterval = null;

async function generate(mode = "selected") {
  const genBtn = $("generateBtn"), allBtn = $("generateAllYearsBtn");
  const oddBtn = $("generateOddYearsBtn"), evenBtn = $("generateEvenYearsBtn");
  [genBtn, allBtn, oddBtn, evenBtn].forEach(b => { if (b) b.disabled = true; });

  let branch_ids = null;
  let label = "All Years";
  if (mode === "odd") {
    const oddBranches = allBranches.filter(b => b.semester && b.semester % 2 !== 0);
    branch_ids = oddBranches.length ? oddBranches.map(b => b.id) : [2, 3, 4];
    label = "All Odd Semesters (Sem 3 + 5 + 7)";
  } else if (mode === "even") {
    const evenBranches = allBranches.filter(b => b.semester && b.semester % 2 === 0);
    branch_ids = evenBranches.length ? evenBranches.map(b => b.id) : [1];
    label = "All Even Semesters (Sem 4 + 6 + 8)";
  } else if (mode === "selected") {
    branch_ids = getSelectedBranchIds();
    label = "";
  } else {
    branch_ids = null;
    label = "All Loaded Years";
  }

  const statusEl = $("genStatus");
  statusEl.className = "status-line active-gen";
  statusEl.innerHTML = `<div class="gen-spinner"></div> <span>Submitting <b>${label || "selected class"}</b> to solver…</span>`;
  $("summary").style.display = "none";
  $("stagesWrap").style.display = "none";
  $("exportRow").style.display = "none";
  $("tabs").style.display = "none";
  $("legend").style.display = "none";
  $("gridArea").innerHTML = "";
  currentGrids = null;
  currentRunId = null;
  clearTimeout(pollTimer);
  clearInterval(genTimerInterval);

  const payload = {
    solver: $("solver").value || "cpsat",
    time_limit: parseFloat($("timeLimit").value) || 180,
    label: label,
    branch_ids: branch_ids,
  };

  try {
    const res = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (res.status === 400) {
      const body = await res.json();
      const issues = Array.isArray(body.detail) ? body.detail : [String(body.detail)];
      renderReadiness({ ready: false, issues });
      statusEl.className = "status-line";
      statusEl.textContent = "not ready — see the readiness banner above.";
      [genBtn, allBtn, oddBtn, evenBtn].forEach(b => { if (b) b.disabled = false; });
      return;
    }
    if (res.status === 409) {
      statusEl.className = "status-line";
      statusEl.textContent = "a run is already in progress — try again shortly.";
      [genBtn, allBtn, oddBtn, evenBtn].forEach(b => { if (b) b.disabled = false; });
      return;
    }
    if (!res.ok) {
      const text = await res.text();
      throw new Error("HTTP " + res.status + " — " + text.slice(0, 300));
    }
    const data = await res.json();
    currentRunId = data.run_id;
    genStartTime = Date.now();
    statusEl.className = "status-line active-gen";
    statusEl.innerHTML = `<div class="gen-spinner"></div> <span>⏳ <b>Solver Running (Run #${currentRunId})</b>: Optimizing ${label || "timetable"} across all divisions, classrooms, labs & faculty… <b id="genTimer">0s elapsed</b></span>`;
    
    genTimerInterval = setInterval(() => {
      const el = $("genTimer");
      if (el && genStartTime) {
        const s = Math.round((Date.now() - genStartTime) / 1000);
        el.textContent = `${s}s elapsed`;
      }
    }, 1000);

    pollRun(currentRunId);
    loadHistory();
  } catch (e) {
    statusEl.className = "status-line";
    statusEl.textContent = "error: " + (e.message || e);
    clearInterval(genTimerInterval);
    [genBtn, allBtn, oddBtn, evenBtn].forEach(b => { if (b) b.disabled = false; });
  }
}

function pollRun(runId) {
  fetch(`/api/runs/${runId}`)
    .then((res) => {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.json();
    })
    .then((run) => {
      const statusEl = $("genStatus");
      if (run.status === "queued" || run.status === "running") {
        const s = genStartTime ? Math.round((Date.now() - genStartTime) / 1000) : 0;
        statusEl.className = "status-line active-gen";
        statusEl.innerHTML = `<div class="gen-spinner"></div> <span>⏳ <b>Solver Running (Run #${runId})</b>: Optimizing ${run.label || "timetable"} with CP-SAT… <b id="genTimer">${s}s elapsed</b></span>`;
        pollTimer = setTimeout(() => pollRun(runId), 1500);
        return;
      }
      clearInterval(genTimerInterval);
      const allBtns = [$("generateBtn"), $("generateAllYearsBtn"), $("generateOddYearsBtn"), $("generateEvenYearsBtn")];
      allBtns.forEach(b => { if (b) b.disabled = false; });

      if (run.status === "done") {
        if (run.hard === 0) {
          statusEl.className = "status-line ok";
          statusEl.innerHTML = `✅ <b>Run #${runId} Done!</b> Successfully generated 100% clash-free timetable in ${run.wall_clock ? Number(run.wall_clock).toFixed(1) : ""}s.`;
        } else {
          statusEl.className = "status-line bad";
          statusEl.innerHTML = `⚠️ <b>Run #${runId} Completed with ${run.hard} hard violation(s).</b>`;
        }
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
        $("resultSection").scrollIntoView({ behavior: "smooth" });
      } else {
        statusEl.className = "status-line bad";
        statusEl.textContent = `❌ Run #${runId} failed: ${run.error || "unknown error"}`;
      }
      loadHistory();
    })
    .catch((e) => {
      clearInterval(genTimerInterval);
      $("genStatus").textContent = "error polling run: " + (e.message || e);
      const allBtns = [$("generateBtn"), $("generateAllYearsBtn"), $("generateOddYearsBtn"), $("generateEvenYearsBtn")];
      allBtns.forEach(b => { if (b) b.disabled = false; });
    });
}

function enableExport(runId) {
  const row = $("exportRow");
  row.style.display = "flex";
  $("exportXlsx").href = `/api/runs/${runId}/export.xlsx`;
  $("exportPdf").href = `/api/runs/${runId}/export.pdf`;
}

// ---------------------------------------------------------------- 4. result rendering
function renderSummary(run) {
  $("summary").style.display = "flex";
  $("statSolver").textContent = run.solver;
  $("statStatus").textContent = run.status;
  const hard = $("statHard");
  hard.textContent = run.hard;
  hard.className = "stat-val " + (run.hard === 0 ? "good" : "bad");
  $("statSoft").textContent = typeof run.soft === "number" ? run.soft.toFixed(1) : run.soft;
  // wall_clock is the solver's total solve time (pipeline total, or the single solver's own).
  $("statWall").textContent = typeof run.wall_clock === "number" ? run.wall_clock.toFixed(1) + "s" : "—";
  renderSolveWarning(run);
}

// An infeasible solve reports status "done" with soft cost 0.0 -- which reads like a perfect
// result when it actually means NOTHING was scheduled and there is no timetable at all. Detect
// that case (no grid content, or every session unplaced) and say so plainly, so a failure is
// never mistaken for a success.
function renderSolveWarning(run) {
  const box = $("solveWarning");
  if (!box) return;
  // grids shape (engine/view.py solution_to_grids): divisions[].cells is an OBJECT keyed
  // "<day>_<period>" -> [session, ...], not an array -- count the placed sessions across it.
  const placed = (run.grids && Array.isArray(run.grids.divisions))
    ? run.grids.divisions.reduce(
        (n, d) => n + Object.values(d.cells || {}).reduce((m, arr) => m + (arr ? arr.length : 0), 0), 0)
    : null;
  const nothingPlaced = placed === 0;

  if (nothingPlaced) {
    box.style.display = "";
    box.className = "readiness-banner not-ready";
    box.innerHTML =
      "<b>No timetable was produced.</b> The solver proved these constraints cannot all be " +
      "satisfied at once, so nothing was scheduled &mdash; the soft cost of 0.0 reflects an " +
      "empty timetable, not a good one. Common causes: a practical whose two batch-halves share " +
      "one teacher (they run simultaneously in different labs), a division needing a lab on " +
      "every teaching day but having fewer labs than days, or weekly hours outside the 6&ndash;8h " +
      "per day window.";
  } else if (run.hard > 0) {
    box.style.display = "";
    box.className = "readiness-banner not-ready";
    box.innerHTML =
      `<b>Partial timetable.</b> ${run.hard} hard constraint violation(s) remain &mdash; this ` +
      "schedule is not usable as-is. Try a longer time limit, or the hybrid pipeline.";
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

// Generic entity helper: returns the array of items for the given view mode
function getEntitiesForMode(grids, mode) {
  if (!grids) return [];
  if (mode === "classrooms") return grids.classrooms || [];
  if (mode === "labs") return grids.labs || [];
  if (mode === "teachers") return grids.teachers || [];
  return grids.divisions || [];
}

// Generic mode-tab renderer: renders tabs for Divisions, Classrooms, Labs, or Teachers
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

// Generic grid-table builder: pure function of (grids, mode, activeIdx, movedSet) -> <table> or null.
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
  const table = buildGridTable(currentGrids, currentViewMode, activeDivision, movedIds);
  $("gridArea").innerHTML = "";
  if (table) $("gridArea").appendChild(table);
}

// ---------------------------------------------------------------- 5. compare solvers
function selectedCompareSolvers() {
  const solvers = [];
  if ($("cmpGreedy").checked) solvers.push("greedy");
  if ($("cmpMip").checked) solvers.push("mip");
  if ($("cmpGa").checked) solvers.push("ga");
  if ($("cmpCpsat").checked) solvers.push("cpsat");
  if ($("cmpPipeline").checked) solvers.push("pipeline");
  return solvers;
}

async function runCompare() {
  const solvers = selectedCompareSolvers();
  if (solvers.length < 2) {
    $("compareStatus").textContent =
      "pick at least 2 solvers to compare — a single solver is just Generate above.";
    return;
  }

  const btn = $("compareBtn");
  btn.disabled = true;
  $("compareResultWrap").style.display = "none";
  $("compareStatus").textContent =
    `running ${solvers.length} solver(s) (${solvers.join(", ")}) back-to-back — this can take a ` +
    `while (cpsat/pipeline may each take 20–60s)…`;

  const payload = {
    time_limit: parseFloat($("compareTimeLimit").value) || 20,
    label: "",
    branch_ids: getSelectedBranchIds(),
    solvers,
  };

  try {
    const res = await fetch("/api/compare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (res.status === 400) {
      const body = await res.json();
      const detail = body.detail;
      if (Array.isArray(detail)) {
        const items = detail.map((d) => `<li>${d}</li>`).join("");
        $("compareStatus").innerHTML = `<b>Not ready to generate:</b><ul>${items}</ul>`;
      } else {
        $("compareStatus").textContent = "error: " + String(detail);
      }
      return;
    }
    if (!res.ok) {
      const text = await res.text();
      throw new Error("HTTP " + res.status + " — " + text.slice(0, 300));
    }
    const data = await res.json();
    compareResults = data.results;
    compareActiveSolver = typeof data.best_index === "number" ? data.best_index : 0;
    compareActiveDivision = 0;

    $("compareStatus").textContent =
      `done — compared ${data.solvers.length} solver(s); winner: ${data.best_solver}.`;
    renderCompareTable(data);
    $("compareResultWrap").style.display = "block";
    renderCompareSolverTabs();
    renderCompareDivisionTabs();
    renderCompareGrid();
  } catch (e) {
    $("compareStatus").textContent = "error: " + (e.message || e);
  } finally {
    btn.disabled = false;
  }
}

function renderCompareTable(data) {
  const body = $("compareTableBody");
  body.innerHTML = "";
  data.results.forEach((r, i) => {
    const tr = document.createElement("tr");
    const isBest = i === data.best_index;
    if (isBest) tr.className = "compare-winner";
    const hardClass = r.hard_violations === 0 ? "good" : "bad";
    const soft = typeof r.soft_cost === "number" ? r.soft_cost.toFixed(1) : r.soft_cost;
    const wall = typeof r.wall_clock_s === "number" ? r.wall_clock_s.toFixed(1) : r.wall_clock_s;
    tr.innerHTML =
      `<td>${r.solver}${isBest ? ' <span class="compare-badge">best</span>' : ""}</td>` +
      `<td>${r.status}</td>` +
      `<td><span class="stat-val ${hardClass}" style="font-size:14px">${r.hard_violations}</span></td>` +
      `<td>${soft}</td>` +
      `<td>${wall}</td>`;
    body.appendChild(tr);
  });
}

function renderCompareSolverTabs() {
  const tabs = $("compareTabs");
  if (!compareResults || compareResults.length === 0) {
    tabs.style.display = "none";
    tabs.innerHTML = "";
    return;
  }
  tabs.style.display = "flex";
  tabs.innerHTML = "";
  compareResults.forEach((r, i) => {
    const t = document.createElement("div");
    t.className = "tab" + (i === compareActiveSolver ? " active" : "");
    t.textContent = r.solver;
    t.onclick = () => {
      compareActiveSolver = i;
      compareActiveDivision = 0;
      renderCompareSolverTabs();
      renderCompareDivisionTabs();
      renderCompareGrid();
    };
    tabs.appendChild(t);
  });
}

function renderCompareDivisionTabs() {
  const active = compareResults ? compareResults[compareActiveSolver] : null;
  renderModeTabs($("compareDivTabs"), active ? active.grids : null, compareViewMode, compareActiveDivision, (i) => {
    compareActiveDivision = i;
    renderCompareDivisionTabs();
    renderCompareGrid();
  });
}

function renderCompareGrid() {
  const active = compareResults ? compareResults[compareActiveSolver] : null;
  const table = buildGridTable(active ? active.grids : null, compareViewMode, compareActiveDivision, new Set());
  $("compareGridArea").innerHTML = "";
  if (table) $("compareGridArea").appendChild(table);
}

// ---------------------------------------------------------------- 6. holiday / rain adjustment
async function adjust() {
  if (!currentRunId) return;
  const btn = $("adjustBtn");
  btn.disabled = true;
  $("adjStatus").textContent = "adjusting…";
  const scope = $("adjScope").value;
  const day = parseInt($("adjDay").value, 10);
  const payload = {
    day,
    from_period: scope === "from" ? (parseInt($("adjFrom").value, 10) || 0) : null,
    reason: scope === "from" ? "rain" : "holiday",
    solver: $("adjSolver").value,
    time_limit_s: parseFloat($("adjTimeLimit").value) || 60,
    // the disrupted day is always relaxed server-side; only send the EXTRA ones the admin ticked
    extra_relaxed_days: [...$("adjRelaxDays").querySelectorAll("input:checked")]
      .map((c) => parseInt(c.value, 10))
      .filter((d) => d !== day),
  };
  $("adjUnplaced").style.display = "none";
  try {
    const res = await fetch(`/api/runs/${currentRunId}/adjust`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error("HTTP " + res.status + " — " + text.slice(0, 200));
    }
    const d = await res.json();
    movedIds = new Set(d.moved.filter((m) => !m.dropped).map((m) => m.session_id));
    currentGrids = d.grids;
    renderTabs();
    renderGrid();
    $("adjStatus").innerHTML =
      `Adjusted: <b>${d.disrupted_day}</b>, ${d.scope} — re-solved with <b>${d.solver}</b>; ` +
      `${d.moved_count} session(s) moved. Moved sessions are highlighted below.`;
    renderUnplaced(d.unplaced_sessions || []);
    $("restoreBtn").style.display = "";
  } catch (e) {
    $("adjStatus").textContent = "error: " + (e.message || e);
  } finally {
    btn.disabled = false;
  }
}

// The over-constrained case (typically a whole-day holiday) can leave sessions with nowhere legal
// to go. Naming them — course, division, faculty — is the point: an admin can only rearrange what
// they can see. A bare "7 dropped" is not actionable.
function renderUnplaced(unplaced) {
  const box = $("adjUnplaced");
  if (!unplaced.length) {
    box.style.display = "none";
    return;
  }
  box.innerHTML =
    `<b>${unplaced.length} session(s) could not be placed anywhere in the week.</b> ` +
    `The disruption removes more capacity than the remaining days can absorb. ` +
    `Tick extra days to relax above and re-apply, or rearrange these by hand:`;
  // labels embed admin-entered faculty/course names — build the list with textContent so a stray
  // "<" in a name renders as text instead of markup
  const list = document.createElement("ul");
  for (const u of unplaced) {
    const li = document.createElement("li");
    li.textContent = u.label || u.session_id;
    list.appendChild(li);
  }
  box.appendChild(list);
  box.style.display = "";
}

function restoreOriginal() {
  if (!originalGrids) return;
  currentGrids = originalGrids;
  movedIds = new Set();
  renderTabs();
  renderGrid();
  $("adjStatus").textContent = "restored the original (un-adjusted) timetable.";
  $("restoreBtn").style.display = "none";
}

// ---------------------------------------------------------------- 7. history
async function loadSavedRun(runId, autoScroll = true) {
  try {
    const statusEl = $("genStatus");
    if (statusEl) statusEl.textContent = `loading run #${runId}…`;
    const res = await fetch(`/api/runs/${runId}`);
    if (!res.ok) throw new Error("HTTP " + res.status);
    const run = await res.json();
    if (run.status === "done" && run.grids) {
      const badge = $("activeRunBadge");
      if (badge) badge.innerHTML = `<span class="badge badge-success">Showing Run #${runId}</span> <b>${run.label || "Saved Timetable"}</b> (${run.solver})`;
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
      if (statusEl) statusEl.textContent = `loaded run #${runId} (${run.label || "Saved Timetable"}).`;
      if (autoScroll) {
        $("resultSection").scrollIntoView({ behavior: "smooth" });
      }
    } else {
      if (statusEl) statusEl.textContent = `run #${runId} status: ${run.status} (hard: ${run.hard}, error: ${run.error || "none"})`;
    }
  } catch (e) {
    if ($("genStatus")) $("genStatus").textContent = `failed to load run #${runId}: ${e.message || e}`;
  }
}

async function loadHistory() {
  try {
    const res = await fetch("/api/runs");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const runs = await res.json();
    const body = $("historyBody");
    body.innerHTML = "";
    runs.forEach((r) => {
      const tr = document.createElement("tr");
      tr.style.cursor = "pointer";
      tr.title = `Click to load Run #${r.id} timetable`;
      const created = (() => {
        const d = new Date(r.created_at);
        return isNaN(d.getTime()) ? r.created_at : d.toLocaleString();
      })();
      tr.innerHTML = `<td><b>#${r.id}</b></td><td>${r.label || ""}</td><td>${r.solver}</td>` +
        `<td><span class="badge ${r.status === "done" ? (r.hard === 0 ? "badge-success" : "badge-warning") : "badge-danger"}">${r.status}</span></td>` +
        `<td><b>${r.hard ?? ""}</b></td><td>${r.soft ? Number(r.soft).toFixed(1) : ""}</td><td>${created}</td>` +
        `<td><button type="button" class="dash-edit-btn" style="color:var(--accent); font-weight:600; cursor:pointer; padding:3px 10px;">👁️ Load</button></td>`;
      tr.addEventListener("click", () => loadSavedRun(r.id, true));
      body.appendChild(tr);
    });

    // Automatically load the latest successful run if no timetable is currently rendered!
    if (!currentGrids && runs.length > 0) {
      const bestRun = runs.find(r => r.status === "done" && r.hard === 0) || runs.find(r => r.status === "done");
      if (bestRun) {
        loadSavedRun(bestRun.id, false);
      }
    }
  } catch (e) {
    // history is a nice-to-have; stay quiet on failure
  }
}

// ---------------------------------------------------------------- 6. pareto sweep
let paretoRunId = null;
let paretoPollTimer = null;

$("paretoBtn").addEventListener("click", runParetoSweep);

function selectedParetoPairs() {
  const pairs = [];
  if ($("parFacStu").checked) pairs.push(["faculty", "students"]);
  if ($("parFacLab").checked) pairs.push(["faculty", "labs"]);
  if ($("parStuLab").checked) pairs.push(["students", "labs"]);
  return pairs;
}

async function runParetoSweep() {
  const btn = $("paretoBtn");
  const pairs = selectedParetoPairs();
  if (pairs.length === 0) {
    $("paretoStatus").textContent = "pick at least one objective pair.";
    return;
  }
  btn.disabled = true;
  $("paretoStatus").textContent = "submitting…";
  $("paretoResultWrap").style.display = "none";
  clearTimeout(paretoPollTimer);

  const payload = {
    pairs,
    time_limit_s: parseFloat($("parTimeLimit").value) || 45,
    sweep_points: parseInt($("parSweepPoints").value, 10) || 5,
    branch_ids: getSelectedBranchIds(),
  };

  try {
    const res = await fetch("/api/pareto", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (res.status === 400) {
      const body = await res.json();
      const issues = Array.isArray(body.detail) ? body.detail : [String(body.detail)];
      $("paretoStatus").textContent = "not ready: " + issues.join("; ");
      btn.disabled = false;
      return;
    }
    if (!res.ok) {
      const text = await res.text();
      throw new Error("HTTP " + res.status + " — " + text.slice(0, 300));
    }
    const data = await res.json();
    paretoRunId = data.run_id;
    $("paretoStatus").textContent = `sweep #${paretoRunId} queued…`;
    pollParetoRun(paretoRunId);
  } catch (e) {
    $("paretoStatus").textContent = "error: " + (e.message || e);
    btn.disabled = false;
  }
}

function pollParetoRun(runId) {
  fetch(`/api/pareto/${runId}`)
    .then((res) => {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.json();
    })
    .then((run) => {
      if (run.status === "queued" || run.status === "running") {
        $("paretoStatus").textContent = `sweep #${runId} ${run.status}…`;
        paretoPollTimer = setTimeout(() => pollParetoRun(runId), 2000);
        return;
      }
      $("paretoBtn").disabled = false;
      if (run.status === "done") {
        $("paretoStatus").textContent = `sweep #${runId} done.`;
        renderParetoResults(run.points);
      } else {
        $("paretoStatus").textContent = `sweep #${runId} failed: ${run.error || "unknown error"}`;
      }
    })
    .catch((e) => {
      $("paretoStatus").textContent = "error polling sweep: " + (e.message || e);
      $("paretoBtn").disabled = false;
    });
}

function renderParetoResults(points) {
  const wrap = $("paretoResultWrap");
  wrap.innerHTML = "";
  const pairLabels = Object.keys(points || {});
  if (pairLabels.length === 0) {
    wrap.innerHTML = '<p class="notes">No pairs returned.</p>';
    wrap.style.display = "";
    return;
  }
  pairLabels.forEach((label) => {
    const rows = points[label] || [];
    const section = document.createElement("div");
    section.style.marginTop = "16px";
    if (rows.length === 0) {
      section.innerHTML = `<h3>${label}</h3><p class="notes">No feasible frontier found for this pair within the time budget — try a larger time limit per point.</p>`;
      wrap.appendChild(section);
      return;
    }
    // Runs stored before the dominance filter existed lack these flags; treat those points as
    // optimal, non-dominated and actually solved, which is what the old sweep implicitly claimed.
    const feasible = rows.filter((r) => r.hard_violations === 0 && r.bound_value != null);
    const table = document.createElement("table");
    table.className = "tt history-table";
    table.innerHTML =
      `<thead><tr><th>${rows[0].bound_category} (bounded)</th><th>${rows[0].minimize_category} (minimized)</th>` +
      `<th>hard violations</th><th>optimal</th><th>on frontier</th><th>wall (s)</th></tr></thead>`;
    const tbody = document.createElement("tbody");
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      const wall = r.skipped_by_bypass ? "skipped (bypass)" : r.wall_s.toFixed(1);
      tr.innerHTML = `<td>${r.bound_value ?? "—"}</td><td>${r.minimize_value ?? "—"}</td>` +
        `<td>${r.hard_violations}</td><td>${r.optimal === false ? "no (time limit)" : "yes"}</td>` +
        `<td>${r.dominated ? "dominated" : "yes"}</td><td>${wall}</td>`;
      if (r.dominated) tr.style.opacity = "0.55";
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);

    const heading = document.createElement("h3");
    heading.textContent = label;
    section.appendChild(heading);
    section.appendChild(table);

    if (feasible.length >= 2) {
      section.appendChild(buildParetoScatter(feasible));
    }
    wrap.appendChild(section);
  });
  wrap.style.display = "";
}

// Minimal inline-SVG scatter of the feasible (hard=0) points for one pair — no chart library,
// matching this page's "vanilla JS, no build step" convention. Non-dominated points are solid and
// joined by the frontier line; dominated ones are hollow and grey, shown but never joined.
// Bypassed points duplicate a solved one, so they are not drawn a second time.
function buildParetoScatter(points) {
  const W = 420, H = 220, PAD = 36;
  const xs = points.map((p) => p.bound_value);
  const ys = points.map((p) => p.minimize_value);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMin = Math.min(...ys), yMax = Math.max(...ys);
  const sx = (v) => PAD + (xMax === xMin ? 0 : ((v - xMin) / (xMax - xMin)) * (W - 2 * PAD));
  const sy = (v) => H - PAD - (yMax === yMin ? 0 : ((v - yMin) / (yMax - yMin)) * (H - 2 * PAD));

  const dots = points
    .filter((p) => !p.skipped_by_bypass)
    .map((p) => p.dominated
      ? `<circle cx="${sx(p.bound_value)}" cy="${sy(p.minimize_value)}" r="4" fill="none" stroke="var(--muted, #999)" stroke-width="1.5"><title>dominated</title></circle>`
      : `<circle cx="${sx(p.bound_value)}" cy="${sy(p.minimize_value)}" r="4" fill="var(--accent-2, #f26d21)" />`)
    .join("");
  const sorted = points.filter((p) => !p.dominated).sort((a, b) => a.bound_value - b.bound_value);
  const line = sorted.map((p) => `${sx(p.bound_value)},${sy(p.minimize_value)}`).join(" ");

  const svg = document.createElement("div");
  svg.innerHTML = `
    <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Pareto frontier scatter plot">
      <line x1="${PAD}" y1="${H - PAD}" x2="${W - PAD}" y2="${H - PAD}" stroke="var(--line, #ccc)" />
      <line x1="${PAD}" y1="${PAD}" x2="${PAD}" y2="${H - PAD}" stroke="var(--line, #ccc)" />
      <polyline points="${line}" fill="none" stroke="var(--accent, #003877)" stroke-width="1.5" />
      ${dots}
    </svg>`;
  return svg;
}

// ---------------------------------------------------------------- nav (Auth, design.md §11)
async function loadNavUser() {
  try {
    const res = await fetch("/api/auth/me");
    if (!res.ok) return;   // the page route already redirects an unauthenticated visitor to
                            // /login server-side; this only personalizes the nav once loaded
    const me = await res.json();
    $("navName").textContent = me.name;
  } catch (e) {
    // nav personalization is cosmetic; ignore failures
  }
}

$("logoutBtn").addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" });
  window.location.href = "/login";
});

// ---------------------------------------------------------------- init
loadNavUser();
loadSeedDatasets();
loadBranches().then(checkReadiness);
loadHistory();
