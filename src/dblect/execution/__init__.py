"""DuckDB execution harness: run dbt models against generated data."""

from dblect.execution.run import RunError, RunResult, run_model

__all__ = ["RunError", "RunResult", "run_model"]
