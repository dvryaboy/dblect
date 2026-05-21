"""DuckDB execution harness: run dbt models against generated data."""

from dblect.execution.run import Phase, RunError, RunResult, run_model

__all__ = ["Phase", "RunError", "RunResult", "run_model"]
