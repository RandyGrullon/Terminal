"""Regression (review round, pychart-sandbox-escape-numpy-submodule): PyChart sandbox escapes.

- ``from numpy.random import mtrand`` (or ``bit_generator``) used to bind the RAW submodule: an import-hook fast
  path returned ``sys.modules[...]`` unproxied, and plain attribute names (``bit_generator.re.enum.bltns``) then
  reached the real builtins and ``sys``;
- Cython functions (all of numpy.random) expose ``func_globals`` (the module ``__dict__``, with the real
  ``__builtins__``), a non-dunder alias the validator did not know;
- the audit hook read its state from a module global (``_Guard``) that a script holding the real ``sys`` could swap
  through ``sys.modules['__main__'].__dict__``, which switched the hook off.
"""
from __future__ import annotations

import builtins
import collections
import statistics
import sys
import types

import numpy as np
import pytest

from app.scripting import worker
from app.scripting.runtime import Bars, ScriptContext, build_namespace
from app.scripting.runtime_base import SANDBOX_TEST_ENV
from app.scripting.sandbox import SANDBOX_DIR, execute_payload, run_script
from app.scripting.validator import attribute_problem, compile_script
from app.sim.synthetic import generate_candleset

OLD_GAPS = {"func_globals", "func_code", "func_closure", "func_dict", "func_defaults"}


@pytest.fixture(scope="module")
def candles():
    return generate_candleset(n=60, seed=4, tf="1h", end_time=1_760_000_400).to_list()


def _payload(code: str, candles: list[dict]) -> dict:
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
        "skip_validation": True,
    }


def _unvalidated(code: str, candles: list[dict]) -> dict:
    """Runs ``code`` with the AST validation switched off (test-only flag): the runtime layers alone."""
    return execute_payload(_payload(code, candles), 20.0, extra_env={SANDBOX_TEST_ENV: "1"})


# ----------------------------------------------------------------------------- the reported exploit and variants

EXPLOIT = """from numpy.random import mtrand, bit_generator
B = bit_generator.re.enum.bltns
ga = B.getattr
sysm = ga(mtrand.warnings, 'sys')
md = ga(sysm.modules['__main__'], '__dict__')
fake = mtrand.operator
fake.phase = 'setup'
md['_Guard'] = fake
print(B.open('pwned_numpy_submodule.txt', 'w'))
"""


@pytest.mark.parametrize(
    "code",
    [
        EXPLOIT,
        "from numpy.random import mtrand",
        "from numpy.random import bit_generator",
        "from numpy.random import default_rng, mtrand as m",
        "import numpy.random as npr\nx = npr.mtrand",
        "from numpy import random as r\nx = r.bit_generator",
        "g = np.random.default_rng.func_globals\nb = g['__builtins__']",
        "g = np.random.normal.func_globals",  # bound methods forward the alias to the Cython function
        "c = np.random.default_rng.func_code",
        "c = np.random.default_rng.func_closure",
        "d = np.random.default_rng.func_dict",
        "d = np.random.default_rng.func_defaults",
        "m = ta.sma.im_func",
        "o = close.data.obj",
        "f = np.random.default_rng(0).bit_generator.cffi",
    ],
)
def test_escape_routes_fail_with_a_security_error(candles, code):
    res = run_script("indicator('escape')\n" + code + "\n", candles)
    assert res["ok"] is False
    assert res["errors"] and res["errors"][0]["kind"] == "security", res["errors"]
    assert not (SANDBOX_DIR / "pwned_numpy_submodule.txt").exists()


@pytest.mark.parametrize(
    "code, fragment",
    [
        ("from numpy.random import mtrand", "mtrand"),
        ("from numpy.random import bit_generator", "bit_generator"),
        ("from numpy.random import _generator", "_generator"),
        ("from numpy.random import default_rng, mtrand as m", "mtrand"),
        ("import numpy.random.mtrand", "numpy.random.mtrand"),
        (EXPLOIT, "mtrand"),
    ],
)
def test_import_hook_refuses_numpy_submodules_without_validation(candles, code, fragment):
    res = _unvalidated(code, candles)
    assert res["ok"] is False and res["errors"][0]["kind"] == "security", res["errors"]
    assert fragment in res["errors"][0]["message"]
    assert not (SANDBOX_DIR / "pwned_numpy_submodule.txt").exists()


