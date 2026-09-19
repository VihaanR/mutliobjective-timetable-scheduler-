# Multiobjective Timetable Scheduler

A university timetabling platform for DJ Sanghvi College of Engineering, built on a
**source-modified build of Google OR-Tools' CP-SAT solver**.

The solver is not merely configured — it is patched. Two scheduling-specific capabilities were
added to CP-SAT's C++ internals because neither is reachable through the public API:

| Feature | What it does |
|---|---|
| `division_day_lns` | A Large Neighbourhood Search generator that re-optimises one whole *division-day* at a time, instead of the random or graph-walk fragments the built-in generators produce. |
| `CHOOSE_MIN_UNFIXED_IN_GROUP` | A branching strategy that picks the course with the fewest placements still open, re-ranked live at every node. |

The complete change is **9 files, 245 insertions, 2 modified lines, 0 deletions** against upstream
`google/or-tools@98c165af`, vendored as a patch at
[`third_party/or-tools-fork/timetabling.patch`](third_party/or-tools-fork/timetabling.patch).

- **[CHANGES_DONE.md](CHANGES_DONE.md)** — the full technical write-up: every code change with
  rationale, why parameters cannot express these features, and the measured results.
- **[QUICKSTART.md](QUICKSTART.md)** — the condensed install path.

---

## Install and run on a fresh machine

You do **not** need a C++ toolchain. Prebuilt wheels for Windows and Linux are published on the
[Releases page](../../releases).

### 1. Requirements

- **Python 3.11** exactly — the wheels are built `cp311` and will not install on 3.10 or 3.12.
- Git.

### 2. Clone

```bash
git clone https://github.com/VihaanR/mutliobjective-timetable-scheduler-
cd mutliobjective-timetable-scheduler-
```

### 3. Create a virtualenv

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
```

### 4. Install the modified OR-Tools **first**

Download the wheel matching your OS from the [Releases page](../../releases) (tag `fork-v5` or
later), then:

```bash
# Windows
pip install ortools-9.15.9999-cp311-cp311-win_amd64.whl
# Linux
pip install ortools-9.15.9999-cp311-cp311-linux_x86_64.whl

pip install -r requirements.txt
```

> **Order matters.** `requirements.txt` lists `ortools>=9.9`. If you install it first, pip pulls
> the stock build from PyPI and then considers the requirement satisfied — you will silently run
> unmodified OR-Tools.

### 5. Verify you are on the modified solver

```bash
python third_party/or-tools-fork/verify_fork.py
```

Must print **`FORK build.`** and exit 0. If it prints `STOCK build.`, the wheel did not take.

**Do not skip this.** The project is deliberately fail-safe: on stock OR-Tools it falls back to
plain CP-SAT, emits no structure tags, and produces perfectly good timetables. A stock fallback
looks completely normal — there is no error to notice.

For the full functional check — that the solver *uses* the modifications, not merely that they
are present:

```bash
python third_party/or-tools-fork/smoke_test.py
```

Expect **5/5 checks passing**, including `division_day_lns` appearing in CP-SAT's subsolver list
(10 interleaved subsolvers, where stock reports 9).

### 6. Run the application

```bash
python -m uvicorn webapp.server:app --host 127.0.0.1 --port 8750
```

Open <http://127.0.0.1:8750>.

Development login (see `webapp/auth.py`):

```
email    : nilesh.marathe@djsce.edu.in
password : Timetable@123
role     : faculty
```

| Page | What it shows |
|---|---|
| `/dashboard` | Faculty, branches, divisions, subjects, allocations, rooms, weekly slot grid |
| `/platform` | Generate a timetable — pick solver and time limit, export to Excel/PDF |
| `/my-timetable` | The signed-in teacher's own week |
| `/login` | Sign in |

Timetables generated here are produced by the modified solver: `engine/solvers/cpsat.py` detects
the fork at import and enables both features automatically.

---

## Repository layout

```
.
├── README.md  QUICKSTART.md  CHANGES_DONE.md
├── requirements.txt  pytest.ini
│
├── data.py  model.py  solver.py            SY-reference pipeline: the original
├── extract_schedule.py  main.py  pareto.py   bespoke DataBundle/CP-SAT model
│
├── engine/                 Generic multi-solver engine (Greedy, MIP, GA, CP-SAT),
│   └── solvers/cpsat.py      any branch/division/year. Fork integration lives here.
│
├── webapp/                 FastAPI app: auth, CRUD routers, static UI
│
├── third_party/or-tools-fork/
│   ├── timetabling.patch       the source modification itself
│   ├── verify_fork.py          fork-vs-stock check
│   ├── smoke_test.py           functional check that the fork engages
│   ├── algo_test.cc            standalone algorithm verification
│   └── NOTICE                  Apache-2.0 attribution for the modified build
│
├── research/               Benchmark harness and 20-seed result data
├── tests/                  pytest suite
├── data/                   Reference DJSCE datasets
├── docs/                   Design notes and the source-modification guide
└── .github/workflows/      CI: builds fork + unpatched baseline wheels
```

Two solver stacks coexist deliberately: the root modules are the original hardcoded SY-reference
pipeline, and `engine/` is the later generic engine that handles any branch and year.

---

## Results

Measured on the DJSCE CSE-DS reference instance: 20 seeds per arm, 120 s limit, 100 solves, run
sequentially on an idle machine. The baseline is the **unpatched build of the same upstream
commit**, not the PyPI wheel.

| arm | | proofs | rate | Fisher *p* vs A |
|---|---|---:|---:|---:|
| A | unpatched baseline | 15/20 | 75% | — |
| B | fork, both features off | 14/20 | 70% | 1.000 |
| C | static ordering (ablation) | 18/20 | 90% | 0.407 |
| D | `division_day_lns` only | 16/20 | 80% | 1.000 |
| E | both features | 19/20 | 95% | 0.182 |

**The patch is inert when disabled** — A vs B is *p* = 1.000 with overlapping ranges, so the
patched binary with both features off behaves like the unpatched build. That is the control the
rest of the table depends on, and it holds.

**No arm reaches statistical significance, and there is no speed-up.** Every time-to-proof median
falls between 61.5 s and 70.0 s. Arm C — plain variable ordering, achievable on a *stock* wheel —
is statistically indistinguishable from arm E, so the visible benefit is attributable to ordering
rather than to the C++ changes.

Separating 75% from 95% at *p* < 0.05 would need roughly 46 seeds per arm. Full analysis, including
why `soft_cost` is not the right metric and why 5 seeds was not enough, is in
[CHANGES_DONE.md §6.3](CHANGES_DONE.md).

---

## Tests

```bash
python -m pytest -q
```

Some solver tests are time-budgeted and flaky by measurement — `test_cpsat_breaks_vary_across_days`
was observed passing 3 times in 5 against an *unmodified* model. Treat isolated failures there as
noise, and never judge a solver change on a single run.

## Building the wheels yourself

Push a `fork-v*` tag; the `build-ortools-fork` workflow patches upstream, builds fork and
unpatched-baseline wheels for both platforms, asserts each is what it claims to be, and publishes
the fork wheels to a Release. To build locally instead, see
[`third_party/or-tools-fork/README.md`](third_party/or-tools-fork/README.md) — expect about an hour
and a full C++ toolchain.

## Licence

OR-Tools is Apache-2.0. This project distributes modified builds of it; see
[`third_party/or-tools-fork/NOTICE`](third_party/or-tools-fork/NOTICE) for the attribution and the
list of modified files, as Apache-2.0 §4(b) requires. Not affiliated with or endorsed by Google.
