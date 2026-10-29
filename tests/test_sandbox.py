import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from sandbox.base import RunResult, Sandbox
from sandbox.docker import DockerSandbox


def docker_up() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


needs_docker = pytest.mark.skipif(not docker_up(), reason="docker daemon not available")


def test_docker_sandbox_satisfies_protocol():
    assert isinstance(DockerSandbox(), Sandbox)


def test_run_result_ok_requires_zero_exit_and_no_timeout():
    assert RunResult(0, "").ok
    assert not RunResult(1, "").ok
    assert not RunResult(0, "", timed_out=True).ok


def test_command_isolates_container(tmp_path):
    argv = DockerSandbox().command("pytest", tmp_path, "seam-x")
    joined = " ".join(argv)
    for flag in ("--rm", "--network none", "--cap-drop ALL", "no-new-privileges"):
        assert flag in joined
    # Only the work dir is mounted, and the command runs inside it.
    assert argv.count("-v") == 1
    assert f"{tmp_path.resolve()}:/work" in argv
    assert argv[-3:] == ["sh", "-c", "pytest"]


def test_worker_options_pass_env_by_name_and_overlay_readonly(tmp_path):
    (tmp_path / ".git").write_text("gitdir: /somewhere\n")
    sb = DockerSandbox(network="bridge", env_names=("ANTHROPIC_API_KEY",),
                       readonly=(".git", "absent"), user="1000:1000")
    argv = sb.command("claude -p hi", tmp_path, "seam-x")
    joined = " ".join(argv)
    assert "--network bridge" in joined
    assert argv[argv.index("-e") + 1] == "ANTHROPIC_API_KEY"  # a name, never a value
    assert "=" not in argv[argv.index("-e") + 1]
    assert f"{tmp_path.resolve()}/.git:/work/.git:ro" in argv
    assert not any("absent" in a for a in argv)  # missing paths are skipped
    assert "--user 1000:1000" in joined


IMAGE = "python:3.13-slim"  # any small image works for these behavior checks


@pytest.fixture
def workdir():
    """A work dir under $HOME: Colima (and Docker Desktop by default) share only $HOME
    with containers, so a pytest tmp_path under /var/folders mounts as empty."""
    with tempfile.TemporaryDirectory(prefix=".seam-test-", dir=Path.home()) as d:
        yield Path(d)


@needs_docker
def test_exit_code_and_output_come_back(workdir):
    (workdir / "f.txt").write_text("hello")
    res = DockerSandbox(IMAGE).run("cat f.txt; exit 3", workdir, timeout=120)
    assert (res.exit_code, res.output.strip()) == (3, "hello")


@needs_docker
def test_no_network(workdir):
    cmd = "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\""
    assert not DockerSandbox(IMAGE).run(cmd, workdir, timeout=120).ok


@needs_docker
def test_timeout_kills_container(workdir):
    res = DockerSandbox(IMAGE).run("sleep 60", workdir, timeout=3)
    assert res.timed_out and not res.ok
