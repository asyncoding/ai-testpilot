"""import-lint 门禁（docs/08 FR-CFG-002 AC1）。"""

import subprocess
import sys
from pathlib import Path

SUBSYSTEMS = Path(__file__).resolve().parents[1]


def test_core_has_no_forbidden_imports():
    script = SUBSYSTEMS / "scripts" / "import_lint.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
