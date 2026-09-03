#!/usr/bin/env python3
"""Static-check the inline three.js dashboard JS embedded in server/app.py.

Loads server/app.py as text, ast.literal_eval's the DASHBOARD constant (the big HTML/JS blob),
extracts the LARGEST <script>...</script> block, writes it to a temp .js, and runs `node --check`
on it so a syntax error in the dashboard can't slip into a deploy unnoticed. RNG-free, no DB.

Run:  python scripts/check_dashboard_js.py
Exit code 0 = OK, non-zero = parse/syntax failure (message on stderr).
"""
import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "server", "app.py")
DASHBOARD_HTML = os.path.join(HERE, "..", "server", "dashboard.html")


def _dashboard_source(path):
    """The dashboard now lives in server/dashboard.html (served via a file read in app.py). Read it directly;
    fall back to the old inline DASHBOARD = \"\"\"...\"\"\" literal in app.py for older revisions."""
    if os.path.exists(DASHBOARD_HTML):
        with open(DASHBOARD_HTML, "r", encoding="utf-8") as f:
            return f.read()
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "DASHBOARD":
                    return ast.literal_eval(node.value)
    raise SystemExit("could not find a DASHBOARD assignment in " + path)


def _largest_script(html):
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL | re.IGNORECASE)
    if not blocks:
        raise SystemExit("no <script> blocks found in DASHBOARD")
    return max(blocks, key=len)


def main():
    html = _dashboard_source(APP)
    js = _largest_script(html)
    if shutil.which("node") is None:
        # Loud, but NOT a hard failure: this runs in the CI unit job, and hard-failing on a missing tool would
        # block every deploy on a runner that simply lacks node. Install node on the k8s-deploy runner to turn
        # this back into a real gate — until then the check is announced as NOT ENFORCED rather than passing
        # silently (a silent skip is how the determinism gate ended up decorative — audit 2026-09-03, F21).
        sys.stderr.write("WARNING: `node` not found — dashboard JS syntax was NOT checked. "
                         "Install node on this runner to enforce it.\n")
        print("dashboard JS check SKIPPED (no node) — %d chars unverified" % len(js))
        return
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tf:
        tf.write(js)
        tmp = tf.name
    try:
        r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
    finally:
        os.unlink(tmp)
    if r.returncode != 0:
        sys.stderr.write(r.stdout + r.stderr)
        raise SystemExit("node --check FAILED on the dashboard JS")
    print("dashboard JS OK (node --check passed, %d chars)" % len(js))


if __name__ == "__main__":
    main()
