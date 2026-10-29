"""Test doubles that never execute fixture code on the host."""

import re
from pathlib import Path

from sandbox.base import RunResult


class ImportCheckSandbox:
    """Fake sandbox. Its 'test suite': every `from lib import X` in any .py file must
    name something `lib.py` defines. It inspects files; it runs nothing."""

    def __init__(self, during=None):
        self.calls = []
        self.during = during  # hook to simulate something happening mid-run

    def run(self, cmd: str, workdir: Path, timeout: float) -> RunResult:
        self.calls.append((cmd, workdir, sorted(p.name for p in workdir.iterdir())))
        if self.during:
            self.during()
        if "FAIL" in cmd:
            return RunResult(1, "forced failure")
        lib = workdir / "lib.py"
        defined = set(re.findall(r"^def (\w+)", lib.read_text(), re.M)) if lib.exists() else set()
        for f in sorted(workdir.rglob("*.py")):
            for name in re.findall(r"from lib import (\w+)", f.read_text()):
                if name not in defined:
                    return RunResult(1, f"ImportError: cannot import name {name!r} ({f.name})")
        return RunResult(0, "all passed")
