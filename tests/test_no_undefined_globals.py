"""STATIC GUARD AGAINST THE BUG CLASS THAT KEEPS STOPPING THIS WORLD.

Python resolves globals at CALL time, so a function may reference a name that does not exist, import
cleanly, pass every test that never calls it — and then fail in production. That has now happened twice:

  * 2026-09-12 `_needs_you` used COLONY_CAP, which was never imported → /observe returned 500 for every
    agent in the world.
  * 2026-09-17 `settle_geese` used bare `W` / `H` (engine.py has no such globals; dimensions come from
    `_world_dims`) → the tick raised NameError on EVERY tick, rolled back, and the world stood still for
    ~20 minutes until the pod crash-looped its way to someone noticing.

Both were syntactically perfect, both imported fine, both shipped green. So this walks the AST of every
module the ConfigMaps ship and asserts that every name a function reads is bound somewhere it can see:
its own scope, an enclosing function, module level, or builtins. No imports, no database, milliseconds.
"""
import ast
import builtins
import glob
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")
TARGETS = ["engine/*.py", "server/*.py", "agents/*.py"]

_BUILTIN = set(dir(builtins)) | {"__name__", "__file__", "__doc__", "__spec__", "__package__", "__builtins__"}


def _bind(target, out):
    """Names bound by an assignment/for/with target (recursing through tuple, list and star targets)."""
    if isinstance(target, ast.Name):
        out.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for el in target.elts:
            _bind(el, out)
    elif isinstance(target, ast.Starred):
        _bind(target.value, out)


def _args_of(argnode, out):
    for a in list(argnode.posonlyargs) + list(argnode.args) + list(argnode.kwonlyargs):
        out.add(a.arg)
    if argnode.vararg:
        out.add(argnode.vararg.arg)
    if argnode.kwarg:
        out.add(argnode.kwarg.arg)


def _scope_of(fn):
    """Everything bound inside one function. Over-approximates on purpose: a nested def's and lambda's
    parameters are folded in too (they are checked again in their own pass), so the walk never has to
    reason about which statements belong to which nested body. Over-approximating can only MISS a bug,
    never invent one — exactly the right direction for a guard that must not cry wolf."""
    out = set()
    _args_of(fn.args, out)
    for n in ast.walk(fn):
        if n is fn:
            continue
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(n.name); _args_of(n.args, out)
        elif isinstance(n, ast.ClassDef):
            out.add(n.name)
        elif isinstance(n, ast.Lambda):
            _args_of(n.args, out)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                _bind(t, out)
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)):
            _bind(n.target, out)
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            _bind(n.target, out)
        elif isinstance(n, ast.withitem):
            if n.optional_vars is not None:
                _bind(n.optional_vars, out)
        elif isinstance(n, ast.ExceptHandler):
            if n.name:
                out.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for al in n.names:
                out.add((al.asname or al.name).split(".")[0])
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
    return out


def _module_scope(node, out):
    """Module-level bindings, including the ones hidden inside if/try/for/with at the top level."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.add(node.name)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            _bind(t, out)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)):
        _bind(node.target, out)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for al in node.names:
            out.add((al.asname or al.name).split(".")[0])
    elif isinstance(node, (ast.If, ast.Try, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            _bind(node.target, out)
        for it in getattr(node, "items", []) or []:
            if it.optional_vars is not None:
                _bind(it.optional_vars, out)
        for field in ("body", "orelse", "finalbody", "handlers"):
            for child in getattr(node, field, []) or []:
                if isinstance(child, ast.ExceptHandler):
                    if child.name:
                        out.add(child.name)
                    for c in child.body:
                        _module_scope(c, out)
                else:
                    _module_scope(child, out)


def undefined_names(path):
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    top = set(_BUILTIN)
    for stmt in tree.body:
        _module_scope(stmt, top)
    found = []

    def walk(node, enclosing):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = enclosing | _scope_of(child)
                for n in ast.walk(child):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) \
                            and n.id not in scope and n.id not in top:
                        found.append("%s:%d in %s(): undefined name %r"
                                     % (os.path.relpath(path, ROOT).replace("\\", "/"), n.lineno, child.name, n.id))
                walk(child, scope)
            else:
                walk(child, enclosing)

    walk(tree, set())
    return found


def test_no_function_reads_a_name_that_does_not_exist():
    files = sorted(f for pat in TARGETS for f in glob.glob(os.path.join(ROOT, pat)))
    assert files, "found no modules to check — the glob is wrong"
    bad = [msg for f in files for msg in undefined_names(f)]
    assert not bad, "NameError waiting to happen at call time:\n  " + "\n  ".join(bad)


def test_the_guard_actually_catches_the_bug_it_was_written_for(tmp_path):
    """A guard nobody has seen fail is a guard nobody knows works. This is the real settle_geese bug."""
    p = tmp_path / "regression.py"
    p.write_text("def settle_geese(ents):\n    return W // 2, H // 2\n", encoding="utf-8")
    hits = undefined_names(str(p))
    assert any("'W'" in h for h in hits) and any("'H'" in h for h in hits), hits
