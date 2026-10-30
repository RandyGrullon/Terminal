"""PyChart sandbox security: static rejection of dangerous constructs, invalid code never runs, and the
runtime layers (restricted builtins, import hook, module proxies, audit hook, OS limits) hold even when the AST
validation is bypassed (test-only flag)."""
from __future__ import annotations

import pytest

from app.scripting import sandbox
from app.scripting.runtime_base import SANDBOX_TEST_ENV
from app.scripting.sandbox import SANDBOX_DIR, execute_payload, run_script
from app.scripting.validator import compile_script
from app.sim.synthetic import generate_candleset

FORBIDDEN = [
    ("import os", 1),
    ("from os import path", 1),
    ("import subprocess as sp", 1),
    ("import numpy.lib", 1),
    ("x = (1).__class__.__bases__[0].__subclasses__()", 1),
    ("y = 2\nz = getattr(close, 'size')", 2),
    ("open('notas.txt', 'w')", 1),
    ("eval('1 + 1')", 1),
    ("exec('x = 1')", 1),
    ("compile('1', 'f', 'eval')", 1),
    ("m = __import__('os')", 1),
    ("import numpy as np\nnp.load('datos.npy')", 2),
    ("import numpy as np\nnp.save('x.npy', close)", 2),
    ("close.tofile('x.bin')", 1),
    ("x = close.ctypes", 1),
    ("import numpy as np\nv = np.lib.stride_tricks", 2),
    ("s = '{0.__class__}'.format(1)", 1),
    ("s = '{x}'.format_map({'x': 1})", 1),
    ("g = globals()", 1),
    ("l = locals()", 1),
    ("v = vars()", 1),
    ("t = type(close)", 1),
    ("o = object()", 1),
    ("class A:\n    pass", 1),
    ("import statistics\nstatistics.sys.modules", 2),
    ("x = close._private", 1),
    ("f = lambda: 0\nc = f.__code__", 2),
    ("def g():\n    yield 1", 2),
    ("async def f():\n    pass", 1),
    ("with ctx as c:\n    pass", 1),
    ("def f():\n    global q\n    q = 1", 2),
    ("from numpy import *", 1),
    ("from math import *", 1),
    ("breakpoint()", 1),
    ("help(ta)", 1),
    ("exit()", 1),
    ("i = id(close)", 1),
    ("d = dir(ta)", 1),
    ("setattr(ta, 'x', 1)", 1),
    ("delattr(ta, 'sma')", 1),
    ("m = memoryview(b'x')", 1),
    ("s = super()", 1),
    ("def f(gen):\n    return gen.gi_frame", 2),
    ("def f(e):\n    return e.tb_frame.f_globals", 2),
    ("print(__builtins__)", 1),
]


@pytest.mark.parametrize("code, line", FORBIDDEN)
def test_forbidden_construct_is_rejected(code, line):
    res = compile_script("indicator('x')\n" + code + "\n")
    assert res["ok"] is False
    assert res["errors"] and all(e["kind"] == "security" for e in res["errors"])
    assert line + 1 in {e["line"] for e in res["errors"]}
    assert all(e["message"] for e in res["errors"])


@pytest.mark.parametrize(
    "code",
    [
        "import numpy as np\nimport math\nimport statistics\nfrom numpy import linalg\nimport numpy.random as npr",
        "x = '%s y %d' % ('a', 1)\ny = f'{close[-1]:.2f}'\nz = format(3.14159, '.2f')",
        "o = open\nplot(open, 'apertura')\nd = {'id': 1}\nstrategy.long",
        "def f(a, b=2, *args, **kw):\n    return [v * b for v in args if v] + sorted(kw)\nf(1, 2, 3)",
        "try:\n    1 / 0\nexcept ZeroDivisionError as e:\n    msg = str(e)",
    ],
)
def test_allowed_constructs_compile(code):
    res = compile_script("indicator('ok')\n" + code + "\n")
    assert res["ok"], res["errors"]


def test_every_violation_is_reported_with_its_line():
    code = "import os\nx = 1\ny = close.__class__\neval('2')\nclass K:\n    pass\nimport socket\n"
    res = compile_script(code)
    assert {e["line"] for e in res["errors"]} == {1, 3, 4, 5, 7}
    assert len(res["errors"]) == 5


@pytest.fixture(scope="module")
def candles():
    return generate_candleset(n=80, seed=2, tf="1h", end_time=1_760_000_400).to_list()


