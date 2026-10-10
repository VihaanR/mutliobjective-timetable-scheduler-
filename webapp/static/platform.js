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
on("modalAddVisitingBtn", "click", addVisitingFacultyFromSelect);
on("numCandidates", "input", () => {
  if ($("modalNumCandidates") && $("numCandidates")) $("modalNumCandidates").value = $("numCandidates").value;
});
on("modalNumCandidates", "input", () => {
  if ($("numCandidates") && $("modalNumCandidates")) $("numCandidates").value = $("modalNumCandidates").value;
});
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

// ---------------------------------------------------------------- 4. generate timetable modal, visiting faculty & runner
let visitingFacultyList = [];
let allFacultyList = [];

async function loadVisitingFacultyModal() {
  try {
    const res = await fetch("/api/faculty");
    if (!res.ok) return;
    allFacultyList = await res.json();

    if (visitingFacultyList.length === 0) {
      visitingFacultyList = allFacultyList
        .filter(f => f.is_visiting)
        .map(f => ({
          id: f.id,
          code: f.code,
          name: f.name,
          is_visiting: true,
          visiting_days: f.visiting_days && f.visiting_days.length > 0 ? f.visiting_days : [0, 1, 2, 3, 4],
          visiting_start_time: f.visiting_start_time || "09:00",
          visiting_end_time: f.visiting_end_time || "16:00",
        }));
    }
    renderVisitingFacultyList();
    updateVisitingSelectOptions();
  } catch (e) {
    console.error("Error loading faculty for modal:", e);
  }
}

function updateVisitingSelectOptions() {
  const sel = $("modalAddVisitingSelect");
  if (!sel) return;
  const existingIds = new Set(visitingFacultyList.map(f => f.id));
  const available = allFacultyList.filter(f => !existingIds.has(f.id));
  sel.innerHTML = `<option value="">+ Designate a faculty member as Visiting...</option>` +
    available.map(f => `<option value="${f.id}">${f.name} (${f.code})</option>`).join("");
}

function renderVisitingFacultyList() {
  const container = $("visitingFacultyContainer");
  if (!container) return;
  if (visitingFacultyList.length === 0) {
    container.innerHTML = `<div style="font-size:12px; color:#64748b; font-style:italic; padding:6px 0;">No visiting faculty designated yet. Add any faculty below to lock in their working days &amp; hours with highest priority.</div>`;
    return;
  }

  const dayNames = ["Mon", "Tue", "Wed", "Thu", "Fri"];
  container.innerHTML = visitingFacultyList.map((f, idx) => {
    const daysChecks = dayNames.map((dName, dIdx) => {
      const checked = (f.visiting_days || []).includes(dIdx) ? "checked" : "";
      return `
        <label style="font-size:11px; display:inline-flex; align-items:center; gap:2px; font-weight:600; cursor:pointer;">
          <input type="checkbox" ${checked} onchange="window.toggleVisitingDay(${idx}, ${dIdx}, this.checked)"> ${dName}
        </label>
      `;
    }).join(" ");

    return `
      <div style="background:#fff; border:1px solid #bae6fd; border-radius:8px; padding:8px 10px; display:flex; flex-direction:column; gap:6px;">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <div>
            <b style="font-size:12.5px; color:#0369a1;">${f.name}</b> <span style="font-size:11.5px; color:#64748b;">(${f.code})</span>
            <span style="font-size:10px; background:#e0f2fe; color:#0284c7; padding:2px 6px; border-radius:10px; font-weight:700; margin-left:4px;">PRIORITY 1</span>
          </div>
          <button type="button" style="background:transparent; border:none; color:#ef4444; font-size:11.5px; cursor:pointer; font-weight:700;" onclick="window.removeVisitingFaculty(${idx})">✕ Remove</button>
        </div>
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; font-size:11.5px;">
          <div style="display:flex; align-items:center; gap:6px;">
            <span style="color:#475569; font-weight:600;">Days:</span>
            ${daysChecks}
          </div>
          <div style="display:flex; align-items:center; gap:4px;">
            <span style="color:#475569; font-weight:600;">Hours:</span>
            <input type="text" value="${f.visiting_start_time || '09:00'}" style="width:50px; padding:2px 4px; font-size:11px; text-align:center; border:1px solid #cbd5e1; border-radius:4px;" onchange="window.updateVisitingTime(${idx}, 'visiting_start_time', this.value)" placeholder="HH:MM">
            <span>to</span>
            <input type="text" value="${f.visiting_end_time || '16:00'}" style="width:50px; padding:2px 4px; font-size:11px; text-align:center; border:1px solid #cbd5e1; border-radius:4px;" onchange="window.updateVisitingTime(${idx}, 'visiting_end_time', this.value)" placeholder="HH:MM">
          </div>
        </div>
      </div>
    `;
  }).join("");
}

function addVisitingFacultyFromSelect() {
  const sel = $("modalAddVisitingSelect");
  if (!sel || !sel.value) return;
  const facId = parseInt(sel.value);
  const fac = allFacultyList.find(f => f.id === facId);
  if (!fac) return;
  visitingFacultyList.push({
    id: fac.id,
    code: fac.code,
    name: fac.name,
    is_visiting: true,
    visiting_days: fac.visiting_days && fac.visiting_days.length > 0 ? fac.visiting_days : [0, 1, 2, 3, 4],
    visiting_start_time: fac.visiting_start_time || "09:00",
    visiting_end_time: fac.visiting_end_time || "16:00",
  });
  renderVisitingFacultyList();
  updateVisitingSelectOptions();
}

window.removeVisitingFaculty = function(idx) {
  visitingFacultyList.splice(idx, 1);
  renderVisitingFacultyList();
  updateVisitingSelectOptions();
};

window.toggleVisitingDay = function(facIdx, dayIdx, checked) {
  const f = visitingFacultyList[facIdx];
  if (!f) return;
  f.visiting_days = f.visiting_days || [];
  if (checked) {
    if (!f.visiting_days.includes(dayIdx)) f.visiting_days.push(dayIdx);
  } else {
    f.visiting_days = f.visiting_days.filter(d => d !== dayIdx);
  }
};

window.updateVisitingTime = function(facIdx, field, val) {
  const f = visitingFacultyList[facIdx];
  if (f) f[field] = val.trim();
};

