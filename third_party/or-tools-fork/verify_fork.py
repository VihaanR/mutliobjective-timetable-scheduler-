"""Report whether the OR-Tools currently importable is the timetabling fork or stock.

Run this after installing a wheel:  python third_party/or-tools-fork/verify_fork.py
"""
import sys

try:
    import ortools
    from ortools.sat.python import cp_model
except ImportError:
    sys.exit("ortools is not installed in this interpreter")

version = getattr(ortools, "__version__", "unknown")
strategy = getattr(cp_model, "CHOOSE_MIN_UNFIXED_IN_GROUP", None)

print(f"interpreter : {sys.executable}")
print(f"ortools     : {version}")
print(f"location    : {ortools.__file__}")
print(f"fork enum   : {strategy}")
print()
if strategy is None:
    print("STOCK build. The fork's features are unavailable; the project falls back")
    print("to plain CP-SAT. Install a wheel from the repository's Releases page.")
    sys.exit(1)
print("FORK build. division_day_lns and CHOOSE_MIN_UNFIXED_IN_GROUP are available.")
