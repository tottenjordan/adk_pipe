"""deployment/backfill_eval_dimension_labels.py: pure SQL builder (no BigQuery)."""

import pytest

from creative_eval.dimensions import DIMENSION_LABELS

TABLE = "hybrid-vertex.trend_trawler.creative_evals"


def _mod():
    pytest.importorskip("absl")  # only the CLI dep may be absent in a bare env
    import deployment.backfill_eval_dimension_labels as mod

    return mod


def test_one_when_per_known_dimension():
    sql = _mod().build_backfill_sql(TABLE)
    for key, label in DIMENSION_LABELS.items():
        assert sql.count(f"WHEN '{key}' THEN '{label}'") == 1
    assert sql.count("WHEN '") == len(DIMENSION_LABELS)


def test_targets_table_and_only_null_rows():
    sql = _mod().build_backfill_sql(TABLE)
    assert sql.startswith(f"UPDATE `{TABLE}`")
    assert "SET weakest_dimension_labels =" in sql
    assert sql.rstrip().endswith("WHERE weakest_dimension_labels IS NULL")


def test_fallback_sentence_case():
    sql = _mod().build_backfill_sql(TABLE)
    assert "REGEXP_REPLACE(d, r'_+', ' ')" in sql
    assert "r'\\s+'" in sql  # whitespace collapse
    assert "CONCAT(UPPER(SUBSTR(" in sql and "LOWER(SUBSTR(" in sql
    assert "TRIM(" in sql


def test_preserves_order_and_joins_with_comma_space():
    sql = _mod().build_backfill_sql(TABLE)
    assert "UNNEST(SPLIT(weakest_dimensions, ',')) AS d WITH OFFSET AS off" in sql
    assert "', ' ORDER BY off" in sql
    assert "STRING_AGG(" in sql


def test_empty_or_null_weakest_dimensions_become_empty_string():
    sql = _mod().build_backfill_sql(TABLE)
    # Empty elements are skipped, and an empty aggregate (NULL) collapses to ''.
    assert "WHERE TRIM(d) != ''" in sql
    assert "IFNULL(" in sql and "''\n)" in sql


@pytest.mark.parametrize(
    "bad",
    [
        "trend_trawler.creative_evals",
        "p.d.t.extra",
        "p.d.t; DROP TABLE x",
        "p.d.t`",
        "p.d.t\n",
        "p.d-x.t",
        "",
    ],
)
def test_rejects_malformed_table(bad):
    with pytest.raises(ValueError):
        _mod().build_backfill_sql(bad)


def test_sql_string_literal_escapes_quotes_and_backslashes():
    lit = _mod()._sql_string_literal
    assert lit("Brand & product") == "'Brand & product'"
    assert lit("It's") == "'It\\'s'"
    assert lit("a\\b") == "'a\\\\b'"
