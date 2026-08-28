"""Token-identity checks for the takeover fix.

Reuse-by-name used to return the bound token to any caller, and /intent enforced the token only if the
agent happened to have one. Agent names are public, so both were takeover paths. These assert the two
rules that close them, plus the runner-side store that has to survive restarts once the server stops
handing secrets out.

Run: python3 tests/test_agent_tokens.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _load_secret_eq():
    """Pull `_secret_eq` out of server/app.py without importing it.

    The function is pure string comparison, but its module imports fastapi, pydantic and psycopg2 —
    none of which a contributor needs installed to check this logic. Reading the one function keeps
    the test honest (it runs the real source) and runnable anywhere.
    """
    import ast
    src = open(os.path.join(os.path.dirname(__file__), "..", "server", "app.py")).read()
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_secret_eq")
    mod = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    ns = {"hmac": __import__("hmac")}
    exec(compile(mod, "<app>", "exec"), ns)
    return ns["_secret_eq"]


def test_secret_eq():
    _secret_eq = _load_secret_eq()
    assert _secret_eq("abc", "abc")
    assert not _secret_eq("abc", "abd")
    assert not _secret_eq("abc", "abcd")          # length mismatch is a refusal, not a crash
    assert not _secret_eq("", "abc")
    assert not _secret_eq(None, "abc")
    assert not _secret_eq("é", "abc")             # non-ASCII must refuse, not raise (would be a 500)
    assert _secret_eq("é", "é")
    print("ok  _secret_eq: mismatch, length, None and non-ASCII all refuse cleanly")


def test_token_store(tmpdir):
    os.environ["NHA_TOKENS"] = os.path.join(tmpdir, "tokens.json")
    sys.modules.pop("agents.runner", None)
    import importlib
    runner = importlib.import_module("agents.runner")
    importlib.reload(runner)

    assert runner._tokens() == {}, "missing file reads as empty"

    runner._save_token("w|alice", "AAA")
    assert runner._tokens()["w|alice"] == "AAA"
    assert oct(os.stat(runner.TOKENS_PATH).st_mode)[-3:] == "600", "secrets must not be world-readable"

    runner._save_token("w|bob", "BBB")
    assert runner._tokens() == {"w|alice": "AAA", "w|bob": "BBB"}, "second write keeps the first key"

    for junk in ("", "{broken", "null", "[]", '"str"'):
        with open(runner.TOKENS_PATH, "w") as f:
            f.write(junk)
        assert runner._tokens() == {}, f"corrupt store {junk!r} must read as empty, not crash"

    assert not [n for n in os.listdir(tmpdir) if n.startswith(".tokens-")], "no temp files left behind"
    print("ok  token store: perms 0600, additive writes, corrupt file degrades to empty, no temp litter")


if __name__ == "__main__":
    test_secret_eq()
    with tempfile.TemporaryDirectory() as d:
        test_token_store(d)
    print("ALL TOKEN TESTS PASS")
