#!/usr/bin/env python3
"""Phase 1: build the file -> tests map from traced runs and report on it.

Input: artifact folders downloaded from the fork's "Trace tests" workflow
(trace-group-*/ containing coverage.sqlite, tests-*.jsonl, opens.log.gz, timing.txt).

Two sources, merged per test:
  coverage  per-test coverage contexts (pytest-cov --cov-context=test): Python files executed
  ebpf      file opens (bpftrace) inside the test's time window on the same worker PID

Usage:
  python3 ci-trace/analyze.py <artifacts_dir> --root /home/runner/work/<repo>/<repo>/ [--out phase1]

Generic version of the HA fork's script/trace/analyze.py (monorepo prefixes via pkg.txt per group).
"""
import argparse
import bisect
import collections
import glob
import gzip
import json
import os
import re
import sqlite3
import statistics

PHASE0_TRIGGERS = []  # repo files to report test counts for (--trigger)
IGNORED_PREFIXES = ("/proc/", "/sys/", "/dev/", "/etc/", "/run/", "/tmp/", "/usr/share/", "/var/")
SITE = re.compile(r"/site-packages/([^/]+)")


def repo_relative(path, roots):
    for root in roots:
        if path.startswith(root):
            return path[len(root):]
    return None


def normalize(path, roots, prefix=""):
    """Return ('repo', relpath) | ('pkg', distribution-ish name) | None for noise.
    Relative paths are relative to the process cwd: the package dir (prefix) in a monorepo."""
    if not path.startswith("/"):
        rel = os.path.normpath(prefix + path)
        return None if rel in (".", prefix.rstrip("/")) else ("repo", rel)
    rel = repo_relative(path, roots)
    if rel is not None:
        if os.path.basename(rel).startswith((".coverage", "junit")):
            return None  # the measurement tools' own output files
        if rel.startswith(("venv/", ".git/", "trace/")) or "/.venv/" in "/" + rel or "__pycache__" in rel or "/.pytest_cache/" in "/" + rel:
            m = SITE.search(path)
            return ("pkg", m.group(1)) if m else None
        return ("repo", rel)
    m = SITE.search(path)
    if m:
        return ("pkg", m.group(1))
    if path.startswith(IGNORED_PREFIXES) or "/lib/python3" in path:
        return None
    return None


def prefix_of(group_dir):
    """Monorepos: pytest runs inside a package dir, so node ids are relative to it (pkg.txt = that dir)."""
    p = os.path.join(group_dir, "pkg.txt")
    return open(p).read().strip().rstrip("/") + "/" if os.path.exists(p) else ""


def load_windows(group_dir):
    windows = collections.defaultdict(list)  # pid -> [(start, end, nodeid, outcome)]
    pre = prefix_of(group_dir)
    for f in glob.glob(os.path.join(group_dir, "tests-*.jsonl")):
        for line in open(f):
            t = json.loads(line)
            windows[t["pid"]].append((t["start_ns"], t["end_ns"], pre + t["nodeid"], t["outcome"]))
    for pid in windows:
        windows[pid].sort()
    return windows


