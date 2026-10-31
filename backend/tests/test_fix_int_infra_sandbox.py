"""Integration round (int-be-infra): sandbox OS-limit hardening and the buffer-array-constructor block.

- POSIX memory (verifies.cloud new_issue): the worker raises its own OOM priority so the kernel kills a runaway
  sandbox before the server, and the RLIMIT_AS headroom over memory_mb is trimmed from +1024 MB.
- Object-dtype ndarray over a raw buffer (verifies.sandbox new_issue): ``np.ndarray(..., buffer=...)`` and
  ``np.recarray(..., buf=...)`` are rejected statically, so a script can no longer crash the isolated worker that way.
"""
from __future__ import annotations

import os

import pytest

from app.scripting import sandbox, worker
from app.scripting.validator import compile_script
from app.sim.synthetic import generate_candleset


# ----------------------------------------------------------------------------- POSIX memory hardening (item 4)


def test_raise_oom_priority_writes_max_score(monkeypatch):
    writes: dict[str, str] = {}

    class FakeFile:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def write(self, text: str) -> None:
            writes[self.path] = writes.get(self.path, "") + text

    def fake_open(path, mode="r", *args, **kwargs):
        assert "w" in mode  # the helper opens for writing only
        return FakeFile(path)

    monkeypatch.setattr("builtins.open", fake_open)
    worker._raise_oom_priority()
    assert writes == {"/proc/self/oom_score_adj": "1000"}


def test_raise_oom_priority_ignores_missing_proc(monkeypatch):
    def fake_open(*args, **kwargs):
        raise OSError("no /proc/self/oom_score_adj here (macOS, Windows, restricted container)")

    monkeypatch.setattr("builtins.open", fake_open)
    worker._raise_oom_priority()  # best effort: must never raise


def test_posix_address_space_headroom_is_trimmed_but_leaves_numpy_room():
    # trimmed from the old +1024 MB (which let a 320 MB worker map ~1.3 GB and OOM a 1 GB VM) but still well above
    # memory_mb so numpy / BLAS virtual mappings fit; the real server protection is now the OOM-priority bump.
    assert 256 <= worker.POSIX_AS_HEADROOM_MB < 1024


@pytest.mark.skipif(os.name == "nt", reason="POSIX rlimits only (resource module)")
def test_limit_posix_caps_address_space_at_memory_plus_headroom(monkeypatch):
    import resource

    captured: dict[int, tuple] = {}
    monkeypatch.setattr(worker, "_raise_oom_priority", lambda: None)
    monkeypatch.setattr(resource, "getrlimit", lambda res: (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", lambda res, limits: captured.__setitem__(res, limits))
    worker._limit_posix(320)
    expected = (320 + worker.POSIX_AS_HEADROOM_MB) * 1024 * 1024
    assert captured[resource.RLIMIT_AS] == (expected, expected)


# ----------------------------------------------------------------------------- buffer-array constructors (item 5)

BUFFER_EXPLOITS = [
    # object dtype over arbitrary bytes: numpy would dereference the bytes as PyObject pointers and crash the worker
    "import numpy as np\nx = np.ndarray((8,), dtype='O', buffer=bytes([255] * 64))",
    "import numpy as np\nx = np.ndarray((8,), 'O', bytes([255] * 64))",  # buffer passed positionally
    "import numpy as np\nx = np.recarray((1,), dtype=[('a', 'O')], buf=bytes(8))",
    "from numpy import ndarray\nx = ndarray((4,), 'f8', bytes(32))",  # from-import, positional buffer
]


@pytest.mark.parametrize("code", BUFFER_EXPLOITS)
def test_buffer_array_constructor_is_rejected(code):
    res = compile_script("indicator('x')\n" + code + "\n")
    assert res["ok"] is False
    assert res["errors"] and all(e["kind"] == "security" for e in res["errors"])
    assert any("búfer" in e["message"] for e in res["errors"])


SAFE_ARRAY_USES = [
    "import numpy as np\nx = np.ndarray((3,))",  # no buffer: uninitialised float array
    "import numpy as np\nx = np.ndarray(shape=(2,), dtype='f8')",  # shape/dtype only, no buffer kwarg
    "import numpy as np\nx = np.zeros(3)\nok = isinstance(x, np.ndarray)",  # type as argument, not callee
    "import numpy as np\ny = np.array([1.0, 2.0, 3.0])\nz = y.reshape(3, 1)",
]


@pytest.mark.parametrize("code", SAFE_ARRAY_USES)
def test_array_use_without_a_buffer_still_compiles(code):
    res = compile_script("indicator('ok')\n" + code + "\n")
    assert res["ok"], res["errors"]


def test_run_script_blocks_buffer_ndarray_without_launching_the_worker(monkeypatch):
    def boom(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("the buffer exploit reached the sandbox worker")

    monkeypatch.setattr(sandbox, "_launch", boom)
    candles = generate_candleset(n=40, seed=2, tf="1h", end_time=1_760_000_400).to_list()
    res = sandbox.run_script("import numpy as np\nnp.ndarray((8,), 'O', bytes([255] * 64))\n", candles)
    assert res["ok"] is False
    assert res["errors"][0]["kind"] == "security"