def test_legit_imports_and_numpy_lazy_internals_still_work(candles):
    code = (
        "from numpy.random import default_rng\n"
        "from numpy.linalg import inv, lstsq\n"
        "from numpy.fft import rfft\n"
        "from numpy import linalg\n"
        "import numpy.random as npr\n"
        "from math import sqrt\n"
        "from statistics import fmean, mode\n"
        "indicator('imports')\n"
        "print(round(float(default_rng(1).normal()), 3), inv(np.eye(2))[0][0], len(rfft(close)))\n"
        "print(linalg.det(np.eye(2)), round(float(npr.default_rng(2).random()), 3), sqrt(4.0), mode([1, 1, 2]))\n"
        "print(repr(close.dtype), fmean([1.0, 2.0]))\n"  # numpy imports numpy._core._dtype lazily from C
        "try:\n"
        "    np.add('a', 1)\n"  # the ufunc error class is also imported lazily
        "except TypeError as e:\n"
        "    print('error de tipo', repr(e)[:14])\n"
        "plot(close)\n"
    )
    res = run_script(code, candles)
    assert res["ok"], res["errors"]
    assert "dtype('float64')" in res["stdout"] and "error de tipo UFuncTypeError" in res["stdout"]
    assert "bloqueó" not in res["stdout"]


@pytest.mark.parametrize("name", sorted(OLD_GAPS | {"im_func", "im_self", "im_class", "cell_contents", "obj", "cffi"}))
def test_validator_knows_the_non_dunder_aliases(name):
    assert attribute_problem(name)
    res = compile_script(f"indicator('x')\ny = close.{name}\n")
    assert res["ok"] is False and res["errors"][0]["kind"] == "security" and res["errors"][0]["line"] == 2


# ----------------------------------------------------------------------------- reachability (in process)

_BAD_TYPES = (types.FrameType, types.CodeType, types.TracebackType, types.CellType)


def _problem(value, module_dicts: dict[int, str]) -> str | None:
    """Why ``value`` must never reach a script (None when it is fine)."""
    if isinstance(value, worker.SafeModule):
        return None
    if isinstance(value, types.ModuleType):
        return f"módulo real {value.__name__}"
    if value is builtins.__dict__:
        return "dict de builtins"
    if isinstance(value, dict) and id(value) in module_dicts:
        return f"__dict__ de {module_dicts[id(value)]}"
    if isinstance(value, _BAD_TYPES):
        return type(value).__name__
    return None


def _children(obj, allowed):
    """What a script can get from ``obj`` in one step: allowed attribute names (modules through their proxy) and
    the items of dicts / short sequences."""
    target = object.__getattribute__(obj, "_m") if isinstance(obj, worker.SafeModule) else obj
    try:
        names = sorted(n for n in dir(target) if isinstance(n, str) and allowed(n))
    except Exception:
        names = []
    for name in names:
        try:
            yield "." + name, getattr(obj, name)
        except Exception:  # refused by a proxy, missing, or a property that fails
            continue
    if isinstance(obj, (dict, types.MappingProxyType)):
        for key, value in list(obj.items())[:500]:
            yield f"[{key!r}]", value
    elif isinstance(obj, (list, tuple)) and len(obj) <= 200:
        for i, value in enumerate(obj):
            yield f"[{i}]", value


def _walk(roots: dict, allowed, depth: int = 3) -> tuple[list[str], int]:
    """Breadth-first walk from ``roots`` (up to ``depth`` steps): (forbidden objects found with their path,
    objects visited)."""
    module_dicts = {id(vars(m)): name for name, m in list(sys.modules.items()) if isinstance(m, types.ModuleType)}
    seen = {id(obj): obj for obj in roots.values()}  # keeps every object alive, so ids stay unique
    queue = collections.deque((obj, label, 0) for label, obj in roots.items())
    found: list[str] = []
    while queue:
        obj, path, level = queue.popleft()
        if level >= depth:
            continue
        for step, value in _children(obj, allowed):
            problem = _problem(value, module_dicts)
            if problem:
                found.append(f"{path}{step} -> {problem}")
            elif id(value) not in seen:
                seen[id(value)] = value
                queue.append((value, path + step, level + 1))
    return found, len(seen)


