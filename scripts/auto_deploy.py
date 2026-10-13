"""One-command deploy of AI Civilization to a VPS (Debian/Ubuntu) over SSH.

    python -m scripts.auto_deploy --host <IP>

Connection settings come from CLI flags or env: VPS_HOST, VPS_PORT, VPS_USER,
VPS_PASSWORD, VPS_SSH_KEY_PATH. The only third-party dependency is paramiko.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import shlex
import sys
import time
from pathlib import Path

try:
    import paramiko
except ImportError:  # pragma: no cover
    sys.exit("paramiko is required: pip install paramiko")

try:
    from scripts import deploy_pack
except ImportError:  # run as a plain file
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts import deploy_pack

TOTAL = 9
CMD_TIMEOUT = 900  # per-read timeout on the SSH channel: a silent command for 15 min is a hang
BAD_LOG = re.compile(r"Traceback \(most recent call last\)|\bCRITICAL\b|\bFATAL\b|ConfigError|RlsNotEnforcedError", re.I)

if os.name == "nt":
    os.system("")  # enables ANSI escapes in the Windows console
_COLOR = sys.stdout.isatty() or os.name == "nt"


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def step(n: int, title: str) -> None:
    print("\n" + _c("1;36", f"[{n}/{TOTAL}] {title}"))


def ok(msg: str) -> None:
    print(_c("32", f"  [ok] {msg}"))


def warn(msg: str) -> None:
    print(_c("33", f"  [!] {msg}"))


def fail(msg: str) -> None:
    print(_c("1;31", f"  [x] {msg}"), file=sys.stderr)


class DeployError(RuntimeError):
    pass


class Remote:
    def __init__(self, client: paramiko.SSHClient, user: str, password: str | None):
        self.client = client
        self.is_root = user == "root"
        self.password = password
        self.docker_direct = False  # can the user talk to the docker daemon without sudo?

    def run(self, cmd: str, *, sudo: bool = False, check: bool = True, quiet: bool = False,
            timeout: int = CMD_TIMEOUT) -> tuple[int, str]:
        """Run a command, stream its output, return (exit_code, output)."""
        feed_password = False
        if sudo and not self.is_root:
            if self.password:
                cmd = f"sudo -S -p '' bash -c {shlex.quote(cmd)}"
                feed_password = True
            else:
                cmd = f"sudo -n bash -c {shlex.quote(cmd)}"
        stdin, stdout, _ = self.client.exec_command(cmd, timeout=timeout)
        stdout.channel.set_combine_stderr(True)
        if feed_password:
            stdin.write(self.password + "\n")
            stdin.flush()
        stdin.channel.shutdown_write()
        lines: list[str] = []
        for line in iter(stdout.readline, ""):
            line = line.rstrip("\n")
            lines.append(line)
            if not quiet:
                print("    " + line)
        code = stdout.channel.recv_exit_status()
        if check and code != 0:
            raise DeployError(f"command failed (exit {code}): {cmd.splitlines()[0][:120]}")
        return code, "\n".join(lines)

    @property
    def docker(self) -> str:
        return "docker" if self.is_root or self.docker_direct else "sudo docker"


def connect(args: argparse.Namespace) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy() if args.strict_host_key else paramiko.AutoAddPolicy())
    kwargs: dict = dict(hostname=args.host, port=args.port, username=args.user,
                        timeout=15, banner_timeout=15, auth_timeout=15)
    if args.key:
        kwargs["key_filename"] = str(Path(args.key).expanduser())
        kwargs["passphrase"] = os.environ.get("VPS_SSH_KEY_PASSPHRASE") or None
    if args.password:
        kwargs["password"] = args.password
    client.connect(**kwargs)
    return client


def resolve_args() -> argparse.Namespace:
    env = os.environ.get
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default=env("VPS_HOST"), help="server IP / hostname (env VPS_HOST)")
    p.add_argument("--port", type=int, default=int(env("VPS_PORT", "22")), help="SSH port (env VPS_PORT, default 22)")
    p.add_argument("--user", default=env("VPS_USER", "root"), help="SSH user (env VPS_USER, default root)")
    p.add_argument("--password", default=env("VPS_PASSWORD"),
                   help="SSH/sudo password (prefer env VPS_PASSWORD: CLI args are visible in process lists)")
    p.add_argument("--key", default=env("VPS_SSH_KEY_PATH"), help="private key path (env VPS_SSH_KEY_PATH)")
    p.add_argument("--remote-dir", default="ai_civ", help="target dir relative to home (default ai_civ)")
    p.add_argument("--migrate-service", default="bot", help="compose service used for alembic (default bot)")
    p.add_argument("--health-wait", type=int, default=15, help="seconds to wait before the health check (default 15)")
    p.add_argument("--keep-archive", action="store_true", help="do not delete the local archive afterwards")
    p.add_argument("--strict-host-key", action="store_true",
                   help="refuse hosts missing from known_hosts (default: trust on first use)")
    args = p.parse_args()
    if not args.host:
        p.error("--host (or VPS_HOST) is required")
    if not args.key and not args.password and sys.stdin.isatty():
        args.password = getpass.getpass(
            f"Password for {args.user}@{args.host} (empty = ssh-agent/default keys): ") or None
    return args


def ensure_docker(r: Remote) -> None:
    code, _ = r.run("docker --version", check=False, quiet=True)
    if code == 0:
        ok("Docker is installed")
    else:
        warn("Docker not found, installing (Debian/Ubuntu)")
        _, osr = r.run("cat /etc/os-release", quiet=True)
        if not re.search(r"^(ID|ID_LIKE)=.*\b(debian|ubuntu)\b", osr.replace('"', ""), re.M):
            raise DeployError("automatic Docker installation is supported only on Debian/Ubuntu")
        r.run("export DEBIAN_FRONTEND=noninteractive; apt-get update -y && apt-get install -y curl ca-certificates "
              "&& curl -fsSL https://get.docker.com | sh && systemctl enable --now docker", sudo=True)
        ok("Docker installed")
    code, _ = r.run("docker info", check=False, quiet=True)
    r.docker_direct = code == 0
    code, ver = r.run(f"{r.docker} compose version", check=False, quiet=True)
    if code != 0:
        warn("Docker Compose plugin not found, installing")
        r.run("export DEBIAN_FRONTEND=noninteractive; apt-get update -y && apt-get install -y docker-compose-plugin",
              sudo=True)
        code, ver = r.run(f"{r.docker} compose version", check=False, quiet=True)
        if code != 0:
            raise DeployError("Docker Compose is still unavailable after installation")
    ok(ver.strip())


# Services that run once and stop (docker-compose.yml: `migrate` applies the migrations and exits). For them "Exited (0)" is success.
ONE_SHOT_SERVICES = frozenset({"migrate"})
_EXITED_OK = re.compile(r"^exited \(0\)", re.I)


def service_ok(service: str, state: str, status: str) -> bool:
    """Is this container in the state a successful deploy leaves it in? A long-running service must be running (not restarting / unhealthy);
    a one-shot service must have finished with exit code 0 - any other exit code is a failed deploy."""
    low = status.lower()
    if service in ONE_SHOT_SERVICES:
        return state == "exited" and _EXITED_OK.match(status.strip()) is not None
    return state == "running" and "restarting" not in low and "unhealthy" not in low


def health_check(r: Remote, workdir: str, wait: int) -> bool:
    d = r.docker
    time.sleep(wait)
    fmt = "'{{.Service}}|{{.State}}|{{.Status}}'"
    _, ps = r.run(f"cd {workdir} && {d} compose ps -a --format {fmt}", quiet=True)
    if not ps.strip():
        fail("no containers found")
        return False
    healthy = True
    for line in filter(None, ps.splitlines()):
        service, _, rest = line.partition("|")
        state, _, status = rest.partition("|")
        if service_ok(service, state, status):
            ok(f"{service}: {status}")
        else:
            fail(f"{service}: {state} ({status})")
            healthy = False
    _, logs = r.run(f"cd {workdir} && {d} compose logs --tail 200 --no-color", quiet=True)
    bad = [ln for ln in logs.splitlines() if BAD_LOG.search(ln)]
    if bad:
        fail(f"{len(bad)} suspicious log line(s); first ones:")
        for ln in bad[:10]:
            print("    " + ln)
        healthy = False
    else:
        ok("no tracebacks in the last 200 log lines")
    return healthy


def deploy(args: argparse.Namespace) -> int:
    step(1, "Building archive")
    archive = deploy_pack.build_archive()
    try:
        ok(f"{archive.name} ({archive.stat().st_size / 1024 / 1024:.2f} MB)")

        step(2, f"Connecting to {args.user}@{args.host}:{args.port} and uploading")
        client = connect(args)
        try:
            r = Remote(client, args.user, args.password)
            sftp = client.open_sftp()
            home = sftp.normalize(".")
            remote_archive = f"{home}/{archive.name}"
            sftp.put(str(archive), remote_archive)
            sftp.close()
            ok(f"uploaded to {remote_archive}")

            step(3, "Checking Docker / Docker Compose")
            ensure_docker(r)
            d = r.docker
            workdir = shlex.quote(f"{home}/{args.remote_dir.strip('/')}")

            step(4, f"Unpacking into ~/{args.remote_dir}")
            r.run(f"mkdir -p {workdir} && cd {workdir} && rm -rf src migrations web/dist "
                  f"&& tar xzf {shlex.quote(remote_archive)} && rm -f {shlex.quote(remote_archive)}")
            ok("unpacked")

            step(5, "Securing .env")
            r.run(f"chmod 600 {workdir}/.env")
            ok(".env -> 600")

            step(6, "Stopping old containers")
            r.run(f"cd {workdir} && {d} compose down")

            step(7, "Building and starting containers")
            r.run(f"cd {workdir} && {d} compose --progress=plain up -d --build")

            step(8, "Applying database migrations")
            svc = args.migrate_service
            code, _ = r.run(f"cd {workdir} && {d} compose exec -T {svc} alembic upgrade head", check=False)
            if code != 0:
                warn(f"exec in '{svc}' failed (container may not be running); retrying in a one-off container")
                r.run(f"cd {workdir} && {d} compose run --rm -T {svc} alembic upgrade head")
            ok("alembic upgrade head done")

            step(9, "Health check")
            healthy = health_check(r, workdir, args.health_wait)
            print()
            if healthy:
                print(_c("1;32", "Deploy finished: all services are running."))
                return 0
            fail(f"Deploy finished with problems. Inspect: ssh {args.user}@{args.host} "
                 f"'cd ~/{args.remote_dir} && docker compose logs --tail 100'")
            return 1
        finally:
            client.close()
    finally:
        if not args.keep_archive:
            archive.unlink(missing_ok=True)


def main() -> int:
    args = resolve_args()
    try:
        return deploy(args)
    except FileNotFoundError as exc:
        fail(str(exc))
    except paramiko.AuthenticationException:
        fail("SSH authentication failed (check VPS_USER / VPS_PASSWORD / VPS_SSH_KEY_PATH)")
    except (paramiko.SSHException, OSError, DeployError) as exc:
        fail(f"{type(exc).__name__}: {exc}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
