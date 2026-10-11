"""Load and validate a per-repo profile (YAML or JSON) against profiles/schema.json.

YAML is read with PyYAML if installed, else via the system Ruby (macOS ships one), so the
engine itself stays stdlib-only.
"""
import json
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = os.path.join(ROOT, "profiles", "schema.json")
TYPES = {"str": str, "list": list, "dict": dict, "bool": bool}


def read(path):
    if path.endswith(".json"):
        with open(path) as f:
            return json.load(f)
    try:
        import yaml  # noqa: PLC0415
        with open(path) as f:
            return yaml.safe_load(f)
    except ImportError:
        out = subprocess.run(["ruby", "-ryaml", "-rjson", "-e", "puts JSON.dump(YAML.safe_load(File.read(ARGV[0])))", path],
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out)


def validate(p, schema):
    errs = []
    if not isinstance(p, dict):
        return ["profile must be a mapping"]
    for k, t in schema["required"].items():
        if k not in p:
            errs.append("missing required field: " + k)
        elif not isinstance(p[k], TYPES[t]):
            errs.append("%s must be %s" % (k, t))
    for k, t in schema["optional"].items():
        if k in p and not isinstance(p[k], TYPES[t]):
            errs.append("%s must be %s" % (k, t))
    known = set(schema["required"]) | set(schema["optional"])
    for k in p:
        base = k[: -len("_confidence")] if k.endswith("_confidence") else k
        if base not in known:
            errs.append("unknown field: " + k)
        if k.endswith("_confidence") and p[k] not in ("read", "guess"):
            errs.append("%s must be read|guess" % k)
    for path, allowed in schema["enums"].items():
        v = p
        for part in path.split("."):
            v = v.get(part) if isinstance(v, dict) else None
        if v is not None and v not in allowed:
            errs.append("%s=%r not in %s" % (path, v, allowed))
    return errs


def load(path, schema_path=SCHEMA):
    """Read + validate; raises ValueError listing every problem."""
    p = read(path)
    with open(schema_path) as f:
        errs = validate(p, json.load(f))
    if errs:
        raise ValueError("%s: %s" % (path, "; ".join(errs)))
    return p
