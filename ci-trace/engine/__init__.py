"""Evidence-based CI engine: audit -> trace -> select -> verify, driven by a per-repo profile.

Engine code stays generic; everything repo-specific lives in profiles/<owner>__<repo>.yaml
(knowledge/engine.md). Stdlib only, Python >= 3.9.
"""
__version__ = "0.1.0"
