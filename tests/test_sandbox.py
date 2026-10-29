import shutil
import subprocess

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


@needs_docker
def test_exit_code_and_output_come_back(tmp_path):
    (tmp_path / "f.txt").write_text("hello")
    res = DockerSandbox().run("cat f.txt; exit 3", tmp_path, timeout=120)
    assert (res.exit_code, res.output.strip()) == (3, "hello")


@needs_docker
def test_no_network(tmp_path):
    cmd = "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\""
    assert not DockerSandbox().run(cmd, tmp_path, timeout=120).ok


@needs_docker
def test_timeout_kills_container(tmp_path):
    res = DockerSandbox().run("sleep 60", tmp_path, timeout=3)
    assert res.timed_out and not res.ok