def _roots() -> dict:
    """Every module proxy of the sandbox, objects that common calls return, the PyChart namespace and the safe
    builtins."""
    registry = worker.build_module_registry()
    roots: dict = {f"<{object.__getattribute__(p, '_m').__name__}>": p for p in registry.values()}
    a = np.arange(5.0)
    rng = np.random.default_rng(0)
    roots.update({
        "np.arange(5.0)": a,
        "default_rng(0)": rng,
        "default_rng(0).bit_generator": rng.bit_generator,
        "RandomState(1)": np.random.RandomState(1),
        "SeedSequence(1)": np.random.SeedSequence(1),
        "linalg.svd": np.linalg.svd(np.eye(3)),
        "linalg.lstsq": np.linalg.lstsq(np.eye(2), np.ones(2)),
        "linalg.eig": np.linalg.eig(np.eye(2)),
        "histogram": np.histogram(a),
        "Polynomial": np.polynomial.Polynomial([1.0, 2.0]),
        "masked_invalid": np.ma.masked_invalid(a),
        "finfo": np.finfo(float),
        "errstate": np.errstate(),
        "printoptions": np.printoptions(),
        "nditer": np.nditer(a),
        "statistics.linear_regression": statistics.linear_regression([1.0, 2.0, 3.0], [1.0, 2.0, 4.0]),
        "statistics.NormalDist": statistics.NormalDist(1.0, 2.0),
    })
    ctx = ScriptContext(Bars(np.arange(5), a, a, a, a, a), {})
    roots.update({f"ns.{k}": v for k, v in build_namespace(ctx).items()})
    roots.update({f"builtins.{n}": getattr(builtins, n) for n in worker.SAFE_BUILTIN_NAMES})
    return roots


@pytest.mark.filterwarnings("ignore")
def test_no_module_globals_or_frames_reachable_through_allowed_names():
    found, visited = _walk(_roots(), lambda name: attribute_problem(name) is None)
    assert visited > 5000  # the walk really went through the numpy / statistics / PyChart API
    assert found == [], "\n".join(found[:20])


@pytest.mark.filterwarnings("ignore")
def test_walker_detects_the_cython_alias_escape():
    """The walk above would have caught the reported escape: with the old deny-list it finds module globals."""
    found, _ = _walk(_roots(), lambda name: attribute_problem(name) is None or name in OLD_GAPS, depth=2)
    assert any("default_rng.func_globals -> __dict__ de numpy.random" in path for path in found), found[:5]


# ----------------------------------------------------------------------------- import hook and audit guard units


def test_import_hook_resolves_from_imports_through_the_proxies():
    registry = worker.build_module_registry()
    safe_import = worker.make_import(registry)
    assert safe_import("numpy.random", None, None, ("default_rng",), 0) is registry[id(np.random)]
    for module, item in (("numpy.random", "mtrand"), ("numpy.random", "bit_generator"), ("numpy", "lib"),
                         ("statistics", "sys"), ("numpy", "_core")):
        with pytest.raises(worker.BlockedImport):
            safe_import(module, None, None, (item,), 0)
    with pytest.raises(ImportError) as info:
        safe_import("numpy.random", None, None, ("no_existe",), 0)
    assert not isinstance(info.value, worker.BlockedImport)


def test_import_hook_serves_numpy_lazy_internals_without_handing_them_out():
    safe_import = worker.make_import(worker.build_module_registry())
    # PyImport_Import shape (numpy's C code): empty *list* fromlist, return value ignored -> nothing given back
    assert safe_import("numpy._core._dtype", {}, {}, [], 0) is None
    # the same name from an import statement (tuple / None fromlist), non-numpy or blocked internals: refused
    for fromlist in (("x",), None):
        with pytest.raises(worker.BlockedImport):
            safe_import("numpy._core._dtype", {}, {}, fromlist, 0)
    for name in ("os", "numpy.lib._npyio_impl", "numpy.ctypeslib", "numpy.no_such_module"):
        with pytest.raises(worker.BlockedImport):
            safe_import(name, {}, {}, [], 0)


