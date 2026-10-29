import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from sandbox.base import RunResult, Sandbox
from sandbox.docker import (
    DEFAULT_IMAGE,
    WORKER_NETWORK,
    CredentialProxyContainer,
    DockerSandbox,
    ProxyError,
)


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


class FakeDocker:
    """Records docker argv; `network inspect` fails until the network is created."""

    def __init__(self, internal="true", have_network=False, fail=()):
        self.calls: list[list[str]] = []
        self.internal, self.have_network, self.fail = internal, have_network, fail

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        args = argv[1:]
        code, out = 0, ""
        if args[:2] == ["network", "inspect"]:
            if not self.have_network:
                code = 1
            elif "-f" in args:
                out = f"{self.internal} 172.30.0.0/16 \n"
        elif args[:2] == ["network", "create"]:
            self.have_network = True
        elif args[:1] == ["logs"]:
            out = "seam-proxy: refused GET /v1/models: not an allowed endpoint\n"
        if any(args[:len(f)] == list(f) for f in self.fail):
            code = 1
        return subprocess.CompletedProcess(argv, code, out, "boom" if code else "")


def test_proxy_creates_internal_network_and_holds_credential_by_name():
    fake = FakeDocker()
    proxy = CredentialProxyContainer("CLAUDE_CODE_OAUTH_TOKEN", runner=fake)
    with proxy:
        pass
    verbs = [c[1:3] for c in fake.calls]
    assert "--internal" in fake.calls[verbs.index(["network", "create"])]
    run = next(c for c in fake.calls if c[1] == "run")
    assert run[run.index("-e") + 1] == "CLAUDE_CODE_OAUTH_TOKEN"  # a name, never a value
    assert run[run.index("--network") + 1] == WORKER_NETWORK
    assert run[run.index("--allow") + 1] == "172.30.0.0/16"
    assert any(a.endswith("proxy.py:/opt/seam/proxy.py:ro") for a in run)
    connect = fake.calls[verbs.index(["network", "connect"])]
    assert connect[1:] == ["network", "connect", "bridge", proxy.name]
    assert fake.calls[-1][1:] == ["rm", "-f", proxy.name]  # removed on exit
    assert proxy.refused() == ["seam-proxy: refused GET /v1/models: not an allowed endpoint"]


def test_worker_env_has_placeholder_and_proxy_url_only():
    proxy = CredentialProxyContainer("ANTHROPIC_API_KEY", runner=FakeDocker())
    env = proxy.worker_env()
    assert env["ANTHROPIC_BASE_URL"] == f"http://{proxy.name}:8080"
    assert env["ANTHROPIC_API_KEY"].endswith("seam-proxy-placeholder")
    argv = DockerSandbox(network=WORKER_NETWORK, env_values=env).command("x", Path("."), "n")
    assert f"ANTHROPIC_BASE_URL=http://{proxy.name}:8080" in argv


def test_proxy_refuses_a_non_internal_network():
    fake = FakeDocker(internal="false", have_network=True)
    with pytest.raises(ProxyError, match="not --internal"):
        CredentialProxyContainer("ANTHROPIC_API_KEY", runner=fake).start()
    assert not any(c[1] == "run" for c in fake.calls)


def test_proxy_is_removed_if_it_cannot_get_egress():
    fake = FakeDocker(have_network=True, fail=[("network", "connect")])
    proxy = CredentialProxyContainer("ANTHROPIC_API_KEY", runner=fake)
    with pytest.raises(ProxyError, match="egress"):
        proxy.start()
    assert fake.calls[-1][1:] == ["rm", "-f", proxy.name]


@needs_docker
def test_real_proxy_reachable_only_from_the_internal_network(monkeypatch, workdir):
    """Real containers, no model call: health answers, other endpoints are refused,
    and a container on the worker network has no other route out."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-for-this-test")
    image = DEFAULT_IMAGE
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode:
        pytest.skip(f"{image} not built")
    with CredentialProxyContainer("ANTHROPIC_API_KEY", image) as proxy:
        box = DockerSandbox(image, network=WORKER_NETWORK)
        probe = (
            "python3 - <<'EOF'\n"
            "import socket, time, urllib.request, urllib.error\n"
            "for _ in range(50):\n"
            "    try:\n"
            f"        r = urllib.request.urlopen('{proxy.health_url}', timeout=2)\n"
            "        print(r.read().decode()); break\n"
            "    except OSError: time.sleep(0.2)\n"
            "try:\n"
            f"    urllib.request.urlopen('{proxy.base_url}/v1/models', timeout=5)\n"
            "except urllib.error.HTTPError as e: print('models', e.code)\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 443), 3); print('egress open')\n"
            "except OSError: print('egress blocked')\n"
            "EOF"
        )
        out = box.run(probe, workdir, timeout=120).output
    assert out.split() == ["ok", "models", "403", "egress", "blocked"], out
