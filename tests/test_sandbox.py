import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from seam_merge.sandbox.base import RunResult, Sandbox
from seam_merge.sandbox.docker import (
    DEFAULT_IMAGE,
    PROXY_SCRIPT,
    WORKER_NETWORK,
    CredentialProxyContainer,
    DockerSandbox,
    ProxyError,
    host_user,
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
    for flag in ("--rm", "--pull never", "--network none", "--cap-drop ALL",
                 "no-new-privileges"):
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
    assert "HOME=/tmp" in argv  # that uid has no home in the image


def test_no_user_keeps_image_user_and_home(tmp_path):
    argv = DockerSandbox().command("true", tmp_path, "seam-x")
    assert "--user" not in argv and not any(a.startswith("HOME=") for a in argv)


def test_explicit_home_is_not_overridden(tmp_path):
    argv = DockerSandbox(user="5:5", env_values={"HOME": "/h"}).command("true", tmp_path, "x")
    assert "HOME=/h" in argv and "HOME=/tmp" not in argv


def test_host_user_only_on_linux():
    assert host_user("darwin") is None
    assert host_user("linux") == f"{os.getuid()}:{os.getgid()}"


IMAGE = "python:3.13-slim"  # any small image works for these behavior checks


@pytest.fixture(scope="module")
def image():
    """Pull the test image explicitly, once: the sandbox itself never pulls."""
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode:
        subprocess.run(["docker", "pull", "-q", IMAGE], check=True, capture_output=True)
    return IMAGE


@pytest.fixture
def workdir():
    """A work dir under $HOME: Colima (and Docker Desktop by default) share only $HOME
    with containers, so a pytest tmp_path under /var/folders mounts as empty."""
    with tempfile.TemporaryDirectory(prefix=".seam-test-", dir=Path.home()) as d:
        # TemporaryDirectory is 0700; the image's user (uid 1000) must still enter it on
        # hosts where the test user is another uid (e.g. CI runners, uid 1001).
        Path(d).chmod(0o755)
        yield Path(d)


@needs_docker
def test_exit_code_and_output_come_back(workdir, image):
    (workdir / "f.txt").write_text("hello")
    res = DockerSandbox(image).run("cat f.txt; exit 3", workdir, timeout=120)
    assert (res.exit_code, res.output.strip()) == (3, "hello")


@needs_docker
def test_no_network(workdir, image):
    cmd = "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\""
    assert not DockerSandbox(image).run(cmd, workdir, timeout=120).ok


@needs_docker
def test_host_user_can_write_workdir_and_home(workdir, image):
    """The Linux uid fix: as the host uid, a container writes the work dir (files end
    up owned by the host user) and has a writable HOME. Explicit uid, so it runs on
    any host; on Linux this is exactly what `host_user()` returns."""
    user = f"{os.getuid()}:{os.getgid()}"
    workdir.chmod(0o700)  # only the host user may write it
    res = DockerSandbox(image, user=user).run(
        "touch made && mkdir -p $HOME/.claude && echo ok", workdir, timeout=120)
    assert res.ok, res.output
    assert (workdir / "made").stat().st_uid == os.getuid()


@needs_docker
def test_timeout_kills_container(workdir, image):
    res = DockerSandbox(image).run("sleep 60", workdir, timeout=3)
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
    # The proxy's source runs inline; no host path is mounted (colima shares only $HOME).
    assert run[run.index("-c") + 1] == PROXY_SCRIPT.read_text() and "-v" not in run
    connect = fake.calls[verbs.index(["network", "connect"])]
    assert connect[1:] == ["network", "connect", "bridge", proxy.name]
    assert fake.calls[-1][1:] == ["rm", "-f", proxy.name]  # removed on exit
    assert proxy.refused() == ["seam-proxy: refused GET /v1/models: not an allowed endpoint"]


def test_proxy_token_cap_reaches_the_proxy_and_its_total_is_read_back():
    capped = CredentialProxyContainer("ANTHROPIC_API_KEY", runner=FakeDocker(),
                                      token_cap=1234)
    argv = capped.argv(["10.0.0.0/8"])
    assert argv[argv.index("--token-cap") + 1] == "1234"
    assert "--token-cap" not in CredentialProxyContainer(
        "ANTHROPIC_API_KEY", runner=FakeDocker()).argv(["10.0.0.0/8"])
    assert capped.metered() is None  # nothing logged yet
    capped.log = ("seam-proxy: metered 5 tokens (total 5 of 1234)\n"
                  "seam-proxy: metered 7 tokens (total 12 of 1234)\n")
    assert capped.metered() == 12


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



@needs_docker
def test_a_missing_image_fails_instead_of_being_pulled(workdir):
    res = DockerSandbox("seam-no-such-image:never").run("true", workdir, timeout=60)
    assert not res.ok and "Pulling" not in res.output


@pytest.mark.parametrize("code, env", [(127, True), (126, True), (125, True), (1, False),
                                       (2, False), (0, False), (-1, False)])
def test_env_failure_means_the_command_never_ran(code, env):
    assert RunResult(code, "").env_failure is env
    assert not RunResult(127, "", timed_out=True).env_failure  # a timeout ran the code


# -- the sandbox image, built from the Dockerfile shipped in the package --------------

def test_image_build_sends_no_context_and_can_override_the_pin():
    from seam_merge.sandbox.docker import build_image_argv

    assert build_image_argv("img") == ["docker", "build", "-t", "img", "-"]
    assert build_image_argv("img", "2.0.0") == [
        "docker", "build", "-t", "img", "--build-arg", "CLAUDE_CODE_VERSION=2.0.0", "-"]


def test_packaged_dockerfile_pins_claude_code():
    from seam_merge.sandbox.docker import DOCKERFILE

    pins = [ln for ln in DOCKERFILE.read_text().splitlines()
            if ln.startswith("ARG CLAUDE_CODE_VERSION=")]
    assert len(pins) == 1 and pins[0].split("=", 1)[1] not in ("", "latest")


def test_build_image_feeds_the_dockerfile_on_stdin(monkeypatch):
    from seam_merge.sandbox import docker as D

    seen = {}

    def fake_run(argv, stdin):
        seen["argv"], seen["stdin"] = argv, stdin.read()
        return subprocess.CompletedProcess(argv, seen.get("code", 0))

    monkeypatch.setattr(D.subprocess, "run", fake_run)
    D.build_image("img")
    assert seen["argv"][-1] == "-" and seen["stdin"] == D.DOCKERFILE.read_bytes()
    seen["code"] = 1
    with pytest.raises(D.ImageBuildFailed):
        D.build_image("img")