def test_audit_guard_state_lives_in_its_closure(monkeypatch):
    code = compile("x = 1", "<script>", "exec")
    hook, set_phase, blocked = worker.make_audit_guard(code)
    assert not hasattr(worker, "_Guard") and not hasattr(worker, "_audit")
    # rebinding the module globals after the guard exists changes nothing (the old exploit swapped _Guard)
    monkeypatch.setattr(worker, "SAFE_EVENTS", frozenset({"open", "os.system"}))
    monkeypatch.setattr(worker, "SandboxBlocked", RuntimeError)
    monkeypatch.setattr(worker, "_Guard", types.SimpleNamespace(phase="setup"), raising=False)
    assert hook("builtins.id", (1,)) is None
    assert hook("exec", (code,)) is None
    with pytest.raises(PermissionError):  # the real SandboxBlocked: one exec only, of the script code
        hook("exec", (code,))
    with pytest.raises(PermissionError):
        hook("open", ("secreto.txt", "r", 0))
    with pytest.raises(PermissionError):
        hook("import", ("socket", None, [], [], []))
    with pytest.raises(PermissionError):  # reading frames: only while the worker writes the report
        hook("object.__getattr__", (None, "tb_frame"))
    monkeypatch.undo()
    set_phase("setup")  # unknown phases fail closed
    with pytest.raises(PermissionError):
        hook("object.__getattr__", (None, "tb_frame"))
    set_phase("report")
    assert hook("object.__getattr__", (None, "tb_frame")) is None
    with pytest.raises(PermissionError):
        hook("open", ("secreto.txt", "w", 0))
    assert hook("import", ("numpy._core.x", None, [], [], [])) is None
    assert blocked and blocked[0].startswith("exec")


# ----------------------------------------------------------------------------- audit hook vs. the real builtins

REAL = (  # validation off + Cython alias: the script holds the real builtins and sys
    "B = np.random.default_rng.func_globals['__builtins__']\n"
    "ga = B['getattr'] if isinstance(B, dict) else B.getattr\n"
    "op = B['open'] if isinstance(B, dict) else B.open\n"
    "sysm = (B['__import__'] if isinstance(B, dict) else ga(B, '__import__'))('sys')\n"
)


def test_audit_hook_cannot_be_disarmed_through_the_worker_globals(candles):
    target = SANDBOX_DIR / "pwned_globals.txt"
    target.unlink(missing_ok=True)
    code = REAL + (
        "md = ga(sysm.modules['__main__'], '__dict__')\n"
        "fake = sysm.modules['operator']\n"
        "fake.phase = 'setup'\n"
        "md['_Guard'] = fake\n"
        "md['SAFE_EVENTS'] = frozenset({'open', 'os.listdir', 'socket.__new__'})\n"
        "md['SandboxBlocked'] = ValueError\n"
        "md['make_audit_guard'] = None\n"
        "op('pwned_globals.txt', 'w').write('x')\n"
    )
    res = _unvalidated(code, candles)
    assert res["ok"] is False
    err = res["errors"][0]
    assert err["kind"] == "security" and "open" in err["message"] and err["line"] == 12, res["errors"]
    assert not target.exists()


@pytest.mark.parametrize(
    "snippet, fragment",
    [
        ("f = sysm._getframe()", "sys._getframe"),
        ("try:\n    1 / 0\nexcept ZeroDivisionError as e:\n    f = ga(ga(e, '__traceback__'), 'tb_frame')",
         "object.__getattr__"),
        ("f = ga(ga(sysm.modules['__main__'], 'run_payload'), '__code__')", "object.__getattr__"),
    ],
)
def test_frames_stay_out_of_reach_with_the_real_builtins(candles, snippet, fragment):
    res = _unvalidated(REAL + snippet + "\n", candles)
    assert res["ok"] is False and res["errors"][0]["kind"] == "security", res["errors"]
    assert fragment in res["errors"][0]["message"]
