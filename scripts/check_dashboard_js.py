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
