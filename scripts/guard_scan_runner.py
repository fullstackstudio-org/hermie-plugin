#!/usr/bin/env python3
"""The program ``guard_scan.py`` runs inside each scanner's own interpreter.

    python guard_scan_runner.py PLUGIN_DIR SCANNER_ROOT

``guard_scan.py`` starts it with ``PYTHONPATH`` set to the scanner checkout, so
``tools`` is the fetched package; this file puts nothing on the import path
itself. It first refuses to go on unless ``tools.plugin_guard`` really came
from under ``SCANNER_ROOT`` (a ``tools`` from anywhere else is not the scanner
that was asked for), then calls the two functions the update path calls and
prints exactly one line: ``GUARD_SCAN_RESULT`` and a JSON object that holds the
verdict, the install decision, the report and the findings. The report travels
inside that object, so nothing the scanner or the tree prints can pass for a
result line. Exit status 0 only for a clean ``safe`` that is allowed outright.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Must be the same text as RESULT_PREFIX in guard_scan.py (a test checks it).
RESULT_PREFIX = "GUARD_SCAN_RESULT "


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: guard_scan_runner.py PLUGIN_DIR SCANNER_ROOT", file=sys.stderr)
        return 2
    plugin, root = Path(argv[1]), Path(argv[2]).resolve()

    from tools import plugin_guard  # noqa: PLC0415 - the scanner under test, found through PYTHONPATH

    loaded = Path(plugin_guard.__file__).resolve()
    if root not in loaded.parents:
        print(f"refusing to continue: tools.plugin_guard came from {loaded}, not from {root}", file=sys.stderr)
        return 1

    result = plugin_guard.scan_plugin(plugin, source="hermie-plugin")
    allowed, reason = plugin_guard.should_allow_plugin_install(result)
    payload = {
        "verdict": str(result.verdict),
        "allowed": allowed,
        "reason": str(reason),
        "report": str(plugin_guard.format_scan_report(result)),
        "scanner_version": str(getattr(plugin_guard, "PLUGIN_SCANNER_VERSION", "unknown")),
        "findings": [
            {
                "severity": str(f.severity),
                "category": str(f.category),
                "pattern": str(f.pattern_id),
                "file": str(f.file),
                "line": int(f.line),
            }
            for f in result.findings
        ],
    }
    print(RESULT_PREFIX + json.dumps(payload, sort_keys=True))
    return 0 if (result.verdict == "safe" and allowed is True) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
