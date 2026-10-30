"""Regression: numpy array methods (x.mean(), x.sum(), np.percentile, repr) work inside the PyChart sandbox.

numpy imports some internals lazily (numpy._core._methods ...) through the caller's builtins; the worker warms them
up before installing its restrictions and serves already-loaded numpy internals to numpy's own C code.
"""
from __future__ import annotations

import pytest

from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END

sandbox = pytest.importorskip("app.scripting.sandbox")


def test_numpy_methods_inside_sandbox():
    cs = generate_candleset(n=200, seed=9, end_time=FIXED_END).to_list()
    code = (
        'indicator("métodos numpy", overlay=False)\n'
        "x = np.nan_to_num(close - ta.sma(close, 20))\n"
        "print(round(float(x.mean()), 6), round(float(x.sum()), 3), round(float(close.std()), 4))\n"
        "print(round(float(np.percentile(close, 90)), 4), round(float(np.nanmedian(x)), 4), x[-2:])\n"
        'plot(x - x.mean(), "desviación")\n'
    )
    result = sandbox.run_script(code, cs, {}, symbol="SIM:NEXUS", tf="1h", timeout=20)
    assert result["ok"], result["errors"]
    assert len(result["plots"]) == 1 and result["stdout"].strip()


def test_numpy_file_api_still_blocked():
    cs = generate_candleset(n=50, seed=9, end_time=FIXED_END).to_list()
    result = sandbox.run_script('indicator("x")\nnp.save("x.npy", close)\n', cs, {}, timeout=10)
    assert not result["ok"] and result["errors"][0]["kind"] == "security"
