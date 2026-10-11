"""Change-aware test selection at test-file level. Every repo-specific rule comes from the profile.

Rules, first match wins per changed file (profile field in brackets):
  run_everything_when glob                -> FULL
  dependency_lockfiles glob               -> tests of the areas whose dependency manifest changed in
                                             the same change [dependency_manifests + area_rule];
                                             no manifest changed -> FULL
  ignore glob                             -> nothing
  tooling_scopes glob                     -> that tool's test folder(s)
  a known test file (co-located or not)   -> itself
  under test_roots:
    runner config file (conftest.py)      -> every test file under its folder
    test file                             -> itself
    other source file (helpers)           -> test files importing it + its folder's tests
    data file                             -> its folder's tests (runner data dirs such as fixtures/ cut off)
                                             + tests that opened it at runtime
  under source_roots:
    module in the import graph            -> body-only edit with a runtime map: runtime users + area tests
                                             otherwise: static importers (+ runtime users: dynamic loading)
    other file inside an area [area_rule(s)], or a statically imported data file
                                          -> the area's tests + runtime users + static importers
    anything else                         -> FULL (conservative)
  anything else                           -> FULL (conservative)
Unless FULL, tests matching `always_run` globs are added (tests whose inputs neither map can see,
e.g. ones that read repo files from disk).

Inputs: the static import map {test_file: [repo files]} (engine/static_imports.py), optionally the
runtime map from the traced run and real seconds per test file.
"""
import collections
import gzip
import json
import os
import re

