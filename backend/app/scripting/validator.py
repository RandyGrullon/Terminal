"""PyChart static compiler: syntax check, AST security validation and static extraction of the script
declaration (``indicator()`` / ``strategy()``) and its inputs (``input.*()``).

``compile_script(code) -> dict`` returns a ``CompileResult`` (frontend/src/api/types.ts). It is pure and fast:
it never executes user code and never starts a subprocess.

Security model (first of three layers; the sandbox worker adds restricted builtins / import hook / module
proxies and an audit hook):
- imports: only ``math``, ``statistics`` and ``numpy`` (plus the pure numeric submodules linalg/random/fft);
- no dunder names or attributes, no private (``_x``) attributes, no dangerous builtins (``eval``, ``open``...);
- no attributes that lead to frames/code objects/module globals (including the non-dunder aliases such as the
  Cython ``func_globals``), string formatting with attribute traversal (``format``/``format_map``) or numpy
  file/system APIs;
- no array constructor over a raw memory buffer (``ndarray``/``recarray`` with ``buffer``/``buf``): an object dtype
  over arbitrary bytes dereferences them as pointers and crashes the worker;
- no classes, ``with``, ``global``/``nonlocal``, ``async``/``await`` or generators (``yield``).
Every violation is reported (with its line) so the user can fix them all at once.
"""
from __future__ import annotations

import ast
import re
import threading
import warnings
from typing import Any

MAX_CODE_CHARS = 100_000
MAX_AST_NODES = 60_000
SCRIPT_FILENAME = "<script>"

ALLOWED_IMPORTS = frozenset({"math", "statistics", "numpy", "numpy.linalg", "numpy.random", "numpy.fft"})

# Builtins that give access to the interpreter internals, files or introspection.
FORBIDDEN_NAMES = frozenset({
    "open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "getattr", "setattr",
    "delattr", "breakpoint", "help", "memoryview", "type", "object", "super", "classmethod", "staticmethod",
    "property", "id", "dir", "exit", "quit",
})
# Forbidden builtins whose name is rebound by the PyChart API (``open`` = series of open prices): the name may
# be read, but calling it is rejected (the file builtin never exists inside the sandbox).
API_SERIES_NAMES = frozenset({"open"})

# Attributes that lead to frames / code objects / module globals or allow attribute traversal inside format
# strings. Some C types keep non-dunder aliases of the dunders: Cython functions (all of numpy.random) expose
# func_globals / func_code / func_closure / func_dict / func_defaults (func_globals is the module __dict__, with the
# real __builtins__), im_* are the Python 2 method names, cell_contents opens closures and ``obj`` hands back the
# object behind an AttributeError or a memoryview.
INTROSPECTION_ATTRS = frozenset({
    "format", "format_map", "mro", "gi_frame", "gi_code", "gi_yieldfrom", "f_globals", "f_locals", "f_back",
    "f_code", "f_builtins", "f_trace", "f_generator", "tb_frame", "tb_next", "cr_frame", "cr_code", "cr_await",
    "ag_frame", "ag_code", "ag_await", "co_code", "co_consts", "co_names", "func_globals", "func_code",
    "func_closure", "func_dict", "func_defaults", "func_kwdefaults", "func_builtins", "im_func", "im_self",
    "im_class", "cell_contents", "obj",
})

# numpy (and generic) file / system / foreign-memory APIs.
SYSTEM_ATTRS = frozenset({
    "load", "save", "savez", "savez_compressed", "savetxt", "loadtxt", "fromfile", "tofile", "memmap",
    "open_memmap", "genfromtxt", "lib", "ctypeslib", "f2py", "testing", "distutils", "DataSource", "frombuffer",
    "fromregex", "ctypes", "cffi", "capsule", "dump", "dumps", "show_config", "show_runtime", "info",
})

# Module objects re-exported by allowed modules (e.g. ``statistics.sys``) must never be reachable.
MODULE_ATTRS = frozenset({
    "sys", "os", "builtins", "subprocess", "importlib", "shutil", "socket", "marshal", "pickle", "gc", "inspect",
    "io", "pathlib", "signal", "threading", "multiprocessing", "posix", "nt", "winreg", "codecs", "types",
    "weakref", "linecache", "tempfile", "glob", "runpy", "pty", "mmap", "faulthandler",
})

FORBIDDEN_ATTRS = INTROSPECTION_ATTRS | SYSTEM_ATTRS | MODULE_ATTRS

