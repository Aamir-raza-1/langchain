#!/usr/bin/env python3
"""Static safety layer: which repo files does each test file import (transitively)?

Covers what per-test coverage cannot: module-level code (constants, class bodies,
decorators) runs once at import time and is never credited to individual tests.

Also classifies a change as body-only or module-level, so selection can use the
cheap runtime map for body-only edits and the import closure otherwise.

Generic version of the fork's script/trace/static_imports.py (which still runs in the HA
workflows until R1): source and test roots come from the profile, the test-file pattern from
the runner conventions in engine/select.py.

Usage:
  python3 -m engine.static_imports <repo_checkout> --profile profiles/<owner>__<repo>.yaml --out static.json
"""
import argparse
import ast
import json
import os
import sys


def expand_roots(repo, patterns):
    """Import roots (dirs on sys.path, e.g. each package of a monorepo); `*` segments are expanded."""
    import glob  # noqa: PLC0415
    out = []
    for p in patterns or []:
        hits = sorted(os.path.relpath(h, repo) for h in glob.glob(os.path.join(repo, p)) if os.path.isdir(h)) if "*" in p else [p.rstrip("/")]
        out += [h for h in hits if h not in out]
    return sorted(out, key=len, reverse=True)  # longest first: nested roots win


def _root_of(rel, import_roots):
    return next((r for r in import_roots if rel.startswith(r + "/")), "")


def module_files(repo, roots, import_roots=()):
    """dotted module name -> repo-relative path, for every .py under the given roots.

    Without import roots, names are relative to the repo root (single-package repos). With import
    roots (monorepos), a file's name is relative to the longest import root holding it, and the
    result is {import root: {name: path}} ("" = the repo root), so two packages may both own
    `tests.conftest`; imports resolve in the importing file's own root first (see `resolver`).
    """
    by_root = {}
    for top in roots:
        for dirpath, dirnames, filenames in os.walk(os.path.join(repo, top)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), repo)
                root = _root_of(rel, import_roots)
                name = (rel[len(root) + 1:] if root else rel)[:-3].replace(os.sep, ".")
                if name.endswith(".__init__"):
                    name = name[: -len(".__init__")]
                by_root.setdefault(root, {})[name] = rel
    if not import_roots:
        return by_root.get("", {})
    return by_root


class resolver(dict):
    """{name: path} view for one importing file: its own import root first, then names unique elsewhere."""

    def __init__(self, own, unique):
        super().__init__(unique)
        self.update(own)


def _unique(by_root):
    seen, dup = {}, set()
    for mods in by_root.values():
        for n, rel in mods.items():
            if n in seen and seen[n] != rel:
                dup.add(n)
            seen[n] = rel
    return {n: r for n, r in seen.items() if n not in dup}


def imports_of(path, rel, mods, name=None):
    """Repo files imported directly by one file (parents' __init__ included).
    `name`: the file's dotted module name when it differs from its repo path (import roots)."""
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=rel)
    except (SyntaxError, UnicodeDecodeError):
        return set()
    pkg = name + (".__init__" if rel.endswith("__init__.py") else "") if name is not None else rel[:-3].replace(os.sep, ".")
    is_init = pkg.endswith(".__init__")
    pkg = pkg[: -len(".__init__")] if is_init else pkg.rsplit(".", 1)[0]
    found = set()

    def add(name):
        # importing a.b.c executes a, a.b and a.b.c
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            m = ".".join(parts[:i])
            if m in mods:
                found.add(mods[m])

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.split(".")
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            add(mod)
            for a in node.names:  # `from pkg import submodule`
                add(mod + "." + a.name)
    found.discard(rel)
    return found


def conftests_for(test_rel, mods_by_path):
    parts = test_rel.split("/")[:-1]
    out = set()
    for i in range(1, len(parts) + 1):
        c = "/".join(parts[:i] + ["conftest.py"])
        if c in mods_by_path:
            out.add(c)
    return out


def closure(start, graph):
    seen, stack = set(), list(start)
    while stack:
        f = stack.pop()
        if f in seen:
            continue
        seen.add(f)
        stack.extend(graph.get(f, ()))
    return seen


def body_only_change(old_src, new_src):
    """True if every changed line sits inside a function body (not signature/decorators)."""
    import difflib

    def body_lines(src):
        lines = set()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return None
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.body:
                lines.update(range(n.body[0].lineno, n.end_lineno + 1))
        return lines

    old_b, new_b = body_lines(old_src), body_lines(new_src)
    if old_b is None or new_b is None:
        return False
    sm = difflib.SequenceMatcher(a=old_src.splitlines(), b=new_src.splitlines(), autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        old_changed = set(range(i1 + 1, i2 + 1))
        new_changed = set(range(j1 + 1, j2 + 1))
        if not old_changed <= old_b or not new_changed <= new_b:
            return False
        if not old_changed and not new_changed:
            return False
    return True


def build(repo, source_roots, test_roots, test_file_re, import_roots=None):
    """{test file: sorted repo files in its import closure (conftests included)}."""
    roots = expand_roots(repo, import_roots)
    mods = module_files(repo, list(source_roots) + list(test_roots), roots)
    if roots:
        unique = _unique(mods)
        views = {r: resolver(m, unique) for r, m in mods.items()}
        names = {rel: (r, n) for r, m in mods.items() for n, rel in m.items()}
        by_path = set(names)
        graph = {rel: imports_of(os.path.join(repo, rel), rel, views[names[rel][0]], names[rel][1]) for rel in by_path}
    else:
        by_path = set(mods.values())
        graph = {rel: imports_of(os.path.join(repo, rel), rel, mods) for rel in by_path}
    tops = tuple(r.rstrip("/") + "/" for r in test_roots)
    test_files = sorted(p for p in by_path if p.startswith(tops) and test_file_re.search(p))
    result = {}
    for t in test_files:
        start = graph[t] | conftests_for(t, by_path)
        result[t] = sorted(closure(start, graph) - {t})
    return result, len(by_path)


def main():
    ap = argparse.ArgumentParser(description="Static import closure per test file (Python).")
    ap.add_argument("repo")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--out", default="static.json")
    args = ap.parse_args()
    from engine.profile import load  # noqa: PLC0415
    from engine.select import RUNNERS  # noqa: PLC0415
    p = load(args.profile)
    result, n_mods = build(args.repo, p["source_roots"], p["test_roots"], RUNNERS[p["test_runner"]]["test_file"],
                           p.get("import_roots"))
    sizes = sorted(len(v) for v in result.values())
    print("modules: %d, test files: %d" % (n_mods, len(result)), file=sys.stderr)
    if sizes:
        print("import closure per test file: median %d, p90 %d, max %d files"
              % (sizes[len(sizes) // 2], sizes[int(len(sizes) * 0.9)], sizes[-1]), file=sys.stderr)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(result, f)


if __name__ == "__main__":
    main()