# Test-runner conventions (not repo rules): how to recognise test files, folder config and data dirs.
RUNNERS = {
    "pytest": {"test_file": re.compile(r"(^|/)test_[^/]*\.py$"), "folder_config": "conftest.py",
               "code_ext": ".py", "data_dirs": ["fixtures", "snapshots", "__snapshots__"]},
    "vitest": {"test_file": re.compile(r"\.(spec|test)\.[cm]?[jt]sx?$"), "folder_config": None,
               "code_ext": (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
               "data_dirs": ["__fixtures__", "__snapshots__", "fixtures"]},
}


def glob_regex(pattern):
    """Path glob -> regex: `**/` any dirs, `/**` everything below, `*` within a segment, `{name}` captures one segment."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern.startswith("{name}", i):
            out.append("(?P<name>[^/]+)")
            i += 6
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


class Globs:
    def __init__(self, patterns):
        self.res = [glob_regex(p) for p in patterns or []]

    def __call__(self, path):
        return any(r.match(path) for r in self.res)


class AreaRule:
    """`src/{name}/** -> tests/{name}/` (one rule or several, first match wins): the test folder
    of the area a source file belongs to. Areas are returned as (rule index, name)."""

    def __init__(self, rules):
        rules = [rules] if isinstance(rules, str) else list(rules or [])
        self.rules = []
        for rule in rules:
            src, dst = [x.strip() for x in rule.split("->")]
            self.rules.append((glob_regex(src), dst))

    def name(self, path):
        for i, (rx, _) in enumerate(self.rules):
            m = rx.match(path)
            if m:
                return (i, m.group("name"))
        return None

    def tests_dir(self, area):
        i, name = area
        return self.rules[i][1].replace("{name}", name)


class Rules:
    def __init__(self, profile):
        self.runner = RUNNERS[profile["test_runner"]]
        self.full = Globs(profile["run_everything_when"])
        self.lockfiles = Globs(profile.get("dependency_lockfiles"))
        self.manifests = Globs(profile.get("dependency_manifests"))
        self.ignore = Globs(profile.get("ignore"))
        self.tooling = [(glob_regex(k), [v] if isinstance(v, str) else list(v))
                        for k, v in (profile.get("tooling_scopes") or {}).items()]
        self.area = AreaRule(profile.get("area_rules") or profile.get("area_rule"))
        self.always_run = Globs(profile.get("always_run"))
        self.test_roots = tuple(r.rstrip("/") + "/" for r in profile["test_roots"])
        self.source_roots = tuple(r.rstrip("/") + "/" for r in profile["source_roots"])


class TestIndex:
    """All test files, who imports what (static), and who used what at runtime (traced run)."""

    def __init__(self, static, runtime=None, seconds=None):
        self.test_files = sorted(static)
        self.test_set = set(self.test_files)
        self.importers = collections.defaultdict(set)
        for t, deps in static.items():
            for d in deps:
                self.importers[d].add(t)
        self.runtime = runtime
        self.seconds = seconds
        self._folders = {}

    def folder(self, prefix):
        if prefix not in self._folders:
            self._folders[prefix] = {t for t in self.test_files if t.startswith(prefix)}
        return self._folders[prefix]

    def used_at_runtime(self, f):
        return self.runtime.get(f, set()) if self.runtime is not None else set()

    def share(self, selected):
        if self.seconds:
            total = sum(self.seconds.get(t, 0) for t in self.test_files)
            return round(100.0 * sum(self.seconds.get(t, 0) for t in selected) / total, 1)
        return round(100.0 * len(selected) / len(self.test_files), 1)


def select(files, kinds, rules, idx):
    """files: changed paths; kinds: {path: 'body-only'|'module-level'|...}. -> (selected, full, reasons)."""
    run = rules.runner
    selected, full, reasons = set(), False, collections.Counter()
    areas_with_manifest = {rules.area.name(f) for f in files if rules.manifests(f)} - {None}
    for f in files:
        tooling = next((dirs for rx, dirs in rules.tooling if rx.match(f)), None)
        if rules.full(f):
            full = True
            reasons["full: " + f] += 1
        elif rules.lockfiles(f):
            if not areas_with_manifest:
                full = True
                reasons["full: lockfile without manifest"] += 1
            for a in areas_with_manifest:
                selected |= idx.folder(rules.area.tests_dir(a))
            reasons["deps"] += 1
        elif rules.ignore(f):
            reasons["no tests"] += 1
        elif tooling is not None:
            for d in tooling:
                selected |= idx.folder(d)
            reasons["tooling"] += 1
        elif f in idx.test_set:
            selected.add(f)
            reasons["test file"] += 1
        elif f.startswith(rules.test_roots):
            d = os.path.dirname(f)
            if os.path.basename(f) == run["folder_config"]:
                selected |= idx.folder(d + "/")
            elif run["test_file"].search(f):
                selected.add(f)
            elif f.endswith(run["code_ext"]):
                selected |= idx.importers.get(f, set()) | idx.folder(d + "/")
            else:
                for dd in run["data_dirs"]:
                    d = d.split("/" + dd)[0]
                selected |= idx.folder(d + "/") | idx.used_at_runtime(f)  # + tests that opened it (traced run)
            reasons["tests"] += 1
        elif f.startswith(rules.source_roots):
            area = rules.area.name(f)
            area_tests = idx.folder(rules.area.tests_dir(area)) if area else set()
            if f.endswith(run["code_ext"]) and f in idx.importers:
                if kinds.get(f) == "body-only" and idx.runtime is not None:
                    selected |= idx.used_at_runtime(f) | area_tests
                    reasons["code body (runtime)"] += 1
                else:
                    selected |= idx.importers[f] | idx.used_at_runtime(f)
                    reasons["code module (static%s)" % ("+runtime" if idx.runtime is not None else "")] += 1
            elif area or f in idx.importers:
                # non-code file (data, svg, css…): area tests + runtime users + static importers if imported
                selected |= area_tests | idx.used_at_runtime(f) | idx.importers.get(f, set())
                reasons["area folder"] += 1
            else:
                full = True
                reasons["full: " + f] += 1
        else:
            full = True
            reasons["full: " + f] += 1
    if not full and rules.always_run.res:
        extra = {t for t in idx.test_files if rules.always_run(t)}
        if extra - selected:
            reasons["always run"] += 1
        selected |= extra
    return selected, full, reasons


def load_runtime_map(path):
    """{repo file: {test files}} from the analyzer's map (test-file .json.gz, or per-test .json)."""
    runtime = collections.defaultdict(set)
    if path.endswith(".gz"):
        with gzip.open(path, "rt") as f:
            m = json.load(f)
        for key, ids in m["file_to_test_files"].items():
            if key.startswith("repo:"):
                runtime[key[5:]] = {m["test_files"][i] for i in ids}
    else:
        with open(path) as f:
            m = json.load(f)
        for key, ids in m["file_to_tests"].items():
            if key.startswith("repo:"):
                for i in ids:
                    runtime[key[5:]].add(m["tests"][i].split("::")[0])
    return runtime


def replay(prs, rules, idx):
    """Selection for each recorded change; savings weighted by each change's full-run test minutes."""
    rows = []
    for p in prs:
        selected, full, reasons = select(p["files"], p.get("change_kinds", {}), rules, idx)
        pct = 100.0 if full else idx.share(selected)
        rows.append({"pr": p["pr"], "label": p.get("label"), "minutes": p["minutes"], "pct_test_files": pct,
                     "selected": None if full else len(selected), "reasons": dict(reasons)})
    total = sum(r["minutes"] for r in rows)
    ours = sum(r["minutes"] * r["pct_test_files"] / 100 for r in rows)
    summary = {
        "prs": len(rows),
        "test_files_total": len(idx.test_files),
        "runtime_map": bool(idx.runtime),
        "full_run_test_minutes_today": round(total),
        "estimated_test_minutes_with_selection": round(ours),
        "estimated_saving_pct": round(100 * (1 - ours / total), 1) if total else None,
        "prs_still_full": sum(1 for r in rows if r["pct_test_files"] == 100.0),
        "prs_under_10pct": sum(1 for r in rows if r["pct_test_files"] < 10),
        "median_pct_test_files": sorted(r["pct_test_files"] for r in rows)[len(rows) // 2] if rows else None,
        "note": "Weighted by real seconds per test file." if idx.seconds else
                "Test-file granularity; minutes assume run time is proportional to the share of test files.",
    }
    return {"summary": summary, "prs": rows}
