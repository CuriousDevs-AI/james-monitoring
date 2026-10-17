"""Run isolated frontend behavior checks with Node when available; no npm packages required."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_owner_console_form_behaviors():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node 18+ required for the dependency-free console form checks")
    result = subprocess.run([node, "--test", str(Path(__file__).with_name("console_controls.test.mjs"))],
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
