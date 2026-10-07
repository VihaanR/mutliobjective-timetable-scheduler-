# Agent Instructions for Multiobjective Timetable Scheduler

## Key Commands

- Run tests: `pytest tests/`
- Run linter: `ruff check .`
- Run type checker: `mypy .`
- Run all checks: `ruff check . && mypy . && pytest tests/`

## Architecture

- Main entrypoint: `main.py`
- Core logic in `engine/` directory
- CP-SAT solver in `engine/solvers/cpsat.py`


## Testing

- Tests in `tests/` directory
- Use `pytest -k <test_name>` to run specific tests
- Integration tests require a running instance of the solver

## Style

- Follow PEP 8 guidelines
- Use type hints for all functions
- Keep functions small and focused

## Constraints

- Slack coefficients must be calculated as `max(1, 1000 // r_k)`

## Important Notes

- Documentation for the changes is in `docs/augmecon2_fix.md`
- All tests have been updated to verify the new implementation