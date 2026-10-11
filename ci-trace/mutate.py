"""Generate and apply mutants (small deliberate bugs) for the selection safety check.

generate: python ci-trace/mutate.py generate targets.json --seed 1 --out mutation [--executed executed.json]
  Writes mutation/mutants.json and mutation/batches.json. Per target file it picks up to
  one body-level mutant (negate an `if` test, or `return None`) and one module-level
  mutant (change a top-level constant). With --executed ({file: [lines]} from the traced run's
  coverage), body mutants are planted only on lines some test executed (HA lesson: bugs on
  unexecuted lines prove nothing). Every mutant gets a batch of its own (monorepo: a core bug can
  break any package's tests, so mixing mutants would blur attribution).

apply: python ci-trace/mutate.py apply mutation 3
  Applies every mutant of batch 3 (batch 0 = baseline, no mutants).

Generic version of the HA fork's script/trace/mutate.py.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import random
import sys


def _candidates(path: str) -> tuple[list[dict], list[dict]]:
    src = Path(path).read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src)
    body, module = [], []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.If) and node.test.lineno == node.test.end_lineno:
                line = lines[node.test.lineno - 1]
                seg = line[node.test.col_offset : node.test.end_col_offset]
                new = line[: node.test.col_offset] + f"not ({seg})" + line[node.test.end_col_offset :]
                body.append({"line": node.test.lineno, "op": "negate-if", "old": line, "new": new})
            elif (
                isinstance(node, ast.Return)
                and node.value is not None
                and not (isinstance(node.value, ast.Constant) and node.value.value is None)
                and node.lineno == node.end_lineno
            ):
                line = lines[node.lineno - 1]
                indent = line[: len(line) - len(line.lstrip())]
                body.append({"line": node.lineno, "op": "return-none", "old": line, "new": indent + "return None"})
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) in (int, str)
            and not isinstance(node.value.value, bool)
            and node.lineno == node.end_lineno
            and not node.targets[0].id.startswith("__")
        ):
            line = lines[node.lineno - 1]
            v = node.value
            repl = repr(v.value + 1) if isinstance(v.value, int) else repr(v.value + "_mutant")
            new = line[: v.col_offset] + repl + line[v.end_col_offset :]
            module.append({"line": node.lineno, "op": "change-constant", "old": line, "new": new})
    return body, module


def _valid(path: str, m: dict) -> bool:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    lines[m["line"] - 1] = m["new"]
    try:
        ast.parse("\n".join(lines))
    except SyntaxError:
        return False
    return True


def generate(targets_file: str, seed: int, out: str, executed: str | None = None) -> None:
    rng = random.Random(seed)
    targets = json.loads(Path(targets_file).read_text())
    ran = {f: set(ls) for f, ls in json.loads(Path(executed).read_text()).items()} if executed else None
    mutants = []
    for path in targets:
        try:
            body, module = _candidates(path)
        except SyntaxError as err:  # only happens on an older local Python
            print(f"skip {path}: {err.msg}", file=sys.stderr)
            continue
        if ran is not None:
            body = [m for m in body if m["line"] in ran.get(path, ())]
        for kind, pool in (("body", body), ("module", module)):
            rng.shuffle(pool)
            for m in pool:
                if _valid(path, m):
                    mutants.append({"id": len(mutants) + 1, "file": path, "kind": kind, **m})
                    break
    batches: list[list[int]] = [[m["id"]] for m in mutants]  # one mutant per batch
    Path(out).mkdir(parents=True, exist_ok=True)
    Path(out, "mutants.json").write_text(json.dumps(mutants, indent=1))
    Path(out, "batches.json").write_text(json.dumps([[]] + batches))  # batch 0 = baseline
    print(f"{len(mutants)} mutants from {len(targets)} files in {len(batches)} batches (+ baseline)")


def apply(out: str, batch: int) -> None:
    mutants = {m["id"]: m for m in json.loads(Path(out, "mutants.json").read_text())}
    ids = json.loads(Path(out, "batches.json").read_text())[batch]
    for i in ids:
        m = mutants[i]
        p = Path(m["file"])
        lines = p.read_text(encoding="utf-8").split("\n")
        assert lines[m["line"] - 1] == m["old"], f"mutant {i}: line changed"
        lines[m["line"] - 1] = m["new"]
        p.write_text("\n".join(lines), encoding="utf-8")
        print(f"applied mutant {i}: {m['file']}:{m['line']} {m['op']}")
        print(f"  - {m['old'].strip()}\n  + {m['new'].strip()}")


def apply_ids(out: str, ids: list[int]) -> None:
    """Apply specific mutants regardless of batch (for isolated re-runs)."""
    batches = json.loads(Path(out, "batches.json").read_text())
    Path(out, "batches.json").write_text(json.dumps(batches + [ids]))
    apply(out, len(batches))


if __name__ == "__main__":
    if sys.argv[1] == "apply-ids":
        apply_ids(sys.argv[2], [int(x) for x in sys.argv[3].split(",") if x])
    elif sys.argv[1] == "generate":
        generate(sys.argv[2], int(sys.argv[sys.argv.index("--seed") + 1]), sys.argv[sys.argv.index("--out") + 1],
                 sys.argv[sys.argv.index("--executed") + 1] if "--executed" in sys.argv else None)
    else:
        apply(sys.argv[2], int(sys.argv[3]))
