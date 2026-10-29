"""Docker sandbox: one throwaway container per command (invariant 10).

The container gets the work directory mounted at /work and nothing else from the
host: no capabilities, no privilege escalation, capped memory/pids, and by default no
network. Worker runs need the model API: they run on the internal `seam-workers`
network, whose only reachable host is the credential proxy (`CredentialProxyContainer`,
doc §10), with a placeholder credential. The real credential reaches only the proxy,
by env-var NAME (`-e NAME`), so it never appears in argv or logs and never enters a
worker container (invariant 13).
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

from engine.errors import SeamError
from sandbox.base import RunResult
from sandbox.proxy import CREDENTIAL_KINDS, HEALTH_PATH

DEFAULT_IMAGE = "seam-sandbox"  # built from sandbox/Dockerfile
WORKER_NETWORK = "seam-workers"  # --internal: no route out except via the proxy
PROXY_PORT = 8080
PROXY_SCRIPT = Path(__file__).with_name("proxy.py")  # mounted read-only into the proxy


class DockerSandbox:
    """`Sandbox` backed by `docker run --rm`. Each call is a fresh container.

    Images are never pulled implicitly (`--pull never`): the image must already be
    built or pulled. An implicit pull would download whatever the name resolves to at
    that moment, and docker's pull progress would land in the command's output (the
    task tests' output the reviewer reads, the merge-suite tail in escalation notes).
    """

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
        env_values: dict[str, str] | None = None,
    ) -> None:
        """`readonly`: paths under the work dir re-mounted read-only (e.g. a worktree's
        `.git` file, so a worker cannot repoint host git at a hostile config).
        `env_names` pass host env vars through by name; `env_values` set fixed values,
        which appear in argv, so they must never be secrets (placeholders, URLs)."""
        self.image = image
        self.memory = memory
        self.pids_limit = pids_limit
        self.docker = docker
        self.network = network
        self.env_names = env_names
        self.readonly = readonly
        self.user = user
        self.env_values = dict(env_values or {})

    def command(self, cmd: str, workdir: Path, name: str) -> list[str]:
        """The full `docker run` argv for `cmd` (exposed so the flags are testable)."""
        root = Path(workdir).resolve()
        argv = [
            self.docker, "run", "--rm", "--pull", "never", "--name", name,
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
        for k, v in self.env_values.items():
            argv += ["-e", f"{k}={v}"]
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


class ProxyError(SeamError):
    """The credential proxy or its network could not be set up."""


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class CredentialProxyContainer:
    """Lifecycle of the credential proxy (doc §10): the internal worker network and
    one proxy container that is the only holder of the real credential.

    The proxy starts on the internal network, is then connected to `bridge` for its
    own egress, and serves only clients from the internal network's subnet. Use as a
    context manager so the container is removed even on Ctrl-C.
    """

    def __init__(
        self,
        credential_env: str,
        image: str = DEFAULT_IMAGE,
        network: str = WORKER_NETWORK,
        docker: str = "docker",
        script: Path = PROXY_SCRIPT,
        runner: Runner = subprocess.run,
    ) -> None:
        if credential_env not in CREDENTIAL_KINDS:
            raise ProxyError(f"unknown credential {credential_env!r}")
        self.kind = CREDENTIAL_KINDS[credential_env]
        self.image = image
        self.network = network
        self.docker = docker
        self.script = Path(script).resolve()
        self.runner = runner
        self.name = f"seam-proxy-{uuid.uuid4().hex[:8]}"
        self.started = False
        self.log = ""

    @property
    def base_url(self) -> str:
        return f"http://{self.name}:{PROXY_PORT}"

    @property
    def health_url(self) -> str:
        return self.base_url + HEALTH_PATH

    def worker_env(self) -> dict[str, str]:
        """Env for worker containers: the proxy URL and a placeholder credential of
        the real one's kind (so Claude Code picks the matching auth mode). No secrets."""
        return {
            "ANTHROPIC_BASE_URL": self.base_url,
            self.kind.env_name: self.kind.placeholder,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }

    def _docker(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.runner([self.docker, *args], capture_output=True, text=True)

    def _ensure_network(self) -> list[str]:
        if self._docker("network", "inspect", self.network).returncode != 0:
            made = self._docker("network", "create", "--internal", self.network)
            if made.returncode != 0:
                raise ProxyError(f"cannot create network {self.network}: {made.stderr}")
        info = self._docker("network", "inspect", "-f",
                            "{{.Internal}} {{range .IPAM.Config}}{{.Subnet}} {{end}}",
                            self.network)
        internal, *subnets = info.stdout.split()
        if internal != "true":
            raise ProxyError(f"network {self.network} exists but is not --internal; "
                             f"remove it (docker network rm {self.network})")
        if not subnets:
            raise ProxyError(f"network {self.network} has no subnet")
        return subnets

    def argv(self, subnets: list[str]) -> list[str]:
        """The proxy's `docker run` argv (exposed so its flags are testable)."""
        argv = [
            self.docker, "run", "-d", "--rm", "--pull", "never", "--name", self.name,
            "--network", self.network,
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", "256m", "--pids-limit", "64",
            "-e", self.kind.env_name,  # by name: the value never enters argv
            "-v", f"{self.script}:/opt/seam/proxy.py:ro",
            self.image, "python3", "/opt/seam/proxy.py",
            "--credential-env", self.kind.env_name, "--port", str(PROXY_PORT),
        ]
        for subnet in subnets:
            argv += ["--allow", subnet]
        return argv

    def start(self) -> None:
        subnets = self._ensure_network()
        ran = self._docker(*self.argv(subnets)[1:])
        if ran.returncode != 0:
            raise ProxyError(f"cannot start proxy: {ran.stderr}")
        self.started = True
        linked = self._docker("network", "connect", "bridge", self.name)
        if linked.returncode != 0:
            self.stop()
            raise ProxyError(f"cannot give proxy egress: {linked.stderr}")

    def stop(self) -> None:
        """Remove the container, keeping its log (request lines and refusals only)."""
        if self.started:
            logs = self._docker("logs", self.name)
            self.log = (logs.stdout or "") + (logs.stderr or "")
            self._docker("rm", "-f", self.name)
            self.started = False

    def refused(self) -> list[str]:
        """Requests the proxy refused, from the log kept by `stop()`."""
        return [ln for ln in self.log.splitlines() if "refused" in ln]

    def __enter__(self) -> CredentialProxyContainer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