# Array constructors that can alias a raw memory buffer. With an object (or structured-object) dtype they treat the
# bytes of that buffer as Python-object pointers and dereference them, which crashes the worker (and could be a
# fake-object primitive). ``np.frombuffer`` (the normal buffer view) is already blocked via SYSTEM_ATTRS, so a buffer
# argument to these is rejected too. The buffer is the 3rd positional argument (ndarray: shape, dtype, buffer;
# recarray: shape, dtype, buf) or the ``buffer`` / ``buf`` keyword. A plain ``np.ndarray((3,))`` (no buffer) and
# ``isinstance(x, np.ndarray)`` (the type is an argument, not the callee) stay allowed.
BUFFER_ARRAY_CTORS = frozenset({"ndarray", "recarray"})
BUFFER_KWARGS = frozenset({"buffer", "buf"})

# input.*() signatures (positional order) shared by the static extractor and the runtime.
INPUT_SIGNATURES: dict[str, tuple[str, ...]] = {
    "int": ("defval", "title", "minval", "maxval", "step", "options"),
    "float": ("defval", "title", "minval", "maxval", "step", "options"),
    "bool": ("defval", "title"),
    "string": ("defval", "title", "options"),
    "select": ("defval", "title", "options"),
    "source": ("defval", "title"),
}
SOURCE_NAMES = ("close", "open", "high", "low", "hl2", "hlc3", "ohlc4", "volume")
INPUT_KEY_KW = "_nexus_key"  # hidden keyword injected by the worker so overrides are matched by name

DEFAULT_TITLE = "Script sin título"
ERROR_MESSAGE_MAX = 4000  # a script must not be able to push a huge message through the API / events

_compile_lock = threading.Lock()