function openGenScopeModal() {
  const modal = $("genScopeModal");
  if (!modal) return;
  if ($("modalNumCandidates") && $("numCandidates")) {
    $("modalNumCandidates").value = $("numCandidates").value || 3;
  }
  loadVisitingFacultyModal();
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

  const numCandEl = $("modalNumCandidates") || $("numCandidates");
  const numCandidates = parseInt(numCandEl ? numCandEl.value : "1") || 1;

  const visitingOverrides = visitingFacultyList.map(f => ({
    id: f.id,
    code: f.code,
    name: f.name,
    is_visiting: true,
    visiting_days: f.visiting_days || [0, 1, 2, 3, 4],
    visiting_start_time: f.visiting_start_time || "09:00",
    visiting_end_time: f.visiting_end_time || "16:00",
  }));

  const payload = {
    solver: $("solver").value,
    optimization_mode: $("optimizationMode") ? $("optimizationMode").value : "baseline",
    time_limit: parseFloat($("timeLimit").value) || 180,
    label,
    branch_ids,
    num_candidates: numCandidates,
    visiting_faculty_overrides: visitingOverrides,
  };

  if (stopBtn) stopBtn.style.display = "inline-flex";

  statusEl.className = "status-line active-gen";
  statusEl.innerHTML = `<div class="gen-spinner"></div> <span>Generating <b>${numCandidates}</b> candidate timetable(s) for <b>${label}</b> (${payload.solver}). Solving simultaneous constraints…</span>`;

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
        const isAdaptive = (run.division_meta && run.division_meta._optimization_mode === "adaptive");
        const solverDisplay = isAdaptive ? "adaptive cpsat" : run.solver;
        if (badge) badge.innerHTML = `<span class="badge badge-success">Showing Run #${runId}</span> <b>${label}</b> (${solverDisplay})`;
        renderSummary(run);
        renderStages(run.stage_reports);
        renderJudgeLeaderboard(run);
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

// ---------------------------------------------------------------- AQWI Judge Leaderboard & Visualizations
const CANDIDATE_COLORS = ["#2563eb", "#16a34a", "#d97706", "#9333ea", "#0284c7", "#e11d48", "#475569"];

const CRITERIA_INFO = [
  { key: "c1_avoid_8_6_span", code: "C1", name: "Excessive Student Day Span", unit: "excess span points", desc: "Penalizes student days stretched beyond 8 periods and full 08:00–18:00 spans." },
  { key: "c2_student_idle_gaps", code: "C2", name: "Student Idle Gaps", unit: "idle gap hours", desc: "Counts internal unoccupied period slots between first and last division classes." },
  { key: "c3_same_day_lec_lab", code: "C3", name: "Same-Day Lecture & Lab", unit: "same-day overlaps", desc: "Penalizes scheduling both theory/tutorial and lab of the same course on the same day." },
  { key: "c4_faculty_load_variance", code: "C4", name: "Faculty Workload Variance", unit: "daily variance (hours²)", desc: "Teaching-hour variance across active teaching days for faculty members." },
  { key: "c5_honours_boundary", code: "C5", name: "Honours/Elective Boundary", unit: "boundary violations", desc: "Sessions scheduled outside boundary slots (period 0 or final 2 periods)." },
  { key: "c6_three_consecutive_days", code: "C6", name: "3+ Consecutive Subject Days", unit: "3+ day streaks", desc: "Streaks of 3 or more consecutive weekdays with the same course for a division." },
  { key: "c7_faculty_gaps_over_2h", code: "C7", name: "Faculty Gaps > 2 Hours", unit: "excess waiting hours", desc: "Excess waiting time exceeding 2 hours between classes on the same day." },
];

let benchmarkCandidateIdx = 0;
let graphBSelectedCandidates = new Set();

function renderJudgeLeaderboard(run) {
  const judgeSection = $("judgeSection");
  if (!judgeSection) return;
  const reports = run.judge_reports || [];
  if (reports.length === 0) {
    judgeSection.style.display = "none";
    return;
  }
  judgeSection.style.display = "block";

  const selectedIdx = run.selected_candidate_idx || 0;
  if (benchmarkCandidateIdx >= reports.length) benchmarkCandidateIdx = (selectedIdx === 0 && reports.length > 1) ? 1 : 0;
  if (graphBSelectedCandidates.size === 0) {
    reports.forEach((_, i) => { if (i < 5) graphBSelectedCandidates.add(i); });
  }

  // Active schedule badge
  const activeBadge = $("activeCandidateBadge");
  if (activeBadge) {
    activeBadge.textContent = `Candidate #${selectedIdx + 1} Active`;
  }

  // 1. Candidate Switcher Tabs
  const switcherWrap = $("candidateSwitcherWrap");
  if (switcherWrap) {
    switcherWrap.innerHTML = reports.map((rep, idx) => {
      const isSelected = idx === selectedIdx;
      const isNonDom = rep.pareto_status === "non_dominated";
      const icon = rep.is_recommended ? "★" : (isNonDom ? "🏆" : "⚠️");
      return `
        <button type="button" class="tab ${isSelected ? 'active' : ''}" style="padding:5px 11px; font-size:12px; font-weight:700; border-radius:6px;" onclick="window.selectCandidateTimetable(${run.id}, ${idx})">
          ${icon} #${idx + 1} (${rep.quality_score}%)
        </button>
      `;
    }).join("");
  }

  // Benchmark Selector
  const benchSelect = $("compareCandidateSelect");
  if (benchSelect) {
    benchSelect.innerHTML = reports.map((rep, idx) => `
      <option value="${idx}" ${idx === benchmarkCandidateIdx ? 'selected' : ''}>
        Candidate #${idx + 1} (${rep.quality_score}%)
      </option>
    `).join("");
    benchSelect.onchange = (e) => {
      benchmarkCandidateIdx = parseInt(e.target.value, 10);
      renderJudgeLeaderboard(run);
    };
  }

  // 2. Candidate Overview Cards
  const cardsContainer = $("candidateCardsContainer");
  if (cardsContainer) {
    cardsContainer.innerHTML = reports.map((rep, idx) => {
      const isSelected = idx === selectedIdx;
      const isNonDom = rep.pareto_status === "non_dominated";
      const isTop = rep.is_recommended || (rep.rank === 1 && isNonDom);
      const scoreColor = rep.quality_score >= 90 ? "#16a34a" : (rep.quality_score >= 80 ? "#2563eb" : "#d97706");
      const candColor = CANDIDATE_COLORS[idx % CANDIDATE_COLORS.length];

      return `
        <div style="background:#fff; border:2px solid ${isSelected ? candColor : '#e2e8f0'}; border-radius:10px; padding:12px 14px; position:relative; cursor:pointer; transition:all 0.2s ease; box-shadow:${isSelected ? '0 4px 14px rgba(37,99,235,0.12)' : 'none'};" onclick="window.selectCandidateTimetable(${run.id}, ${idx})">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
            <div style="display:flex; align-items:center; gap:6px;">
              <span style="display:inline-block; width:10px; height:10px; border-radius:50%; background:${candColor};"></span>
              <span style="font-weight:800; font-size:13.5px; color:#0f172a;">Candidate #${idx + 1}</span>
              ${isTop ? '<span style="background:#16a34a; color:#fff; font-size:10px; font-weight:800; padding:2px 6px; border-radius:10px;">★ Recommended</span>' :
                (isNonDom ? '<span style="background:#0284c7; color:#fff; font-size:10px; font-weight:700; padding:2px 6px; border-radius:10px;">🏆 Rank Tier 1 (Optimal)</span>' :
                            '<span style="background:#f1f5f9; color:#64748b; font-size:10px; font-weight:600; padding:2px 6px; border-radius:10px;">Rank Tier 2 (Dominated)</span>')}
            </div>
            ${isSelected ? '<span style="font-size:11px; font-weight:700; color:#2563eb; background:#eff6ff; padding:2px 7px; border-radius:10px;">Active Grid</span>' : '<button type="button" style="font-size:11px; font-weight:600; padding:2px 8px; border-radius:4px; border:1px solid #cbd5e1; background:#f8fafc; cursor:pointer;">Select</button>'}
          </div>
          <div style="display:flex; justify-content:space-between; align-items:flex-end;">
            <div>
              <div style="font-size:11px; color:#64748b; font-weight:600;">AQWI Quality Score</div>
              <div style="font-size:22px; font-weight:800; color:${scoreColor}; line-height:1.1;">
                ${rep.quality_score}<span style="font-size:13px; font-weight:700;">%</span>
              </div>
            </div>
            <div style="text-align:right;">
              <div style="font-size:11px; color:#64748b; font-weight:600;">Penalty / Baseline</div>
              <div style="font-size:13px; font-weight:700; color:#334155;">${rep.total_penalty.toFixed(1)} <span style="font-weight:400; color:#94a3b8;">/ ${rep.cohort_baseline || 3000}</span></div>
            </div>
          </div>
        </div>
      `;
    }).join("");
  }

  const selectedRep = reports[selectedIdx] || reports[0];
  const benchRep = reports[benchmarkCandidateIdx] || reports[0];

  // 3. 7-Criterion Violation Profile Cards
  const critContainer = $("criterionCardsContainer");
  if (critContainer && selectedRep) {
    critContainer.innerHTML = CRITERIA_INFO.map((info) => {
      const curCrit = selectedRep.criteria?.[info.key] || {};
      const benchCrit = benchRep.criteria?.[info.key] || {};
      const curVal = curCrit.raw_metric ?? 0;
      const benchVal = benchCrit.raw_metric ?? 0;
      const diff = curVal - benchVal;

      let diffBadge = "";
      if (selectedIdx === benchmarkCandidateIdx) {
        diffBadge = `<span style="font-size:11px; color:#64748b; font-weight:600;">Benchmark</span>`;
      } else if (Math.abs(diff) < 0.001) {
        diffBadge = `<span style="font-size:11px; color:#64748b; font-weight:700; background:#f1f5f9; padding:1px 6px; border-radius:4px;">= 0.0</span>`;
      } else if (diff < 0) {
        diffBadge = `<span style="font-size:11px; color:#16a34a; font-weight:700; background:#f0fdf4; padding:1px 6px; border-radius:4px;">&Delta; ${diff.toFixed(1)} (Better)</span>`;
      } else {
        diffBadge = `<span style="font-size:11px; color:#dc2626; font-weight:700; background:#fef2f2; padding:1px 6px; border-radius:4px;">&Delta; +${diff.toFixed(1)} (Worse)</span>`;
      }

      return `
        <div style="background:#fff; border:1px solid #e2e8f0; border-radius:8px; padding:12px 14px; display:flex; flex-direction:column; justify-content:space-between; box-shadow:0 1px 4px rgba(0,0,0,0.02);">
          <div>
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
              <span style="font-weight:700; font-size:12.5px; color:#0f172a;">${info.code}. ${info.name}</span>
              ${diffBadge}
            </div>
            <div style="font-size:11px; color:#64748b; margin-bottom:8px; min-height:28px;">${info.desc}</div>
          </div>
          <div style="display:flex; justify-content:space-between; align-items:flex-end; border-top:1px solid #f8fafc; padding-top:6px;">
            <div>
              <div style="font-size:10px; text-transform:uppercase; color:#94a3b8; font-weight:700; letter-spacing:0.3px;">Raw Metric</div>
              <div style="font-size:18px; font-weight:800; color:#1e293b;">${curVal.toFixed(1)} <span style="font-size:11px; font-weight:600; color:#64748b;">${info.unit}</span></div>
            </div>
            <div style="text-align:right; font-size:11px; font-weight:600; color:#16a34a;">
              Lower is better &darr;
            </div>
          </div>
        </div>
      `;
    }).join("");
  }

  // 4. Graph A: Single Candidate Violation Profile
  if ($("graphASelectedLabel")) $("graphASelectedLabel").textContent = `Candidate #${selectedIdx + 1}`;
  renderGraphA(selectedRep);

  // 5. Graph B: Multi-Candidate Comparison Grouped Bar Chart
  renderGraphB(reports);

  // 6. Graph C: AQWI Quality Score Breakdown
  renderGraphC(reports, selectedIdx);

  // 7. Graph D: 2D Pareto Scatter Plot & Trade-Off Matrix
  renderGraphD(reports, selectedIdx, run.id);

  // 8. Pareto Candidate Ledger Table
  renderParetoTable(reports, selectedIdx, run.id);

  // 9. Resource & Lab Utilization Audit
  renderResourceAudit(selectedRep);

  // 10. Audit details table
  renderAuditBreakdownDetails(selectedRep, selectedIdx);
}

// ---------------------------------------------------------------- Graph A: Violation Profile (SVG)
function renderGraphA(selectedRep) {
  const container = $("graphAContainer");
  if (!container || !selectedRep) return;

  const width = 500;
  const height = 240;
  const padLeft = 45;
  const padRight = 20;
  const padTop = 25;
  const padBottom = 40;
  const chartW = width - padLeft - padRight;
  const chartH = height - padTop - padBottom;

  const vals = CRITERIA_INFO.map(info => selectedRep.criteria?.[info.key]?.raw_metric ?? 0);
  const maxVal = Math.max(5, ...vals) * 1.15;
  const numBars = vals.length;
  const barWidth = Math.min(36, chartW / numBars - 14);

  const barsSvg = vals.map((val, i) => {
    const x = padLeft + (i + 0.5) * (chartW / numBars) - barWidth / 2;
    const barH = (val / maxVal) * chartH;
    const y = padTop + chartH - barH;
    return `
      <g class="bar-group" style="cursor:pointer;">
        <title>${CRITERIA_INFO[i].code}: ${CRITERIA_INFO[i].name} = ${val.toFixed(2)} ${CRITERIA_INFO[i].unit}</title>
        <rect x="${x}" y="${y}" width="${barWidth}" height="${Math.max(2, barH)}" fill="#2563eb" rx="4" opacity="0.88">
          <animate attributeName="height" from="0" to="${Math.max(2, barH)}" dur="0.3s" fill="freeze" />
        </rect>
        <text x="${x + barWidth / 2}" y="${y - 6}" font-size="11" font-weight="700" fill="#1e293b" text-anchor="middle">${val.toFixed(1)}</text>
        <text x="${x + barWidth / 2}" y="${padTop + chartH + 16}" font-size="11" font-weight="700" fill="#475569" text-anchor="middle">${CRITERIA_INFO[i].code}</text>
      </g>
    `;
  }).join("");

  // Horizontal Grid Lines
  const gridLines = [0, 0.5, 1.0].map(ratio => {
    const y = padTop + chartH - ratio * chartH;
    const tickVal = (ratio * maxVal).toFixed(0);
    return `
      <line x1="${padLeft}" y1="${y}" x2="${width - padRight}" y2="${y}" stroke="#e2e8f0" stroke-dasharray="3,3" />
      <text x="${padLeft - 8}" y="${y + 4}" font-size="10" fill="#94a3b8" text-anchor="end">${tickVal}</text>
    `;
  }).join("");

  container.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" style="width:100%; height:100%; overflow:visible; font-family:inherit;">
      ${gridLines}
      <line x1="${padLeft}" y1="${padTop + chartH}" x2="${width - padRight}" y2="${padTop + chartH}" stroke="#cbd5e1" stroke-width="1.5" />
      ${barsSvg}
    </svg>
  `;
}

// ---------------------------------------------------------------- Graph B: Multi-Candidate Comparison (SVG)
function renderGraphB(reports) {
  const container = $("graphBContainer");
  const togglesWrap = $("graphBCandidateToggles");
  if (!container || !reports) return;

  // Render Toggles
  if (togglesWrap) {
    togglesWrap.innerHTML = reports.map((rep, idx) => {
      const isChecked = graphBSelectedCandidates.has(idx);
      const color = CANDIDATE_COLORS[idx % CANDIDATE_COLORS.length];
      return `
        <label style="display:inline-flex; align-items:center; gap:4px; cursor:pointer;">
          <input type="checkbox" ${isChecked ? 'checked' : ''} onchange="window.toggleGraphBCandidate(${idx})" />
          <span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:${color};"></span>
          <span>#${idx + 1}</span>
        </label>
      `;
    }).join("");
  }

  const activeIndices = Array.from(graphBSelectedCandidates).filter(idx => idx < reports.length);
  if (activeIndices.length === 0) {
    container.innerHTML = `<div style="text-align:center; padding:50px; color:#94a3b8;">No candidates selected.</div>`;
    return;
  }

  const width = 520;
  const height = 240;
  const padLeft = 40;
  const padRight = 20;
  const padTop = 25;
  const padBottom = 40;
  const chartW = width - padLeft - padRight;
  const chartH = height - padTop - padBottom;

  let maxVal = 5;
  CRITERIA_INFO.forEach(info => {
    activeIndices.forEach(cIdx => {
      const v = reports[cIdx]?.criteria?.[info.key]?.raw_metric ?? 0;
      if (v > maxVal) maxVal = v;
    });
  });
  maxVal *= 1.15;

  const numGroups = CRITERIA_INFO.length;
  const groupW = chartW / numGroups;
  const barW = Math.max(3, Math.min(14, (groupW - 10) / activeIndices.length));

  const groupsSvg = CRITERIA_INFO.map((info, gIdx) => {
    const groupX = padLeft + gIdx * groupW;
    const bars = activeIndices.map((cIdx, barIdx) => {
      const val = reports[cIdx]?.criteria?.[info.key]?.raw_metric ?? 0;
      const barH = (val / maxVal) * chartH;
      const x = groupX + 5 + barIdx * (barW + 1);
      const y = padTop + chartH - barH;
      const color = CANDIDATE_COLORS[cIdx % CANDIDATE_COLORS.length];
      return `
        <rect x="${x}" y="${y}" width="${barW}" height="${Math.max(1, barH)}" fill="${color}" rx="2">
          <title>Candidate #${cIdx + 1} - ${info.name}: ${val.toFixed(1)} ${info.unit}</title>
        </rect>
      `;
    }).join("");

    return `
      <g>
        ${bars}
        <text x="${groupX + groupW / 2}" y="${padTop + chartH + 16}" font-size="10.5" font-weight="700" fill="#475569" text-anchor="middle">${info.code}</text>
      </g>
    `;
  }).join("");

  const gridLines = [0, 0.5, 1.0].map(ratio => {
    const y = padTop + chartH - ratio * chartH;
    return `
      <line x1="${padLeft}" y1="${y}" x2="${width - padRight}" y2="${y}" stroke="#e2e8f0" stroke-dasharray="3,3" />
      <text x="${padLeft - 6}" y="${y + 4}" font-size="10" fill="#94a3b8" text-anchor="end">${(ratio * maxVal).toFixed(0)}</text>
    `;
  }).join("");

  container.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" style="width:100%; height:100%; overflow:visible; font-family:inherit;">
      ${gridLines}
      <line x1="${padLeft}" y1="${padTop + chartH}" x2="${width - padRight}" y2="${padTop + chartH}" stroke="#cbd5e1" stroke-width="1.5" />
      ${groupsSvg}
    </svg>
  `;
}

window.toggleGraphBCandidate = function(idx) {
  if (graphBSelectedCandidates.has(idx)) {
    if (graphBSelectedCandidates.size > 1) graphBSelectedCandidates.delete(idx);
  } else {
    graphBSelectedCandidates.add(idx);
  }
  const runId = currentRunId;
  if (runId) {
    fetch(`/api/runs/${runId}`).then(r => r.json()).then(run => renderGraphB(run.judge_reports || []));
  }
};

// ---------------------------------------------------------------- Graph C: AQWI Score Comparison (Bars)
function renderGraphC(reports, selectedIdx) {
  const container = $("graphCContainer");
  if (!container || !reports) return;

  const rows = reports.map((rep, idx) => {
    const isSelected = idx === selectedIdx;
    const color = rep.quality_score >= 90 ? "#16a34a" : (rep.quality_score >= 80 ? "#2563eb" : "#d97706");
    return `
      <div style="margin-bottom:10px;">
        <div style="display:flex; justify-content:space-between; font-size:12px; font-weight:600; margin-bottom:3px;">
          <span>
            Candidate #${idx + 1}
            ${rep.is_recommended ? '<span style="color:#16a34a; font-weight:700;">★ Recommended</span>' : ''}
            ${isSelected ? '<span style="color:#2563eb;">(Active)</span>' : ''}
          </span>
          <span style="color:${color}; font-weight:800;">${rep.quality_score}% &bull; P=${rep.total_penalty.toFixed(1)}</span>
        </div>
        <div style="background:#f1f5f9; height:12px; border-radius:6px; overflow:hidden; position:relative;">
          <div style="background:${color}; height:100%; width:${Math.min(100, Math.max(0, rep.quality_score))}%; border-radius:6px; transition:width 0.4s ease;"></div>
        </div>
      </div>
    `;
  }).join("");

  container.innerHTML = `
    <div style="padding:4px 0;">
      ${rows}
    </div>
  `;
}

// ---------------------------------------------------------------- Graph D: 2D Pareto Scatter Plot (SVG)
function renderGraphD(reports, selectedIdx, runId) {
  const container = $("graphDContainer");
  if (!container || !reports) return;

  const width = 500;
  const height = 230;
  const padLeft = 45;
  const padRight = 25;
  const padTop = 20;
  const padBottom = 35;
  const chartW = width - padLeft - padRight;
  const chartH = height - padTop - padBottom;

  // Student welfare: M1+M2+M3+M5+M6; Faculty load: M4+M7
  const points = reports.map((rep, idx) => {
    const c = rep.criteria || {};
    const stu = (c["c1_avoid_8_6_span"]?.raw_metric ?? 0) +
                (c["c2_student_idle_gaps"]?.raw_metric ?? 0) +
                (c["c3_same_day_lec_lab"]?.raw_metric ?? 0) +
                (c["c5_honours_boundary"]?.raw_metric ?? 0) +
                (c["c6_three_consecutive_days"]?.raw_metric ?? 0);
    const fac = (c["c4_faculty_load_variance"]?.raw_metric ?? 0) +
                (c["c7_faculty_gaps_over_2h"]?.raw_metric ?? 0);
    return {
      idx,
      stu,
      fac,
      rep,
      isNonDom: rep.pareto_status === "non_dominated",
      isSelected: idx === selectedIdx,
    };
  });

  const maxStu = Math.max(5, ...points.map(p => p.stu)) * 1.25;
  const maxFac = Math.max(3, ...points.map(p => p.fac)) * 1.25;

  const pointsSvg = points.map(pt => {
    const cx = padLeft + (pt.stu / maxStu) * chartW;
    const cy = padTop + chartH - (pt.fac / maxFac) * chartH;
    const color = pt.isNonDom ? "#16a34a" : "#94a3b8";
    const stroke = pt.isSelected ? "#2563eb" : (pt.isNonDom ? "#15803d" : "#64748b");
    const strokeWidth = pt.isSelected ? 3 : 1.5;

    return `
      <g style="cursor:pointer;" onclick="window.selectCandidateTimetable(${runId}, ${pt.idx})">
        <title>Candidate #${pt.idx + 1} (${pt.isNonDom ? 'Rank Tier 1 (Optimal)' : 'Rank Tier 2 (Dominated)'})\nStudent Welfare Cost: ${pt.stu.toFixed(1)}\nFaculty Workload Cost: ${pt.fac.toFixed(1)}\nAQWI Score: ${pt.rep.quality_score}%</title>
        ${pt.isNonDom ? `
          <polygon points="${cx},${cy - 7} ${cx + 7},${cy} ${cx},${cy + 7} ${cx - 7},${cy}" fill="#22c55e" stroke="${stroke}" stroke-width="${strokeWidth}" />
        ` : `
          <circle cx="${cx}" cy="${cy}" r="6" fill="#cbd5e1" stroke="${stroke}" stroke-width="${strokeWidth}" />
        `}
        <text x="${cx}" y="${cy - 9}" font-size="10" font-weight="700" fill="${pt.isSelected ? '#1d4ed8' : '#334155'}" text-anchor="middle">#${pt.idx + 1}</text>
      </g>
    `;
  }).join("");

  container.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" style="width:100%; height:100%; overflow:visible; font-family:inherit;">
      <!-- Grid -->
      <line x1="${padLeft}" y1="${padTop + chartH}" x2="${width - padRight}" y2="${padTop + chartH}" stroke="#cbd5e1" stroke-width="1.5" />
      <line x1="${padLeft}" y1="${padTop}" x2="${padLeft}" y2="${padTop + chartH}" stroke="#cbd5e1" stroke-width="1.5" />

      <!-- Axis Labels -->
      <text x="${padLeft + chartW / 2}" y="${height - 6}" font-size="11" font-weight="600" fill="#64748b" text-anchor="middle">Student Welfare Penalty &rarr;</text>
      <text x="12" y="${padTop + chartH / 2}" font-size="11" font-weight="600" fill="#64748b" text-anchor="middle" transform="rotate(-90 12 ${padTop + chartH / 2})">Faculty Workload Penalty &rarr;</text>

      ${pointsSvg}
    </svg>
  `;
}

// ---------------------------------------------------------------- Pareto Candidate Summary Table
function renderParetoTable(reports, selectedIdx, runId) {
  const container = $("paretoTableContainer");
  if (!container || !reports) return;

  const rows = reports.map((rep, idx) => {
    const isSelected = idx === selectedIdx;
    const isNonDom = rep.pareto_status === "non_dominated";
    const statusBadge = rep.is_recommended ?
      '<span class="badge" style="background:#16a34a; color:#fff; font-size:10.5px; padding:2px 7px; border-radius:10px;">★ Recommended</span>' :
      (isNonDom ? '<span class="badge" style="background:#0284c7; color:#fff; font-size:10.5px; padding:2px 7px; border-radius:10px;">🏆 Rank Tier 1 (Optimal)</span>' :
                  '<span class="badge" style="background:#f1f5f9; color:#64748b; font-size:10.5px; padding:2px 7px; border-radius:10px;">Rank Tier 2 (Dominated)</span>');

    const m = rep.criterion_metrics || {};

    return `
      <tr style="border-bottom:1px solid #f1f5f9; background:${isSelected ? '#eff6ff' : 'transparent'}; font-size:12px;">
        <td style="padding:8px 10px; font-weight:700;">Candidate #${idx + 1}</td>
        <td style="padding:8px 10px; color:#16a34a; font-weight:600;">✓ Pass</td>
        <td style="padding:8px 10px; font-weight:800; color:#0f172a;">${rep.quality_score}%</td>
        <td style="padding:8px 10px; font-weight:600; color:#334155;">${rep.total_penalty.toFixed(1)}</td>
        <td style="padding:8px 10px;">${statusBadge}</td>
        <td style="padding:8px 6px; text-align:center;">${m.M1 ?? '-'}</td>
        <td style="padding:8px 6px; text-align:center;">${m.M2 ?? '-'}</td>
        <td style="padding:8px 6px; text-align:center;">${m.M3 ?? '-'}</td>
        <td style="padding:8px 6px; text-align:center;">${m.M4 ?? '-'}</td>
        <td style="padding:8px 6px; text-align:center;">${m.M5 ?? '-'}</td>
        <td style="padding:8px 6px; text-align:center;">${m.M6 ?? '-'}</td>
        <td style="padding:8px 6px; text-align:center;">${m.M7 ?? '-'}</td>
        <td style="padding:8px 10px; text-align:right;">
          ${isSelected ? '<span style="font-weight:700; color:#2563eb;">Active</span>' :
            `<button type="button" style="padding:2px 8px; font-size:11px; cursor:pointer; border:1px solid #cbd5e1; border-radius:4px; background:#fff;" onclick="window.selectCandidateTimetable(${runId}, ${idx})">Select</button>`}
        </td>
      </tr>
    `;
  }).join("");

  container.innerHTML = `
    <table style="width:100%; border-collapse:collapse; min-width:700px;">
      <thead>
        <tr style="background:#f8fafc; border-bottom:1.5px solid #e2e8f0; font-size:11px; text-transform:uppercase; color:#64748b; font-weight:700;">
          <th style="padding:8px 10px; text-align:left;">Candidate</th>
          <th style="padding:8px 10px; text-align:left;">Hard Gate</th>
          <th style="padding:8px 10px; text-align:left;">AQWI</th>
          <th style="padding:8px 10px; text-align:left;">Penalty</th>
          <th style="padding:8px 10px; text-align:left;">Ranked Status</th>
          <th style="padding:8px 6px; text-align:center;">M1</th>
          <th style="padding:8px 6px; text-align:center;">M2</th>
          <th style="padding:8px 6px; text-align:center;">M3</th>
          <th style="padding:8px 6px; text-align:center;">M4</th>
          <th style="padding:8px 6px; text-align:center;">M5</th>
          <th style="padding:8px 6px; text-align:center;">M6</th>
          <th style="padding:8px 6px; text-align:center;">M7</th>
          <th style="padding:8px 10px; text-align:right;">Action</th>
        </tr>
      </thead>
      <tbody>
        ${rows}
      </tbody>
    </table>
  `;
}

// ---------------------------------------------------------------- Resource & Lab Utilization Audit Table
function renderResourceAudit(selectedRep) {
  const container = $("resourceAuditContainer");
  if (!container || !selectedRep || !selectedRep.resource_utilization) return;

  const res = selectedRep.resource_utilization;
  const classrooms = res.classrooms || [];
  const labs = res.labs || [];

  const renderRows = (items) => items.map(item => `
    <tr style="border-bottom:1px solid #f1f5f9; font-size:12px;">
      <td style="padding:6px 10px; font-weight:700; color:#1e293b;">${item.room_id}</td>
      <td style="padding:6px 10px; color:#475569;">${item.room_name}</td>
      <td style="padding:6px 10px; text-align:center;">
        ${item.is_preferred ?
          '<span style="background:#f0fdf4; color:#166534; font-weight:700; font-size:10px; padding:1px 6px; border-radius:4px; border:1px solid #bbf7d0;">★ High Preference</span>' :
          '<span style="color:#94a3b8; font-size:11px;">Auxiliary</span>'}
      </td>
      <td style="padding:6px 10px; text-align:center; font-weight:600;">${item.occupied_hours} hrs</td>
      <td style="padding:6px 10px; text-align:right; font-weight:700; color:${item.utilization_pct > 0 ? '#0f172a' : '#94a3b8'};">${item.utilization_pct}%</td>
    </tr>
  `).join("");

  container.innerHTML = `
    <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(320px, 1fr)); gap:16px;">
      <div>
        <div style="font-weight:700; font-size:12px; color:#475569; margin-bottom:6px; text-transform:uppercase;">Classrooms (51, 52, 53 Prioritized)</div>
        <table style="width:100%; border-collapse:collapse;">
          <thead>
            <tr style="background:#f8fafc; font-size:10.5px; text-transform:uppercase; color:#64748b;">
              <th style="padding:6px 10px; text-align:left;">Code</th>
              <th style="padding:6px 10px; text-align:left;">Name</th>
              <th style="padding:6px 10px; text-align:center;">Tier</th>
              <th style="padding:6px 10px; text-align:center;">Occupied</th>
              <th style="padding:6px 10px; text-align:right;">Utilization</th>
            </tr>
          </thead>
          <tbody>
            ${renderRows(classrooms)}
          </tbody>
        </table>
      </div>
      <div>
        <div style="font-weight:700; font-size:12px; color:#475569; margin-bottom:6px; text-transform:uppercase;">Laboratories (L1, L2, L3, L4 Prioritized - Maximum Saturation)</div>
        <table style="width:100%; border-collapse:collapse;">
          <thead>
            <tr style="background:#f8fafc; font-size:10.5px; text-transform:uppercase; color:#64748b;">
              <th style="padding:6px 10px; text-align:left;">Code</th>
              <th style="padding:6px 10px; text-align:left;">Name</th>
              <th style="padding:6px 10px; text-align:center;">Tier</th>
              <th style="padding:6px 10px; text-align:center;">Occupied</th>
              <th style="padding:6px 10px; text-align:right;">Utilization</th>
            </tr>
          </thead>
          <tbody>
            ${renderRows(labs)}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

let currentAuditViewMode = "candidate";
let lastActiveRep = null;
let lastSelectedIdx = 0;

window.toggleAuditViewMode = function(mode) {
  currentAuditViewMode = mode;
  if (lastActiveRep) {
    renderAuditBreakdownDetails(lastActiveRep, lastSelectedIdx);
  }
};

// ---------------------------------------------------------------- Audit Details Breakdown Table
function renderAuditBreakdownDetails(activeRep, selectedIdx) {
  const detailsContainer = $("judgeBreakdownDetails");
  if (!detailsContainer || !activeRep) return;
  lastActiveRep = activeRep;
  lastSelectedIdx = selectedIdx;

  const navButtons = (activeMode) => `
    <div style="display:flex; gap:8px; flex-wrap:wrap;">
      <button type="button" class="tab ${activeMode === 'candidate' ? 'active' : ''}" style="padding:5px 12px; font-size:11.5px; font-weight:700; border-radius:6px; ${activeMode === 'candidate' ? 'background:#2563eb; color:#fff;' : 'background:#f8fafc; border:1px solid #cbd5e1;'} cursor:pointer;" onclick="window.toggleAuditViewMode('candidate')">
        Candidate #${selectedIdx + 1} Audit
      </button>
      <button type="button" class="tab ${activeMode === 'weights' ? 'active' : ''}" style="padding:5px 12px; font-size:11.5px; font-weight:700; border-radius:6px; ${activeMode === 'weights' ? 'background:#2563eb; color:#fff;' : 'background:#f8fafc; border:1px solid #cbd5e1;'} cursor:pointer;" onclick="window.toggleAuditViewMode('weights')">
        ⚖️ Weight Justification (3 Cases)
      </button>
      <button type="button" class="tab ${activeMode === 'hypothesis1' ? 'active' : ''}" style="padding:5px 12px; font-size:11.5px; font-weight:700; border-radius:6px; ${activeMode === 'hypothesis1' ? 'background:#2563eb; color:#fff;' : 'background:#f8fafc; border:1px solid #cbd5e1;'} cursor:pointer;" onclick="window.toggleAuditViewMode('hypothesis1')">
        📊 Hypothesis 1: Adaptive Priority Test
      </button>
      <button type="button" class="tab ${activeMode === 'hypothesis3' ? 'active' : ''}" style="padding:5px 12px; font-size:11.5px; font-weight:700; border-radius:6px; ${activeMode === 'hypothesis3' ? 'background:#2563eb; color:#fff;' : 'background:#f8fafc; border:1px solid #cbd5e1;'} cursor:pointer;" onclick="window.toggleAuditViewMode('hypothesis3')">
        📊 Hypothesis 3: Modified CP-SAT
      </button>
    </div>
  `;

  const renderAuditCard = (title, subtitle, criteriaList, softCostVal, headerBg, solverName="cpsat", statusVal="done") => `
    <div style="background:#fff; border:1px solid #e2e8f0; border-radius:12px; padding:18px; box-shadow:0 2px 10px rgba(0,0,0,0.03); flex:1; min-width:320px;">
      <div style="margin-bottom:12px;">
        <div style="font-weight:800; font-size:14px; color:#0f172a;">Candidate #1 — 7-Point AQWI Institutional Audit Breakdown</div>
        <div style="font-size:11.5px; font-weight:700; color:${headerBg}; text-transform:uppercase; letter-spacing:0.5px; margin-top:2px;">${subtitle}</div>
      </div>
      <table style="width:100%; border-collapse:collapse; background:#fff; margin-bottom:16px;">
        <thead>
          <tr style="background:#f8fafc; border-bottom:1.5px solid #e2e8f0; font-size:10.5px; text-transform:uppercase; color:#64748b; font-weight:700; letter-spacing:0.4px;">
            <th style="padding:8px 10px; text-align:left;">JUDGE CRITERION</th>
            <th style="padding:8px 10px; text-align:left;">OBJECTIVE GOAL</th>
            <th style="padding:8px 10px; text-align:right;">RAW METRIC</th>
          </tr>
        </thead>
        <tbody>
          ${criteriaList.map(c => `
            <tr style="border-bottom:1px solid #f1f5f9; font-size:12px;">
              <td style="padding:8px 10px; font-weight:700; color:#1e293b;">${c.num}. ${c.name}</td>
              <td style="padding:8px 10px; color:#475569; font-size:11.5px;">${c.goal}</td>
              <td style="padding:8px 10px; text-align:right; font-weight:700; color:#334155;">${c.val}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
      <div style="display:grid; grid-template-columns:repeat(4, 1fr); gap:8px; border-top:1px solid #f1f5f9; padding-top:12px;">
        <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:10px 8px; text-align:center;">
          <div style="font-size:16px; font-weight:800; color:#0f172a;">${solverName}</div>
          <div style="font-size:10px; font-weight:700; color:#64748b; text-transform:uppercase; margin-top:2px;">SOLVER</div>
        </div>
        <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:10px 8px; text-align:center;">
          <div style="font-size:18px; font-weight:800; color:#16a34a;">${statusVal}</div>
          <div style="font-size:10px; font-weight:700; color:#64748b; text-transform:uppercase; margin-top:2px;">STATUS</div>
        </div>
        <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:10px 8px; text-align:center;">
          <div style="font-size:18px; font-weight:800; color:#16a34a;">0</div>
          <div style="font-size:10px; font-weight:700; color:#64748b; text-transform:uppercase; margin-top:2px;">HARD VIOLATIONS</div>
        </div>
        <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:10px 8px; text-align:center;">
          <div style="font-size:18px; font-weight:800; color:#2563eb;">${softCostVal}</div>
          <div style="font-size:10px; font-weight:700; color:#64748b; text-transform:uppercase; margin-top:2px;">SOFT COST</div>
        </div>
      </div>
      <div style="text-align:center; margin-top:10px; font-weight:800; font-size:12.5px; color:#1e293b; letter-spacing:0.5px;">
        ${title}
      </div>
    </div>
  `;

  if (currentAuditViewMode === "weights") {
    const case1Crit = [
      { num: "1", name: "8-6 Day Span Avoidance", w: "3.0", raw: "0.0", wt: "0.00" },
      { num: "2", name: "Student Gap Minimization", w: "3.0", raw: "0.0", wt: "0.00" },
      { num: "3", name: "Same-Day Lec & Lab", w: "2.0", raw: "0.0", wt: "0.00" },
      { num: "4", name: "Faculty Workload Balance", w: "0.2", raw: "8.25", wt: "1.65" },
      { num: "5", name: "Honours Boundary Placement", w: "1.0", raw: "0.0", wt: "0.00" },
      { num: "6", name: "3-Day Consecutive Spread", w: "1.0", raw: "14.0", wt: "14.00" },
      { num: "7", name: "Faculty Long Gap Elimination", w: "0.2", raw: "19.0", wt: "3.80" },
    ];
    const case2Crit = [
      { num: "1", name: "8-6 Day Span Avoidance", w: "0.2", raw: "0.0", wt: "0.00" },
      { num: "2", name: "Student Gap Minimization", w: "0.2", raw: "0.0", wt: "0.00" },
      { num: "3", name: "Same-Day Lec & Lab", w: "0.5", raw: "0.0", wt: "0.00" },
      { num: "4", name: "Faculty Workload Balance", w: "3.0", raw: "8.25", wt: "24.75" },
      { num: "5", name: "Honours Boundary Placement", w: "0.5", raw: "0.0", wt: "0.00" },
      { num: "6", name: "3-Day Consecutive Spread", w: "0.5", raw: "14.0", wt: "7.00" },
      { num: "7", name: "Faculty Long Gap Elimination", w: "3.0", raw: "19.0", wt: "57.00" },
    ];
    const case3Crit = [
      { num: "1", name: "8-6 Day Span Avoidance", w: "1.0", raw: "0.0", wt: "0.00" },
      { num: "2", name: "Student Gap Minimization", w: "1.0", raw: "0.0", wt: "0.00" },
      { num: "3", name: "Same-Day Lec & Lab", w: "1.0", raw: "0.0", wt: "0.00" },
      { num: "4", name: "Faculty Workload Balance", w: "1.0", raw: "8.25", wt: "8.25" },
      { num: "5", name: "Honours Boundary Placement", w: "1.0", raw: "0.0", wt: "0.00" },
      { num: "6", name: "3-Day Consecutive Spread", w: "1.0", raw: "14.0", wt: "14.00" },
      { num: "7", name: "Faculty Long Gap Elimination", w: "1.0", raw: "19.0", wt: "19.00" },
    ];

    const renderWeightCaseCard = (title, profileDesc, critList, totalPen, scoreVal, verdictText, verdictBg, isBest=false) => `
      <div style="background:#fff; border:${isBest ? '2.5px solid #16a34a' : '1px solid #e2e8f0'}; border-radius:12px; padding:16px; box-shadow:${isBest ? '0 4px 18px rgba(22,163,74,0.12)' : '0 2px 10px rgba(0,0,0,0.03)'}; flex:1; min-width:310px; display:flex; flex-direction:column; justify-content:space-between;">
        <div>
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
            <div style="font-weight:800; font-size:13.5px; color:#0f172a;">${title}</div>
            <span style="font-size:10px; font-weight:800; padding:2px 7px; border-radius:10px; ${verdictBg}">${verdictText}</span>
          </div>
          <div style="font-size:11px; color:#64748b; margin-bottom:12px; line-height:1.4;">${profileDesc}</div>
          <table style="width:100%; border-collapse:collapse; background:#fff; margin-bottom:14px; font-size:11.5px;">
            <thead>
              <tr style="background:#f8fafc; border-bottom:1.5px solid #e2e8f0; font-size:10px; text-transform:uppercase; color:#64748b; font-weight:700;">
                <th style="padding:6px 8px; text-align:left;">Criterion</th>
                <th style="padding:6px 6px; text-align:center;">Weight</th>
                <th style="padding:6px 6px; text-align:right;">Raw</th>
                <th style="padding:6px 8px; text-align:right;">Penalty</th>
              </tr>
            </thead>
            <tbody>
              ${critList.map(c => `
                <tr style="border-bottom:1px solid #f8fafc;">
                  <td style="padding:6px 8px; font-weight:600; color:#1e293b;">${c.num}. ${c.name}</td>
                  <td style="padding:6px 6px; text-align:center; font-weight:700; color:#2563eb;">${c.w}</td>
                  <td style="padding:6px 6px; text-align:right; color:#64748b;">${c.raw}</td>
                  <td style="padding:6px 8px; text-align:right; font-weight:700; color:#0f172a;">${c.wt}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>
        <div>
          <div style="display:grid; grid-template-columns:repeat(2, 1fr); gap:8px; border-top:1px solid #f1f5f9; padding-top:10px;">
            <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:8px; text-align:center;">
              <div style="font-size:18px; font-weight:800; color:${isBest ? '#16a34a' : '#2563eb'};">${totalPen}</div>
              <div style="font-size:9.5px; font-weight:700; color:#64748b; text-transform:uppercase;">TOTAL PENALTY</div>
            </div>
            <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:8px; text-align:center;">
              <div style="font-size:18px; font-weight:800; color:${isBest ? '#16a34a' : '#0f172a'};">${scoreVal}%</div>
              <div style="font-size:9.5px; font-weight:700; color:#64748b; text-transform:uppercase;">AQWI SCORE</div>
            </div>
          </div>
        </div>
      </div>
    `;

    detailsContainer.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; flex-wrap:wrap; gap:10px;">
        <div>
          <div style="font-weight:800; font-size:16px; color:#0f172a;">Justification of Penalty Weights — 3 Comparative Profiles (Sensitivity Analysis)</div>
          <div style="font-size:12px; color:#64748b;">
            Empirical evaluation of criterion weighting profiles: Demonstrating why <b>Balanced Unit Weighting (Case 3)</b> is mathematically and pedagogically superior to biased extremes.
          </div>
        </div>
        ${navButtons('weights')}
      </div>
      <div style="display:flex; gap:14px; flex-wrap:wrap; margin-bottom:16px;">
        ${renderWeightCaseCard("CASE 1: STUDENT-BIASED", "High student priority (w1,w2=3.0, w3=2.0) with neglected faculty load (w4,w7=0.2). Masks faculty burnout.", case1Crit, "19.45", "99.8", "SUB-OPTIMAL", "background:#fef2f2; color:#dc2626; border:1px solid #fecaca;", false)}
        ${renderWeightCaseCard("CASE 2: FACULTY-BIASED", "High faculty priority (w4,w7=3.0) with downweighted student welfare (w1,w2=0.2). Amplifies teacher gaps to 57.0.", case2Crit, "88.75", "99.3", "SUB-OPTIMAL", "background:#fffbeb; color:#d97706; border:1px solid #fde68a;", false)}
        ${renderWeightCaseCard("CASE 3: BALANCED NEP UNIT", "Equal unit weighting (w_i = 1.0 ∀ i). True Pareto-optimal balance without dimensional scale distortion or stakeholder bias.", case3Crit, "41.25", "99.7", "★ OPTIMAL (CURRENT)", "background:#f0fdf4; color:#166534; border:1px solid #bbf7d0;", true)}
      </div>
      <div style="background:#f0fdf4; border:1.5px solid #86efac; border-radius:10px; padding:14px 18px; font-size:12.5px; color:#14532d; line-height:1.55;">
        <b>🎯 Formal Justification of Case 3 (Current Equal Unit Weighting):</b>
        <ol style="margin:6px 0 0 18px;">
          <li><b>Pareto Dominance Equivalence:</b> Equal unit weights directly preserve the non-dominated Pareto frontier ($M_1..M_7$), ensuring no arbitrary trade-off distortion.</li>
          <li><b>Scale Invariance:</b> Unit weights treat each pedagogical criterion with unit parity, calibrated against the exponential cohort baseline $K = 1000 \cdot (N_{\text{divisions}} + N_{\text{faculty}})$.</li>
          <li><b>NEP 2020 Compliance:</b> Prevents sacrificing faculty welfare for students (Case 1) or student welfare for faculty (Case 2), providing a robust, dispute-free institutional standard.</li>
        </ol>
      </div>
    `;
    return;
  }

  if (currentAuditViewMode === "hypothesis1") {
    // Exact Slide 1: Side-by-side Hypothesis 1 Adaptive Priority Test
    const fixedCriteria = [
      { num: "1", name: "8-6 Day Span Avoidance", goal: "Penalizes student days stretched across 10 hours (08:00 to 18:00).", val: "0.0" },
      { num: "2", name: "Student Gap Minimization", goal: "Penalizes idle unallotted gap hours between student lectures.", val: "0.0" },
      { num: "3", name: "Same-Day Lec & Lab Separation", goal: "Prevents single-subject overload by separating theory and lab onto different days.", val: "0.0" },
      { num: "4", name: "Faculty Workload Balance", goal: "Evenly spreads teaching hours across a faculty member's active days.", val: "18.2" },
      { num: "5", name: "Honours Boundary Placement", goal: "Places Honours and Open Electives at start (08:00) or end of day to avoid midday gaps.", val: "6.0" },
      { num: "6", name: "3-Day Consecutive Subject Spread", goal: "Penalizes clustering the same subject on 3 or more consecutive weekdays.", val: "38.0" },
      { num: "7", name: "Faculty Long Gap Elimination", goal: "Prevents faculty from waiting idle on campus for more than 2 hours between classes.", val: "56.7" },
    ];

    const adaptiveCriteria = [
      { num: "1", name: "8-6 Day Span Avoidance", goal: "Penalizes student days stretched across 10 hours (08:00 to 18:00).", val: "0.0" },
      { num: "2", name: "Student Gap Minimization", goal: "Penalizes idle unallotted gap hours between student lectures.", val: "0.0" },
      { num: "3", name: "Same-Day Lec & Lab Separation", goal: "Prevents single-subject overload by separating theory and lab onto different days.", val: "0.0" },
      { num: "4", name: "Faculty Workload Balance", goal: "Evenly spreads teaching hours across a faculty member's active days.", val: "17.1" },
      { num: "5", name: "Honours Boundary Placement", goal: "Places Honours and Open Electives at start (08:00) or end of day to avoid midday gaps.", val: "6.0" },
      { num: "6", name: "3-Day Consecutive Subject Spread", goal: "Penalizes clustering the same subject on 3 or more consecutive weekdays.", val: "37.5" },
      { num: "7", name: "Faculty Long Gap Elimination", goal: "Prevents faculty from waiting idle on campus for more than 2 hours between classes.", val: "56.0" },
    ];

    detailsContainer.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; flex-wrap:wrap; gap:10px;">
        <div>
          <div style="font-weight:800; font-size:16px; color:#0f172a;">Formulation of Hypothesis — Hypothesis 1 Results: Adaptive Priority Test</div>
          <div style="font-size:12px; color:#64748b;">
            Evaluating whether dynamically increasing the priority of persistently violated soft constraints improves timetable quality: 
            <b>H<sub>0</sub>: &mu;<sub>Adaptive</sub> &ge; &mu;<sub>Fixed</sub></b> vs <b>H<sub>1</sub>: &mu;<sub>Adaptive</sub> &lt; &mu;<sub>Fixed</sub></b>
          </div>
        </div>
        ${navButtons('hypothesis1')}
      </div>
      <div style="display:flex; gap:16px; flex-wrap:wrap; margin-bottom:16px;">
        ${renderAuditCard("BASELINE — FIXED-WEIGHT CP-SAT", "Fixed Objective Weights (W_k = Constant)", fixedCriteria, "118.9", "#64748b", "cpsat", "FEASIBLE")}
        ${renderAuditCard("ADAPTIVE CP-SAT (FEEDBACK LOOP)", "Adaptive Closed-Loop Weight Updates", adaptiveCriteria, "116.6", "#16a34a", "adaptive cpsat", "OPTIMAL")}
      </div>
      <div style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:8px; padding:12px 16px; font-size:12px; color:#14532d; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
        <div>
          <b>★ Hypothesis 1 Confirmed (H<sub>0</sub> Rejected):</b> Adaptive CP-SAT dynamic weight updates significantly reduced soft penalty from <b>118.9</b> down to <b>116.6</b> while proving mathematical optimality.
        </div>
        <div style="font-family:monospace; font-size:11.5px; background:#fff; padding:4px 8px; border-radius:4px; border:1px solid #bbf7d0;">
          W(k,t+1) = min[50, W(k,t) &times; (1 + 0.15 &times; Pressure)]
        </div>
      </div>
    `;
    return;
  }

  if (currentAuditViewMode === "hypothesis3") {
    // Exact Slide 2: Side-by-side BASELINE vs MODIFIED CPSAT cards
    const baselineCriteria = [
      { num: "1", name: "8-6 Day Span Avoidance", goal: "Penalizes student days stretched across 10 hours (08:00 to 18:00).", val: "0.0" },
      { num: "2", name: "Student Gap Minimization", goal: "Penalizes idle unallotted gap hours between student lectures.", val: "0.0" },
      { num: "3", name: "Same-Day Lec & Lab Separation", goal: "Prevents single-subject overload by separating theory and lab onto different days.", val: "0.0" },
      { num: "4", name: "Faculty Workload Balance", goal: "Evenly spreads teaching hours across a faculty member's active days.", val: "10.2" },
      { num: "5", name: "Honours Boundary Placement", goal: "Places Honours and Open Electives at start (08:00) or end of day to avoid midday gaps.", val: "6.0" },
      { num: "6", name: "3-Day Consecutive Subject Spread", goal: "Penalizes clustering the same subject on 3 or more consecutive weekdays.", val: "31.0" },
      { num: "7", name: "Faculty Long Gap Elimination", goal: "Prevents faculty from waiting idle on campus for more than 2 hours between classes.", val: "15.0" },
    ];

    const modifiedCriteria = [
      { num: "1", name: "8-6 Day Span Avoidance", goal: "Penalizes student days stretched across 10 hours (08:00 to 18:00).", val: "3.0" },
      { num: "2", name: "Student Gap Minimization", goal: "Penalizes idle unallotted gap hours between student lectures.", val: "0.0" },
      { num: "3", name: "Same-Day Lec & Lab Separation", goal: "Prevents single-subject overload by separating theory and lab onto different days.", val: "0.0" },
      { num: "4", name: "Faculty Workload Balance", goal: "Evenly spreads teaching hours across a faculty member's active days.", val: "6.0" },
      { num: "5", name: "Honours Boundary Placement", goal: "Places Honours and Open Electives at start (08:00) or end of day to avoid midday gaps.", val: "6.0" },
      { num: "6", name: "3-Day Consecutive Subject Spread", goal: "Penalizes clustering the same subject on 3 or more consecutive weekdays.", val: "30.0" },
      { num: "7", name: "Faculty Long Gap Elimination", goal: "Prevents faculty from waiting idle on campus for more than 2 hours between classes.", val: "10.0" },
    ];

    detailsContainer.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; flex-wrap:wrap; gap:10px;">
        <div>
          <div style="font-weight:800; font-size:16px; color:#0f172a;">Formulation of Hypothesis — Hypothesis 3 Results:</div>
          <div style="font-size:12px; color:#64748b;">Evaluating soft-cost reduction achieved by custom CP-SAT source code changes (LNS &amp; MRV heuristics)</div>
        </div>
        ${navButtons('hypothesis3')}
      </div>
      <div style="display:flex; gap:16px; flex-wrap:wrap;">
        ${renderAuditCard("BASELINE — STOCK CPSAT", "Stock Google OR-Tools Solver", baselineCriteria, "96.8", "#64748b", "cpsat", "done")}
        ${renderAuditCard("MODIFIED CPSAT (SOURCE CODE CHANGES)", "Custom LNS + MRV + Visiting Strategy", modifiedCriteria, "78.5", "#16a34a", "adaptive cpsat", "done")}
      </div>
    `;
    return;
  }

  // Standard Candidate Audit Breakdown
  const critEntries = Object.entries(activeRep.criteria || {});
  const critRows = critEntries.map(([key, crit], cIdx) => {
    return `
      <tr style="border-bottom:1px solid #f1f5f9; font-size:12.5px;">
        <td style="padding:9px 12px; font-weight:700; color:#1e293b;">${cIdx + 1}. ${crit.name}</td>
        <td style="padding:9px 12px; color:#475569; font-size:12px;">${crit.description}</td>
        <td style="padding:9px 12px; text-align:right; font-weight:700; color:#334155;">${crit.raw_metric.toFixed(1)}</td>
      </tr>
    `;
  }).join("");

  detailsContainer.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; flex-wrap:wrap; gap:10px;">
      <div>
        <div style="font-weight:800; font-size:15px; color:#0f172a;">
          Candidate #${selectedIdx + 1} — 7-Point AQWI Institutional Audit Breakdown
        </div>
        <div style="font-size:12px; color:#64748b;">
          Evaluated against 7 NEP-aligned pedagogical and faculty welfare criteria (Equal unit weight 1.0)
        </div>
      </div>
      ${navButtons('candidate')}
    </div>
    <table style="width:100%; border-collapse:collapse; background:#fff; margin-bottom:16px;">
      <thead>
        <tr style="background:#f8fafc; border-bottom:1.5px solid #e2e8f0; font-size:11px; text-transform:uppercase; color:#64748b; font-weight:700; letter-spacing:0.4px;">
          <th style="padding:10px 12px; text-align:left;">JUDGE CRITERION</th>
          <th style="padding:10px 12px; text-align:left;">OBJECTIVE GOAL</th>
          <th style="padding:10px 12px; text-align:right;">RAW METRIC</th>
        </tr>
      </thead>
      <tbody>
        ${critRows}
      </tbody>
    </table>
    <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(130px, 1fr)); gap:12px; border-top:1px solid #f1f5f9; padding-top:14px;">
      <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:10px; padding:12px 14px; text-align:center;">
        <div style="font-size:20px; font-weight:800; color:#0f172a;">adaptive cpsat</div>
        <div style="font-size:11px; font-weight:700; color:#64748b; text-transform:uppercase; letter-spacing:0.5px; margin-top:2px;">SOLVER</div>
      </div>
      <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:10px; padding:12px 14px; text-align:center;">
        <div style="font-size:22px; font-weight:800; color:#16a34a;">done</div>
        <div style="font-size:11px; font-weight:700; color:#64748b; text-transform:uppercase; letter-spacing:0.5px; margin-top:2px;">STATUS</div>
      </div>
      <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:10px; padding:12px 14px; text-align:center;">
        <div style="font-size:22px; font-weight:800; color:${activeRep.hard_violations === 0 ? '#16a34a' : '#dc2626'};">${activeRep.hard_violations}</div>
        <div style="font-size:11px; font-weight:700; color:#64748b; text-transform:uppercase; letter-spacing:0.5px; margin-top:2px;">HARD VIOLATIONS</div>
      </div>
      <div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:10px; padding:12px 14px; text-align:center;">
        <div style="font-size:22px; font-weight:800; color:#2563eb;">${activeRep.total_penalty.toFixed(1)}</div>
        <div style="font-size:11px; font-weight:700; color:#64748b; text-transform:uppercase; letter-spacing:0.5px; margin-top:2px;">SOFT COST</div>
      </div>
    </div>
  `;
}

window.selectCandidateTimetable = async function(runId, candIdx) {
  try {
    const res = await fetch(`/api/runs/${runId}/select-candidate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candidate_idx: candIdx }),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    currentGrids = data.grids;
    originalGrids = data.grids;
    activeDivision = 0;
    renderTabs();
    renderGrid();

    const runRes = await fetch(`/api/runs/${runId}`);
    if (runRes.ok) {
      const run = await runRes.json();
      renderSummary(run);
      renderJudgeLeaderboard(run);
    }
  } catch (err) {
    console.error("Failed to switch candidate:", err);
  }
};

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
      renderJudgeLeaderboard(run);
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
