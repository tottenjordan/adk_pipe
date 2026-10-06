"""A configurable, dependency-free fake ``google.cloud.bigquery.Client``.

Kept out of ``tests/_fakes.py`` on purpose: that module imports ``google.adk`` at
module level, and the bandit/experiments tests must not pay that import.

``FakeBigQueryClient`` records every ``query`` (``queries``: ``(sql, job_config)``
in call order) and ``insert_rows_json`` (``inserts``: ``(table, rows, row_ids)``).
``query`` returns a ``FakeQueryJob`` whose ``result()`` is the configured rows and
which carries the attributes the production code reads (``errors``, ``job_id``,
``num_dml_affected_rows``).

``results`` is either the row list every query returns, or a callable
``(sql, job_config) -> rows``; the call is recorded *before* the callable runs, so
a callable that raises (e.g. ``google.api_core.exceptions.BadRequest`` to mimic an
unmigrated table) still shows up in ``queries``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

Rows = Iterable[Any]


class FakeQueryJob:
    """What ``FakeBigQueryClient.query`` returns: a finished ``QueryJob``."""

    def __init__(
        self,
        rows: list[Any],
        *,
        errors: Any = None,
        job_id: str = "j1",
        num_dml_affected_rows: int | None = 1,
    ):
        self._rows = rows
        self.errors = errors
        self.job_id = job_id
        self.num_dml_affected_rows = num_dml_affected_rows

    def result(self) -> list[Any]:
        return list(self._rows)


class FakeBigQueryClient:
    """Records queries/streaming inserts; returns canned rows and insert errors.

    ``job_errors`` / ``num_dml_affected_rows`` are copied onto every returned job
    (both are plain attributes, so a test may change them between calls).
    """

    def __init__(
        self,
        results: Rows | Callable[[str, Any], Rows] | None = None,
        insert_errors: list[Any] | None = None,
        *,
        job_errors: Any = None,
        num_dml_affected_rows: int | None = 1,
    ):
        self.results = results
        self.insert_errors = insert_errors
        self.job_errors = job_errors
        self.num_dml_affected_rows = num_dml_affected_rows
        self.queries: list[tuple[str, Any]] = []
        self.inserts: list[tuple[str, list[dict], list[str]]] = []

    @property
    def sqls(self) -> list[str]:
        """Just the SQL of every query, in call order."""
        return [sql for sql, _ in self.queries]

    def query(self, sql: str, job_config: Any = None) -> FakeQueryJob:
        self.queries.append((sql, job_config))
        rows = self.results(sql, job_config) if callable(self.results) else self.results
        return FakeQueryJob(
            list(rows or []),
            errors=self.job_errors,
            num_dml_affected_rows=self.num_dml_affected_rows,
        )

    def insert_rows_json(
        self, table: Any, rows: Iterable[dict], row_ids: Iterable[str] | None = None
    ) -> list[Any]:
        self.inserts.append((table, list(rows), list(row_ids or [])))
        return list(self.insert_errors or [])

    def rows(self, table_suffix: str) -> list[dict]:
        """Every row streamed into a table whose name ends with ``table_suffix``."""
        return [
            row
            for table, rows, _ in self.inserts
            if str(table).endswith(table_suffix)
            for row in rows
        ]