def clip_text(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters (with a Spanish note saying how much was left out)."""
    if len(text) <= limit:
        return text
    more = f"{len(text) - limit:,}".replace(",", " ")
    return f"{text[:limit]}… (mensaje truncado, {more} caracteres más)"


def make_error(kind: str, message: str, line: int | None = None, col: int | None = None) -> dict:
    """ScriptError dict (line and col are 1-based; None when unknown; long messages are truncated)."""
    return {
        "line": int(line) if isinstance(line, int) and line > 0 else None,
        "col": int(col) if isinstance(col, int) and col > 0 else None,
        "message": clip_text(str(message), ERROR_MESSAGE_MAX),
        "kind": kind,
    }


def is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def attribute_problem(attr: str) -> str | None:
    """Spanish reason why accessing ``obj.<attr>`` is forbidden (None when allowed)."""
    if is_dunder(attr):
        return f"Acceso al atributo especial '{attr}' no permitido."
    if attr.startswith("_"):
        return f"Acceso al atributo privado '{attr}' no permitido."
    if attr in INTROSPECTION_ATTRS:
        return f"El atributo '{attr}' no está permitido (introspección / formato con atributos)."
    if attr in SYSTEM_ATTRS:
        return f"'{attr}' no está permitido: PyChart no puede acceder a archivos, memoria ni al sistema."
    if attr in MODULE_ATTRS:
        return f"El acceso al módulo '{attr}' no está permitido."
    return None


class _SecurityVisitor(ast.NodeVisitor):
    """Collects every security violation of a parsed script."""

    def __init__(self) -> None:
        self.errors: list[dict] = []
        self._seen: set[tuple[Any, Any, str]] = set()

    def _add(self, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", None)
        col = getattr(node, "col_offset", None)
        col = col + 1 if isinstance(col, int) else None
        key = (line, col, message)
        if key not in self._seen:
            self._seen.add(key)
            self.errors.append(make_error("security", message, line, col))

    def _check_identifier(self, node: ast.AST, name: str | None, *, builtin_lookup: bool = False) -> None:
        if not name:
            return
        if is_dunder(name):
            self._add(node, f"Los nombres especiales como '{name}' no están permitidos.")
        elif builtin_lookup and name in FORBIDDEN_NAMES and name not in API_SERIES_NAMES:
            self._add(node, f"El uso de '{name}' no está permitido en PyChart.")

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in API_SERIES_NAMES:
            self._add(
                node,
                f"{func.id}() no está permitido: PyChart no puede abrir archivos ('{func.id}' es la serie de precios).",
            )
        ctor = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
        if ctor in BUFFER_ARRAY_CTORS and (
            len(node.args) >= 3 or any(kw.arg in BUFFER_KWARGS for kw in node.keywords)
        ):
            self._add(
                node,
                f"No se permite crear un arreglo '{ctor}' a partir de un búfer de memoria: PyChart no puede "
                "interpretar memoria arbitraria como arreglo.",
            )
        self.generic_visit(node)

    def _check_attr(self, node: ast.AST, attr: str) -> None:
        problem = attribute_problem(attr)
        if problem:
            self._add(node, problem)

    # -- imports
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name not in ALLOWED_IMPORTS:
                self._add(node, f"Importación no permitida: '{alias.name}'. Solo se permiten math, statistics y numpy.")
            self._check_identifier(node, alias.asname)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            self._add(node, "Las importaciones relativas no están permitidas.")
        elif module not in ALLOWED_IMPORTS:
            self._add(node, f"Importación no permitida: '{module}'. Solo se permiten math, statistics y numpy.")
        for alias in node.names:
            if alias.name == "*":
                self._add(node, "La importación con '*' no está permitida; importa nombres concretos.")
            else:
                self._check_attr(node, alias.name)
            self._check_identifier(node, alias.asname)

    # -- names and attributes
    def visit_Name(self, node: ast.Name) -> None:
        self._check_identifier(node, node.id, builtin_lookup=not isinstance(node.ctx, ast.Store))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._check_attr(node, node.attr)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_identifier(node, node.name)
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        self._check_identifier(node, node.arg)
        self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> None:
        self._check_identifier(node, node.arg)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._check_identifier(node, node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        self._check_identifier(node, node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        self._check_identifier(node, node.name)
        self.generic_visit(node)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        self._check_identifier(node, node.rest)
        self.generic_visit(node)

    # -- forbidden statements
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add(node, "No se permiten definiciones de clase (class).")
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        self._add(node, "La sentencia 'with' no está permitida.")
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self._add(node, "La sentencia 'global' no está permitida.")

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self._add(node, "La sentencia 'nonlocal' no está permitida.")

    def _async(self, node: ast.AST) -> None:
        self._add(node, "El código asíncrono (async / await) no está permitido.")
        self.generic_visit(node)

    visit_AsyncFunctionDef = _async
    visit_AsyncFor = _async
    visit_AsyncWith = _async
    visit_Await = _async

    def _generator(self, node: ast.AST) -> None:
        self._add(node, "Los generadores (yield) no están permitidos.")
        self.generic_visit(node)

    visit_Yield = _generator
    visit_YieldFrom = _generator


def find_security_violations(tree: ast.AST) -> list[dict]:
    """Every security violation of the parsed script, sorted by position."""
    visitor = _SecurityVisitor()
    visitor.visit(tree)
    return sorted(visitor.errors, key=lambda e: (e["line"] or 0, e["col"] or 0))


# --- end of section A: security ---


# ============================================================================= static declarations

_MISSING = object()


def _literal(node: ast.AST | None) -> Any:
    if node is None:
        return _MISSING
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return _MISSING


def _position(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "lineno", 0) or 0, getattr(node, "col_offset", 0) or 0)


def _bind_args(call: ast.Call, names: tuple[str, ...]) -> dict[str, ast.expr]:
    """Maps positional and keyword arguments of a call to parameter names (static, best effort)."""
    out: dict[str, ast.expr] = {}
    for i, arg in enumerate(call.args):
        if isinstance(arg, ast.Starred) or i >= len(names):
            break
        out[names[i]] = arg
    for kw in call.keywords:
        if kw.arg:
            out[kw.arg] = kw.value
    return out


def extract_meta(tree: ast.AST) -> tuple[dict, list[str]]:
    """ScriptMetaInfo from the first ``indicator()`` / ``strategy()`` call (source order)."""
    warns: list[str] = []
    decls = sorted(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("indicator", "strategy")
        ),
        key=_position,
    )
    meta = {"title": DEFAULT_TITLE, "overlay": True, "kind": "indicator"}
    if not decls:
        warns.append("No se encontró indicator(...) ni strategy(...): se usará un indicador superpuesto sin título.")
        if _uses_strategy_orders(tree):
            warns.append("El script usa strategy.entry/exit/close pero no declara strategy(...).")
        return meta, warns
    first = decls[0]
    if len(decls) > 1:
        warns.append(f"Línea {decls[1].lineno}: declaración repetida; se usa la de la línea {first.lineno}.")
    bound = _bind_args(first, ("title", "overlay"))
    title = _literal(bound.get("title"))
    if isinstance(title, str) and title.strip():
        meta["title"] = title.strip()[:120]
    elif "title" in bound:
        warns.append(f"Línea {first.lineno}: el título debe ser un texto literal.")
    overlay = _literal(bound.get("overlay"))
    if isinstance(overlay, bool):
        meta["overlay"] = overlay
    meta["kind"] = "strategy" if first.func.id == "strategy" else "indicator"  # type: ignore[union-attr]
    if meta["kind"] == "indicator" and _uses_strategy_orders(tree):
        warns.append("El script usa strategy.entry/exit/close pero no declara strategy(...).")
    return meta, warns


def _uses_strategy_orders(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "strategy"
            and node.attr in ("entry", "exit", "close", "close_all")
        ):
            return True
    return False


def _input_kind(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "input":
        return func.attr if func.attr in INPUT_SIGNATURES else None
    if isinstance(func, ast.Name) and func.id == "input":
        return "auto"
    return None


def _bind_target(target: ast.AST, value: ast.AST, names: dict[int, str]) -> None:
    if isinstance(target, ast.Name) and isinstance(value, ast.Call):
        names[id(value)] = target.id
    elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
        if len(target.elts) == len(value.elts):
            for t, v in zip(target.elts, value.elts):
                _bind_target(t, v, names)


def _assigned_names(tree: ast.AST) -> dict[int, str]:
    """id(call node) -> variable it is assigned to (``length = input.int(14)`` -> "length")."""
    names: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and node.targets:
            _bind_target(node.targets[0], node.value, names)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            _bind_target(node.target, node.value, names)
        elif isinstance(node, ast.NamedExpr):
            _bind_target(node.target, node.value, names)
    return names


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if value == value and abs(value) != float("inf") else None


def coerce_input_value(kind: str, value: Any) -> tuple[Any, bool]:
    """(value converted to the input type, ok). Shared by the compiler (defaults) and the runtime (overrides)."""
    if kind == "int":
        num = _number(value)
        if num is None and isinstance(value, str):
            num = _number(_parse_number(value))
        return (int(round(num)), True) if num is not None else (None, False)
    if kind == "float":
        num = _number(value)
        if num is None and isinstance(value, str):
            num = _number(_parse_number(value))
        return (float(num), True) if num is not None else (None, False)
    if kind == "bool":
        if isinstance(value, bool):
            return value, True
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "si", "sí", "no"):
            return value.strip().lower() in ("true", "1", "si", "sí"), True
        return (bool(value), True) if _number(value) in (0, 1) else (None, False)
    if isinstance(value, str):
        return value, True
    num = _number(value)
    return (str(value), True) if num is not None else (None, False)


def _parse_number(text: str) -> float | None:
    try:
        return float(text.strip().replace(",", "."))
    except ValueError:
        return None


def _auto_kind(default_node: ast.AST | None, default: Any) -> str:
    if isinstance(default_node, ast.Name) and default_node.id in SOURCE_NAMES:
        return "source"
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, float):
        return "float"
    return "string"


def _build_input(call: ast.Call, kind: str, name: str, warns: list[str]) -> dict:
    line = call.lineno
    if kind == "auto":
        default_node = _bind_args(call, ("defval", "title")).get("defval")
        kind = _auto_kind(default_node, _literal(default_node))
    bound = _bind_args(call, INPUT_SIGNATURES[kind])
    title = _literal(bound.get("title"))
    spec: dict[str, Any] = {
        "name": name,
        "title": title.strip()[:80] if isinstance(title, str) and title.strip() else name,
        "type": kind,
        "default": None,
    }
    if kind == "source":
        node = bound.get("defval")
        src = node.id if isinstance(node, ast.Name) else _literal(node)
        if src not in SOURCE_NAMES:
            if node is not None:
                warns.append(f"Línea {line}: input.source('{spec['title']}') usará 'close' como valor por defecto.")
            src = "close"
        spec["default"] = src
        spec["options"] = list(SOURCE_NAMES)
        return spec
    raw = _literal(bound.get("defval"))
    if raw is _MISSING:
        warns.append(f"Línea {line}: el valor por defecto de '{spec['title']}' no es un literal; no se podrá editar.")
    else:
        value, ok = coerce_input_value(kind, raw)
        if ok:
            spec["default"] = value
        else:
            warns.append(f"Línea {line}: el valor por defecto de '{spec['title']}' no es de tipo {kind}.")
    for key, out_key in (("minval", "min"), ("maxval", "max"), ("step", "step")):
        num = _number(_literal(bound.get(key)))
        if num is not None:
            spec[out_key] = num
    options = _literal(bound.get("options"))
    if isinstance(options, (list, tuple)) and options:
        spec["options"] = [str(o) for o in options][:200]
    elif kind == "select":
        warns.append(f"Línea {line}: input.select('{spec['title']}') necesita una lista literal en options.")
    return spec


def extract_inputs(tree: ast.AST) -> tuple[list[tuple[ast.Call, dict]], list[str]]:
    """(call node, ScriptInput) for every input declaration in source order, with unique names."""
    warns: list[str] = []
    assigned = _assigned_names(tree)
    found = [(node, _input_kind(node)) for node in ast.walk(tree) if isinstance(node, ast.Call)]
    calls = sorted(((node, kind) for node, kind in found if kind), key=lambda item: _position(item[0]))
    out: list[tuple[ast.Call, dict]] = []
    used: set[str] = set()
    for index, (call, kind) in enumerate(calls, start=1):
        base = assigned.get(id(call)) or f"input_{index}"
        name, k = base, 2
        while name in used:
            name, k = f"{base}_{k}", k + 1
        if name != base:
            warns.append(f"Línea {call.lineno}: la entrada '{base}' está repetida; se renombra a '{name}'.")
        used.add(name)
        out.append((call, _build_input(call, kind, name, warns)))  # type: ignore[arg-type]
    return out, warns


def annotate_input_keys(tree: ast.Module) -> ast.Module:
    """Adds the hidden ``_nexus_key="<name>"`` keyword to every input call, so the runtime matches the user's
    overrides with exactly the names reported by the compiler (used by the sandbox worker)."""
    for call, spec in extract_inputs(tree)[0]:
        call.keywords = [kw for kw in call.keywords if kw.arg != INPUT_KEY_KW]
        kw = ast.keyword(arg=INPUT_KEY_KW, value=ast.Constant(spec["name"]))
        ast.copy_location(kw, call)
        ast.copy_location(kw.value, call)
        call.keywords.append(kw)
    return ast.fix_missing_locations(tree)


# --- end of section B: declarations ---


# ============================================================================= parse / compile

_SYNTAX_TRANSLATIONS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), repl)
    for pattern, repl in (
        (r"^invalid syntax\. Perhaps you forgot a comma\?$", "sintaxis inválida. ¿Olvidaste una coma?"),
        (r"^invalid syntax\. Maybe you meant '==' or ':=' instead of '='\?$", "sintaxis inválida. ¿Querías usar '=='?"),
        (r"^invalid syntax$", "sintaxis inválida"),
        (r"^expected '(.+)'$", r"se esperaba '\1'"),
        (r"^unexpected indent$", "sangría inesperada"),
        (r"^unindent does not match any outer indentation level$", "la sangría no coincide con ningún nivel exterior"),
        (r"^expected an indented block.*$", "se esperaba un bloque con sangría"),
        (r"^unterminated triple-quoted string literal.*$", "texto con triple comilla sin cerrar"),
        (r"^unterminated string literal.*$", "texto (string) sin cerrar"),
        (r"^'(.)' was never closed$", r"'\1' nunca se cerró"),
        (r"^unmatched '(.)'$", r"'\1' no tiene pareja"),
        (r"^closing parenthesis '(.)' does not match opening parenthesis '(.)'.*$", r"'\1' no corresponde con '\2'"),
        (r"^'return' outside function$", "'return' fuera de una función"),
        (r"^'break' outside loop$", "'break' fuera de un bucle"),
        (r"^'continue' not properly in loop$", "'continue' fuera de un bucle"),
        (r"^cannot assign to (.+?)( here.*)?$", r"no se puede asignar a \1"),
        (r"^invalid decimal literal$", "número inválido"),
        (r"^unexpected EOF while parsing$", "fin de código inesperado"),
        (r"^unexpected character after line continuation character$", "carácter inesperado después de '\\\\'"),
        (r"^inconsistent use of tabs and spaces in indentation$", "mezcla de tabuladores y espacios en la sangría"),
        (r"^Missing parentheses in call to '(.+?)'.*$", r"faltan paréntesis en la llamada a '\1'"),
        (r"^positional argument follows keyword argument$", "argumento posicional después de un argumento con nombre"),
        (r"^keyword argument repeated: (.+)$", r"argumento con nombre repetido: \1"),
        (r"^duplicate argument '(.+)' in function definition$", r"argumento '\1' duplicado en la función"),
    )
)


def _syntax_message(exc: SyntaxError) -> str:
    raw = str(exc.msg or "invalid syntax").strip()
    for pattern, repl in _SYNTAX_TRANSLATIONS:
        if pattern.match(raw):
            return "Error de sintaxis: " + pattern.sub(repl, raw)
    return f"Error de sintaxis: {raw}"


def _syntax_error(exc: SyntaxError) -> dict:
    return make_error("syntax", _syntax_message(exc), exc.lineno, exc.offset)


def _warning_text(w: warnings.WarningMessage) -> str:
    text = str(w.message)
    match = re.match(r'^"?invalid escape sequence (.+?)"?$', text)
    if match:
        text = f"secuencia de escape inválida {match.group(1)} (usa un texto r'...')"
    return f"Línea {w.lineno}: aviso de Python: {text}" if w.lineno else f"Aviso de Python: {text}"


def parse_script(code: Any) -> tuple[ast.Module | None, list[dict], list[str]]:
    """Parses (never executes) a script: (tree or None, errors, warnings)."""
    if not isinstance(code, str):
        return None, [make_error("syntax", "El código del script debe ser texto.")], []
    if len(code) > MAX_CODE_CHARS:
        return None, [make_error("syntax", f"El script es demasiado largo (máximo {MAX_CODE_CHARS:,} caracteres).")], []
    if not code.strip():
        return None, [make_error("syntax", "El script está vacío.")], []
    try:
        with _compile_lock, warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            tree = ast.parse(code, filename=SCRIPT_FILENAME, mode="exec")
    except SyntaxError as exc:
        return None, [_syntax_error(exc)], []
    except ValueError:
        return None, [make_error("syntax", "El código contiene caracteres no válidos (bytes nulos).")], []
    except (MemoryError, RecursionError):
        return None, [make_error("syntax", "El script está anidado en exceso o es demasiado complejo.")], []
    warns = [_warning_text(w) for w in caught]
    if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
        return None, [make_error("syntax", "El script es demasiado complejo (demasiadas instrucciones).")], warns
    return tree, [], warns


def compile_tree(tree: ast.Module) -> tuple[Any, list[dict], list[str]]:
    """Bytecode for a validated tree: (code object or None, errors, warnings). Catches compiler-stage errors
    such as ``return`` outside a function."""
    try:
        with _compile_lock, warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            code_obj = compile(tree, SCRIPT_FILENAME, "exec", dont_inherit=True)
    except SyntaxError as exc:
        return None, [_syntax_error(exc)], []
    except (ValueError, TypeError, MemoryError, RecursionError, OverflowError):
        return None, [make_error("syntax", "El script no se pudo compilar (demasiado complejo).")], []
    return code_obj, [], [_warning_text(w) for w in caught]


def compile_script(code: str) -> dict:
    """CompileResult: syntax + security validation and static meta/inputs. Pure: never runs the script."""
    result: dict[str, Any] = {
        "ok": False,
        "errors": [],
        "warnings": [],
        "inputs": [],
        "meta": {"title": DEFAULT_TITLE, "overlay": True, "kind": "indicator"},
    }
    tree, errors, warns = parse_script(code)
    result["warnings"].extend(warns)
    if tree is None:
        result["errors"] = errors
        return result
    meta, meta_warns = extract_meta(tree)
    specs, input_warns = extract_inputs(tree)
    result["meta"] = meta
    result["inputs"] = [spec for _, spec in specs]
    result["warnings"].extend(meta_warns + input_warns)
    violations = find_security_violations(tree)
    if violations:
        result["errors"] = violations
        return result
    _, errors, compile_warns = compile_tree(tree)
    result["warnings"].extend(w for w in compile_warns if w not in result["warnings"])
    if errors:
        result["errors"] = errors
        return result
    result["ok"] = True
    return result


def build_code(code: str, *, validate: bool = True) -> tuple[Any, list[dict]]:
    """Code object ready to exec in the sandbox (inputs annotated with their names): (code or None, errors).

    ``validate=False`` skips the AST security check; only the sandbox tests use it to prove that the runtime
    layers (restricted builtins, import hook, audit hook) hold on their own.
    """
    tree, errors, _ = parse_script(code)
    if tree is None:
        return None, errors
    if validate:
        violations = find_security_violations(tree)
        if violations:
            return None, violations
    annotate_input_keys(tree)
    code_obj, errors, _ = compile_tree(tree)
    return code_obj, errors
