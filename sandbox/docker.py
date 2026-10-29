"""Docker sandbox: one throwaway container per command (invariant 10).

The container gets the work directory mounted at /work and nothing else from the
host: no capabilities, no privilege escalation, capped memory/pids, and by default no
network. Worker runs need the model API, so a worker sandbox opts into a network and
passes its API key through by NAME only (`-e NAME`), so secrets never appear in argv
or logs (invariant 13).
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from sandbox.base import RunResult

DEFAULT_IMAGE = "seam-sandbox"  # built from sandbox/Dockerfile


class DockerSandbox:
    """`Sandbox` backed by `docker run --rm`. Each call is a fresh container."""

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        memory: str = "2g",
        pids_limit: int = 512,
        docker: str = "docker",
        network: str = "none",
        env_names: tuple[str, ...] = (),
        readonly: tuple[str, ...] = (),
        user: str | None = None,
    ) -> None:
        """`readonly`: paths under the work dir re-mounted read-only (e.g. a worktree's
        `.git` file, so a worker cannot repoint host git at a hostile config)."""
        self.image = image
        self.memory = memory
        self.pids_limit = pids_limit
        self.docker = docker
        self.network = network
        self.env_names = env_names
        self.readonly = readonly
        self.user = user

    def command(self, cmd: str, workdir: Path, name: str) -> list[str]:
        """The full `docker run` argv for `cmd` (exposed so the flags are testable)."""
        root = Path(workdir).resolve()
        argv = [
            self.docker, "run", "--rm", "--name", name,
            "--network", self.network,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--memory", self.memory,
            "--pids-limit", str(self.pids_limit),
            "-v", f"{root}:/work",
        ]
        for rel in self.readonly:
            if (root / rel).exists():
                argv += ["-v", f"{root / rel}:/work/{rel}:ro"]
        for env_name in self.env_names:
            argv += ["-e", env_name]
        if self.user:
            argv += ["--user", self.user]
        return [*argv, "-w", "/work", self.image, "sh", "-c", cmd]

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
