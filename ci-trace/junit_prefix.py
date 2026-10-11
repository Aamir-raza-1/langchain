"""Prefix junit testcase file= attributes with the package dir, so test files are repo-relative (monorepo).
Usage: python ci-trace/junit_prefix.py <junit.xml> <libs/pkg>"""
import sys
import xml.etree.ElementTree as ET

path, pre = sys.argv[1], sys.argv[2].rstrip("/") + "/"
tree = ET.parse(path)
for tc in tree.getroot().iter("testcase"):
    f = tc.get("file") or tc.get("classname", "").replace(".", "/") + ".py"
    if not f.startswith(pre):
        tc.set("file", pre + f)
tree.write(path)
