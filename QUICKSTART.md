# Quickstart — run the prototype on the modified OR-Tools

This project runs on a **source-modified build of Google OR-Tools' CP-SAT solver**. The changes
are described in [`CHANGES_DONE.md`](CHANGES_DONE.md); the patch itself is
[`third_party/or-tools-fork/timetabling.patch`](third_party/or-tools-fork/timetabling.patch).

You do **not** need a C++ toolchain. Prebuilt wheels are published on the
[Releases page](../../releases) for Windows and Linux (Python 3.11).

---

## 1. Clone and create a virtualenv

```bash
git clone https://github.com/VihaanR/mutliobjective-timetable-scheduler-
cd mutliobjective-timetable-scheduler-

python -m venv .venv-fork
# Windows
.venv-fork\Scripts\activate
# Linux / macOS
source .venv-fork/bin/activate
```

Python **3.11** specifically — the wheels are built `cp311` and will not install on 3.10 or 3.12.

## 2. Install the modified OR-Tools

Download the wheel for your platform from the [Releases page](../../releases) (tag `fork-v5` or
later), then:

```bash
pip install <path-to>/ortools-9.15.9999-cp311-cp311-win_amd64.whl     # Windows
pip install <path-to>/ortools-9.15.9999-cp311-cp311-linux_x86_64.whl  # Linux
pip install -r requirements.txt
```

Install the fork **before** `requirements.txt`, or pip will pull stock `ortools` from PyPI and you
will silently run the unmodified solver.

## 3. Verify you are actually on the fork — do not skip this

```bash
python third_party/or-tools-fork/verify_fork.py
```

Must print **`FORK build.`** and exit 0. If it prints `STOCK build.` the wheel did not take, and
everything below will run on unmodified OR-Tools while looking completely normal.

For the full functional check — that the solver *uses* the changes, not merely that they are
present:

```bash
python third_party/or-tools-fork/smoke_test.py
```

Expect 5/5 checks passing, including `division_day_lns` appearing in CP-SAT's subsolver list
(10 interleaved subsolvers instead of the stock 9).

## 4. Run the web application

```bash
python -m uvicorn webapp.server:app --host 127.0.0.1 --port 8750
```

Open <http://127.0.0.1:8750>.

Sample faculty login (development credentials, see `webapp/auth.py`):

```
email    : nilesh.marathe@djsce.edu.in
password : Timetable@123
role     : faculty
```

Pages: `/platform` (generate a timetable), `/dashboard` (faculty, branches, allocations),
`/my-timetable` (the signed-in teacher's week), `/sy-reference`.

Timetables generated here are produced by the modified solver — `engine/solvers/cpsat.py` detects
the fork at import and enables both features automatically.

---

## Running on stock OR-Tools instead

The project is deliberately fail-safe: on a stock `pip install ortools` it drops back to plain
CP-SAT, emits no structure tags, and never names the fork's subsolver. Nothing breaks; you simply
do not get the modified behaviour. That is what makes a stock-vs-fork comparison valid — both
builds solve a byte-identical model.

## Reproducing the benchmark

See [`CHANGES_DONE.md` §6.3](CHANGES_DONE.md). Briefly: the baseline must be the **unpatched build
of the same upstream commit** (also produced by CI), never the PyPI wheel — PyPI ships 9.9.x while
this tree is 9.15.x, so comparing against it measures six minor releases of upstream improvement
and credits them to the fork.

```bash
.venv-baseline/Scripts/python -m research.fork_benchmark --arms A --seeds 20 --out research/base20.json
.venv-fork/Scripts/python     -m research.fork_benchmark --arms B C D E --seeds 20 --out research/fork20.json
.venv-fork/Scripts/python     -m research.fork_benchmark --compare research/base20.json research/fork20.json
```

## Building the wheels yourself

Push a `fork-v*` tag and the `build-ortools-fork` workflow builds and publishes them. To build
locally instead, see [`third_party/or-tools-fork/README.md`](third_party/or-tools-fork/README.md)
— expect roughly an hour and a full C++ toolchain.

## Tests

```bash
python -m pytest -q
```

Known: a small number of solver tests are time-budgeted and flaky by measurement —
`test_cpsat_breaks_vary_across_days` was observed passing 3 times in 5 against an *unmodified*
model. Treat isolated failures there as noise rather than regressions, and never judge solver
changes on a single run (§6.1).
