"""Docker sandbox: one throwaway container per command (invariant 10).

The container gets the work directory mounted at /work and nothing else from the
host: no network, no capabilities, no privilege escalation, capped memory/pids.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from sandbox.base import RunResult

DEFAULT_IMAGE = "python:3.13-slim"


class DockerSandbox:
    """`Sandbox` backed by `docker run --rm`. Each call is a fresh container."""

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        memory: str = "2g",
        pids_limit: int = 512,
        docker: str = "docker",
    ) -> None:
        self.image = image
        self.memory = memory
        self.pids_limit = pids_limit
        self.docker = docker

    def command(self, cmd: str, workdir: Path, name: str) -> list[str]:
        """The full `docker run` argv for `cmd` (exposed so the flags are testable)."""
        return [
            self.docker, "run", "--rm", "--name", name,
            "--network", "none",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--memory", self.memory,
            "--pids-limit", str(self.pids_limit),
            "-v", f"{Path(workdir).resolve()}:/work",
            "-w", "/work",
            self.image, "sh", "-c", cmd,
        ]

    def run(self, cmd: str, workdir: Path, timeout: float) -> RunResult:
        name = f"seam-{uuid.uuid4().hex[:12]}"
        try:
            proc = subprocess.run(
                self.command(cmd, workdir, name),
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            subprocess.run([self.docker, "kill", name], capture_output=True)
            # TimeoutExpired carries bytes even with text=True.
            out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return RunResult(exit_code=-1, output=out, timed_out=True)
        return RunResult(exit_code=proc.returncode, output=proc.stdout + proc.stderr)