def ebpf_deps(group_dir, windows, roots, prefix=""):
    deps = collections.defaultdict(set)
    path = os.path.join(group_dir, "opens.log.gz")
    if not os.path.exists(path):
        return deps, 0, 0
    starts = {pid: [w[0] for w in ws] for pid, ws in windows.items()}
    seen = attributed = 0
    cwd = {}  # pid -> absolute cwd after the last chdir (opens.bt CHDIR lines); default: the package dir
    base = roots[0].rstrip("/") + "/" + prefix if roots else ""
    events = []
    with gzip.open(path, "rt", errors="replace") as fh:
        for line in fh:
            parts = line.rstrip("\n").split(" ", 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                events.append((int(parts[0]), int(parts[1]), parts[2]))
    events.sort()  # per-CPU buffers can interleave; chdir must precede the opens it affects
    if True:
        for ns, pid, fpath in events:
            if fpath.startswith("CHDIR "):
                d = fpath[6:]
                cwd[pid] = os.path.normpath(d if d.startswith("/") else os.path.join(cwd.get(pid, base), d))
                continue
            if not fpath.startswith("/") and pid in cwd:
                fpath = os.path.normpath(os.path.join(cwd[pid], fpath))
            seen += 1
            ws = windows.get(pid)
            if not ws:
                continue
            i = bisect.bisect_right(starts[pid], ns) - 1
            if i < 0 or ns > ws[i][1]:
                continue
            norm = normalize(fpath, roots, prefix)
            if norm:
                attributed += 1
                deps[ws[i][2]].add(norm)
    return deps, seen, attributed


def coverage_deps(group_dir, roots, prefix=""):
    deps = collections.defaultdict(set)
    unattributed = set()
    db = os.path.join(group_dir, "coverage.sqlite")
    if not os.path.exists(db):
        return deps, unattributed
    con = sqlite3.connect(db)
    files = dict(con.execute("select id, path from file"))
    contexts = dict(con.execute("select id, context from context"))
    table = "line_bits" if con.execute("select count(*) from sqlite_master where name='line_bits'").fetchone()[0] else "arc"
    for file_id, ctx_id in con.execute("select distinct file_id, context_id from %s" % table):
        rel = repo_relative(files[file_id], roots) or files[file_id]
        ctx = contexts.get(ctx_id, "")
        if not ctx:
            unattributed.add(rel)
            continue
        nodeid = prefix + ctx.rsplit("|", 1)[0]
        deps[nodeid].add(("repo", rel))
    return deps, unattributed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("artifacts")
    ap.add_argument("--out", default="phase1")
    ap.add_argument("--no-test-map", action="store_true", help="skip the large per-test map.json")
    ap.add_argument("--root", action="append", required=True, help="checkout root on the runner, e.g. /home/runner/work/<repo>/<repo>/")
    ap.add_argument("--trigger", action="append", default=[], help="repo file to report test counts for")
    args = ap.parse_args()

    tests = {}  # nodeid -> outcome
    cov_all, ebpf_all = collections.defaultdict(set), collections.defaultdict(set)
    import_time_only = set()
    timing, opens_seen, opens_attr = {}, 0, 0
    for gdir in sorted(glob.glob(os.path.join(args.artifacts, "trace-group-*"))):
        commit = open(os.path.join(gdir, "commit.txt")).read().strip() if os.path.exists(os.path.join(gdir, "commit.txt")) else None
        roots = args.root
        windows = load_windows(gdir)
        for ws in windows.values():
            for _, _, nodeid, outcome in ws:
                tests[nodeid] = outcome
        c, unattr = coverage_deps(gdir, roots, prefix_of(gdir))
        e, seen, attr = ebpf_deps(gdir, windows, roots, prefix_of(gdir))
        opens_seen += seen
        opens_attr += attr
        import_time_only |= unattr
        for k, v in c.items():
            cov_all[k] |= v
        for k, v in e.items():
            ebpf_all[k] |= v
        t = os.path.join(gdir, "timing.txt")
        if os.path.exists(t):
            timing[os.path.basename(gdir)] = dict(l.strip().split("=") for l in open(t) if "=" in l)

    # merge: test -> deps; invert: dep -> tests
    merged = {n: cov_all.get(n, set()) | ebpf_all.get(n, set()) for n in tests}
    by_file = collections.defaultdict(set)
    for n, ds in merged.items():
        for kind, name in ds:
            by_file["%s:%s" % (kind, name)].add(n)
    n_tests = len(tests)

    def is_code(p):
        return p.endswith(".py")

    ebpf_only_noncode = collections.Counter()
    tests_with_noncode = 0
    for n in tests:
        extra = {d for d in ebpf_all.get(n, set()) - cov_all.get(n, set()) if d[0] == "repo" and not is_code(d[1])}
        if extra:
            tests_with_noncode += 1
        for d in extra:
            ebpf_only_noncode[d[1]] += 1

    report = {
        "tests": n_tests,
        "outcomes": collections.Counter(tests.values()),
        "timing": timing,
        "deps_per_test_median": {
            "coverage": statistics.median([len(cov_all.get(n, ())) for n in tests]) if tests else None,
            "ebpf": statistics.median([len(ebpf_all.get(n, ())) for n in tests]) if tests else None,
            "merged": statistics.median([len(merged[n]) for n in tests]) if tests else None,
        },
        "ebpf_opens_seen": opens_seen,
        "ebpf_opens_attributed_to_tests": opens_attr,
        "tests_reading_noncode_files_invisible_to_coverage": tests_with_noncode,
        "top_noncode_files_invisible_to_coverage": ebpf_only_noncode.most_common(15),
        "files_executed_only_outside_tests (import time)": len(import_time_only),
        "phase0_trigger_files": {
            f: {"tests": len(by_file.get("repo:" + f, ())), "pct_of_traced": round(100.0 * len(by_file.get("repo:" + f, ())) / n_tests, 1) if n_tests else None}
            for f in PHASE0_TRIGGERS + args.trigger
        },
        "packages_by_tests": sorted(((k[4:], len(v)) for k, v in by_file.items() if k.startswith("pkg:")), key=lambda kv: -kv[1])[:25],
    }
    # Real time per test file (junit.xml from the traced run), to weight savings by seconds, not file counts.
    import xml.etree.ElementTree as ET
    file_seconds = collections.Counter()
    for gdir in sorted(glob.glob(os.path.join(args.artifacts, "trace-group-*"))):
        jx = os.path.join(gdir, "junit.xml")
        if os.path.exists(jx):
            pre = prefix_of(gdir)
            for tc in ET.parse(jx).getroot().iter("testcase"):
                f = tc.get("file") or tc.get("classname", "").replace(".", "/") + ".py"
                file_seconds[pre + f] += float(tc.get("time") or 0)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "test_file_seconds.json"), "w") as f:
        json.dump(dict(file_seconds), f)
    with open(os.path.join(args.out, "report.json"), "w") as f:
        json.dump(report, f, indent=2, default=list)
        f.write("\n")
    index = sorted(tests)
    pos = {n: i for i, n in enumerate(index)}
    if not args.no_test_map:
        with open(os.path.join(args.out, "map.json"), "w") as f:
            json.dump({"tests": index, "file_to_tests": {k: sorted(pos[n] for n in v) for k, v in by_file.items()}}, f)
    # Test-file granularity (what selection runs): much smaller than the per-test map.
    tfiles = sorted({n.split("::")[0] for n in tests})
    tpos = {t: i for i, t in enumerate(tfiles)}
    with gzip.open(os.path.join(args.out, "map_testfiles.json.gz"), "wt") as f:
        json.dump({"test_files": tfiles,
                   "file_to_test_files": {k: sorted({tpos[n.split("::")[0]] for n in v}) for k, v in by_file.items()}}, f)
    print(json.dumps(report, indent=2, default=list))


if __name__ == "__main__":
    main()
