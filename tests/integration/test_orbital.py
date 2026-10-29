"""A fitted scikit-learn pipeline, exported to SQL by orbital, scored in
DuckDB and in q: the scores have to agree.

orbital compiles a pipeline through ibis, which has no q backend, so the
route is orbital to DuckDB SQL, then sqlglot to q. Needs the orbital group:
uv sync --group orbital.
"""

from __future__ import annotations

import pytest
import sqlglot

from tests.integration.harness import duckdb_connection, run_q, same

orbital = pytest.importorskip("orbital")
np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")
sklearn = pytest.importorskip("sklearn")

from sklearn.ensemble import GradientBoostingRegressor, RandomForestClassifier  # noqa: E402
from sklearn.linear_model import LinearRegression, LogisticRegression  # noqa: E402
from sklearn.pipeline import make_pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402
from sklearn.tree import DecisionTreeRegressor  # noqa: E402

rng = np.random.default_rng(0)
FRAME = pd.DataFrame({"x1": rng.normal(size=40), "x2": rng.normal(size=40)})
TARGET = 2 * FRAME.x1 - FRAME.x2 + 0.5
LABEL = (TARGET > 0.5).astype(int)

MODELS = {
    "linear": (make_pipeline(StandardScaler(), LinearRegression()), TARGET),
    "logistic": (make_pipeline(StandardScaler(), LogisticRegression()), LABEL),
    "tree": (DecisionTreeRegressor(max_depth=3), TARGET),
    "boosting": (GradientBoostingRegressor(n_estimators=5, max_depth=2), TARGET),
    "forest": (RandomForestClassifier(n_estimators=3, max_depth=2, random_state=0), LABEL),
}


@pytest.mark.parametrize("name", MODELS)
def test_q_scores_what_duckdb_scores(name):
    model, target = MODELS[name]
    model.fit(FRAME, target)
    pipeline = orbital.parse_pipeline(model, features=orbital.types.guess_datatypes(FRAME))
    sql = orbital.export_sql("t", pipeline, dialect="duckdb")
    q = sqlglot.transpile(sql, read="duckdb", write="q")[0]

    tables = {"t": {c: FRAME[c].tolist() for c in FRAME}}
    expected = [tuple(r) for r in duckdb_connection(tables).execute(sql).fetchall()]
    (got,) = run_q(tables, [q])
    assert not isinstance(got, str), f"q refused {q}: {got}"
    assert same(got, expected, ordered=True), f"{q}\nq:      {got[:3]}\nduckdb: {expected[:3]}"
