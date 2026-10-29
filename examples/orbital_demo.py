"""Fit a scikit-learn model, export it with orbital, and score it in q.

    pip install orbital scikit-learn pandas \
        "sqlglot-q @ git+https://github.com/kwojdalski/sqlglot-q"
    python examples/orbital_demo.py

Needs q on PATH. The script writes the data to a CSV, loads it into q as the
table `t`, runs the translated model on it, and checks q's probabilities
against scikit-learn's own predict_proba.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import orbital
import pandas as pd
import sqlglot
from sklearn.datasets import load_iris
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

# 1. Fit a model on four float features.
iris = load_iris(as_frame=True)
X = iris.data.set_axis(["sepal_length", "sepal_width", "petal_length", "petal_width"], axis=1)
y = (iris.target == 2).astype(int)  # virginica or not
model = make_pipeline(StandardScaler(), LogisticRegression()).fit(X, y)

# 2. orbital exports SQL through ibis, which has no q backend: export DuckDB
#    SQL, then let sqlglot (with this plugin installed) write it as q.
pipeline = orbital.parse_pipeline(model, features=orbital.types.guess_datatypes(X))
sql = orbital.export_sql("t", pipeline, dialect="duckdb")
q = sqlglot.transpile(sql, read="duckdb", write="q")[0]
print("q:\n", q, "\n", sep="")

# 3. Score in q: load the CSV as t, run the query, print JSON.
q_bin = shutil.which("q")
if q_bin is None:
    raise SystemExit("q is not on PATH; the translation above is the result")
with tempfile.TemporaryDirectory() as tmp:
    csv = Path(tmp) / "t.csv"
    X.to_csv(csv, index=False)
    script = Path(tmp) / "score.q"
    script.write_text(
        'system "P 0";\n'
        f't:("FFFF";enlist ",")0:`$":{csv}";\n'
        f"r:{q};\n"
        "-1 .j.j flip value flip r;\n"
        "exit 0;\n"
    )
    out = subprocess.run([q_bin, str(script), "-q"], cwd=tmp, capture_output=True, text=True)
    if out.returncode or not out.stdout.strip():
        raise SystemExit(out.stdout + out.stderr)

rows = json.loads(out.stdout)
scores = pd.DataFrame(rows, columns=["p0", "p1", "label"])
expected = model.predict_proba(X)
print(scores.head(), "\n")
gap = float(np.abs(scores[["p0", "p1"]].to_numpy() - expected).max())
labels = bool((scores.label.to_numpy() == model.predict(X)).all())
print(f"max |q - sklearn| probability: {gap:.2e}; labels agree: {labels}")