def test_run_script_never_launches_invalid_code(candles, monkeypatch):
    def boom(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("invalid code reached the sandbox")

    monkeypatch.setattr(sandbox, "_launch", boom)
    res = run_script("import os\nos.system('echo hola')\n", candles)
    assert res["ok"] is False and res["errors"][0]["kind"] == "security" and res["errors"][0]["line"] == 1
    assert res["plots"] == [] and res["strategy"] is None and len(res["candles"]) == 80


# ----------------------------------------------------------------------------- runtime layers (AST bypassed)


def _payload(code: str, candles: list[dict], skip: bool = True) -> dict:
    return {
        "code": code,
        "time": [c["time"] for c in candles],
        "open": [c["open"] for c in candles],
        "high": [c["high"] for c in candles],
        "low": [c["low"] for c in candles],
        "close": [c["close"] for c in candles],
        "volume": [c["volume"] for c in candles],
        "inputs": {},
        "symbol": "SIM:NEXUS",
        "tf": "1h",
        "skip_validation": skip,
    }


def _unvalidated(code: str, candles: list[dict]) -> dict:
    return execute_payload(_payload(code, candles), 20.0, extra_env={SANDBOX_TEST_ENV: "1"})


FIND_CLASS = (  # classic escape: walk every subclass of object looking for a dangerous class
    "def find(name):\n"
    "    todo = [().__class__.__base__]\n"
    "    while todo:\n"
    "        c = todo.pop()\n"
    "        if c.__name__ == name:\n"
    "            return c\n"
    "        try:\n"
    "            todo.extend(c.__subclasses__())\n"
    "        except TypeError:\n"
    "            pass\n"
)
REAL_BUILTINS = FIND_CLASS + "rb = find('_wrap_close').__init__.__globals__['__builtins__']\n"


def test_audit_hook_blocks_file_writes_when_validation_is_bypassed(candles):
    target = SANDBOX_DIR / "pwned_fileio.txt"
    target.unlink(missing_ok=True)
    res = _unvalidated(FIND_CLASS + "f = find('FileIO')('pwned_fileio.txt', 'w')\nf.write(b'x')\n", candles)
    assert res["ok"] is False
    err = res["errors"][0]
    assert err["kind"] == "security" and "open" in err["message"] and err["line"] == 11
    assert not target.exists()
    res = _unvalidated(REAL_BUILTINS + "rb['open']('pwned_open.txt', 'w').write('x')\n", candles)
    assert res["errors"][0]["kind"] == "security" and not (SANDBOX_DIR / "pwned_open.txt").exists()


def test_audit_hook_blocks_process_creation(candles):
    target = SANDBOX_DIR / "pwned_system.txt"
    target.unlink(missing_ok=True)
    res = _unvalidated(FIND_CLASS + "find('_wrap_close').__init__.__globals__['system']('echo x > pwned_system.txt')\n", candles)
    assert res["errors"][0]["kind"] == "security" and "os.system" in res["errors"][0]["message"]
    assert not target.exists()
    res = _unvalidated(REAL_BUILTINS + "sp = rb['__import__']('subprocess')\nsp.run(['cmd', '/c', 'echo hi'])\n", candles)
    assert res["errors"][0]["kind"] == "security"


@pytest.mark.parametrize(
    "snippet, fragment",
    [
        ("rb['__import__']('socket').socket()", "socket"),
        ("rb['__import__']('ctypes').create_string_buffer(8)", "ctypes"),
        ("rb['exec']('y = 1')", ""),
        ("rb['compile']('1 + 1', 's', 'eval')", "compile"),
        ("rb['__import__']('shutil').rmtree('x')", ""),
    ],
)
def test_audit_hook_blocks_dangerous_operations(candles, snippet, fragment):
    res = _unvalidated(REAL_BUILTINS + snippet + "\n", candles)
    assert res["ok"] is False
    assert res["errors"][0]["kind"] == "security", res["errors"]
    assert fragment in res["errors"][0]["message"]


@pytest.mark.parametrize(
    "code, fragment",
    [
        ("import os", "Importación no permitida"),
        ("import statistics\nx = statistics.sys", "sys"),
        ("import numpy as np\nx = np.lib", "lib"),
        ("import numpy as np\nx = np.load", "load"),
        ("x = np.random.mtrand", "mtrand"),
        ("from numpy import load", "load"),
    ],
)
def test_import_hook_and_module_proxies_hold_without_validation(candles, code, fragment):
    res = _unvalidated(code + "\n", candles)
    assert res["ok"] is False and res["errors"][0]["kind"] == "security"
    assert fragment in res["errors"][0]["message"]


def test_restricted_builtins_without_validation(candles):
    res = _unvalidated("x = eval('1')\n", candles)
    assert res["errors"][0]["kind"] == "runtime" and "eval" in res["errors"][0]["message"]
    res = _unvalidated("class A:\n    pass\n", candles)
    assert res["ok"] is False


def test_swallowed_violation_is_still_blocked_and_reported(candles):
    code = REAL_BUILTINS + "try:\n    rb['open']('pwned_swallow.txt', 'w')\nexcept Exception:\n    pass\nindicator('ok')\nplot(close)\n"
    res = _unvalidated(code, candles)
    assert res["ok"] is True and "bloqueó" in res["stdout"] and "open" in res["stdout"]
    assert not (SANDBOX_DIR / "pwned_swallow.txt").exists()


def test_skip_validation_flag_is_ignored_outside_tests(candles):
    res = execute_payload(_payload("import os\n", candles), 20.0)
    assert res["ok"] is False and res["errors"][0]["kind"] == "security" and res["errors"][0]["line"] == 1
    assert "Solo se permiten" in res["errors"][0]["message"]


def test_memory_is_capped(candles):
    res = run_script("indicator('m')\nx = np.ones(500_000_000)\n", candles)
    assert res["ok"] is False and res["errors"][0]["line"] == 2
    assert "Memoria insuficiente" in res["errors"][0]["message"]
