#!/usr/bin/env python3
"""Prepare one LAN Ray worker for AMDockVS/MolSuite and verify both transports."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


RAY_PORT = 6379
MINICONDA_RELEASE = "Miniconda3-py312_26.7.1-1-Linux"
MINICONDA = {
    "x86_64": "b27f60ab63e77eeab50a5417c989120f767e863df32400190d4c7262369f8695",
    "aarch64": "f6d64a1565e713429683720f59dd6661f5131c2f959a4830438eb1969cde23f7",
}

CLUSTER_CODE = r"""
import os
from pathlib import Path

from ms_flow.core.executor.provisioning import cluster_down_steps, cluster_up_steps, run_steps
from ms_flow.core.settings.models import RayWorkerConfig

worker = RayWorkerConfig(
    name=os.environ["MOLSUITE_CLUSTER_NAME"],
    mode="managed",
    native=True,
    shared_fs=os.environ["MOLSUITE_SHARED_FS"] == "1",
    head_ip=os.environ["MOLSUITE_HEAD_IP"],
    worker_ips=[os.environ["MOLSUITE_WORKER_IP"]],
    ssh_user=os.environ["MOLSUITE_SSH_USER"],
    conda_env=os.environ["MOLSUITE_CONDA_ENV"],
    cpus=int(os.environ["MOLSUITE_HEAD_CPUS"]),
    gpus=0,
)
root = Path(os.environ["MOLSUITE_CLUSTER_ROOT"])
if os.environ["MOLSUITE_CLUSTER_ACTION"] == "up":
    steps = cluster_up_steps(worker, root=root)
else:
    steps = cluster_down_steps(worker, root=root)
run_steps(
    steps,
    on_step=lambda index, step: print(f"[{index + 1}/{len(steps)}] {step.name}"),
    on_line=print,
)
"""

PROBE_CODE = r"""
import os
from pathlib import Path

from ms_flow.api import FileInputSpec, MolSuite

root = Path(os.environ["MOLSUITE_PROJECT_ROOT"])
address = os.environ["MOLSUITE_RAY_ADDRESS"]
worker_ip = os.environ["MOLSUITE_WORKER_IP"]
probe_cpus = int(os.environ["MOLSUITE_PROBE_CPUS"])
ms = MolSuite(app_id="molsuite-cluster-probe")
try:
    ms.create_or_open_project(name="ray_cluster_probe", folder=root, activate=True)

    if os.environ["MOLSUITE_TEST_SHARED"] == "1":
        ms.register_ray_executor(
            name="ray-shared",
            mode="external",
            shared_fs=True,
            native=True,
            address=address,
            namespace="molsuite-probe",
        )
        job_id = ms.run(
            name="ray_shared_probe",
            input=[{"probe": "shared-fs-ok"}],
            process="ms_flow.core.executor.backend_selector:_healthcheck_noop",
            executor="ray-shared",
            cpu_required=probe_cpus,
            store_results=True,
        )
        result = ms.wait_for_job(job_id, poll_s=0.1)
        assert result.status == "completed", result
        assert "shared-fs-ok" in repr(ms.get_job_outputs(job_id))
        ms.unregister_executor("ray-shared")

    ms.register_ray_executor(
        name="ray-staged",
        mode="external",
        shared_fs=False,
        native=True,
        address=address,
        namespace="molsuite-probe",
    )
    job_id = ms.run(
        name="ray_object_staging_probe",
        input=[{
            "probe": FileInputSpec(
                os.environ["MOLSUITE_PROBE_FILE"],
                fmt="text",
                delivery="content",
                cache=True,
            )
        }],
        process="ms_flow.core.executor.backend_selector:_healthcheck_noop",
        executor="ray-staged",
        cpu_required=probe_cpus,
        store_results=True,
    )
    result = ms.wait_for_job(job_id, poll_s=0.1)
    assert result.status == "completed", result
    assert "molsuite-ray-object-staging-ok" in repr(ms.get_job_outputs(job_id))

    import ray

    alive = {
        str(node.get("NodeManagerAddress"))
        for node in ray.nodes()
        if node.get("Alive")
    }
    assert worker_ip in alive, f"Worker {worker_ip} did not join; alive nodes={sorted(alive)}"
    print(f"PASS shared_fs={os.environ['MOLSUITE_TEST_SHARED'] == '1'}")
    print("PASS shared_fs=False object staging")
    print(f"PASS Ray worker joined: {worker_ip}")
finally:
    ms.shutdown()
"""


@dataclass(frozen=True)
class LanRayBootstrapConfig:
    """Values that can later be populated from an ms_flow settings model."""

    worker_host: str
    ssh_user: str
    projects_root: Path
    env_name: str = "molsuite_env"
    ray_version: str = "2.52.0"
    head_cpus: int = 1
    shared_fs: bool = True
    with_amdock: bool = True
    managed_tools: tuple[str, ...] = ()
    assume_yes: bool = False
    cluster_name: str = "amdock-lan"

    def validate(self) -> None:
        local_user = os.environ.get("USER") or Path.home().name
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", self.worker_host):
            raise ValueError("Invalid worker host.")
        if self.ssh_user != local_user:
            raise ValueError("MolSuite's Ray local provider requires the same SSH user on both nodes.")
        if not self.env_name or not all(c.isalnum() or c in "._-" for c in self.env_name):
            raise ValueError("Invalid environment name.")
        if self.head_cpus < 1:
            raise ValueError("head_cpus must be positive.")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", self.ray_version):
            raise ValueError("Invalid Ray version.")
        unknown = set(self.managed_tools) - {"openbabel", "pkasso", "p2rank"}
        if unknown:
            raise ValueError(f"Unknown managed tools: {', '.join(sorted(unknown))}")
        if self.managed_tools and not self.with_amdock:
            raise ValueError("Managed tools require the AMDock stack.")


class LanRayBootstrap:
    """Idempotent bootstrap with persistent installs and temporary Ray/SSHFS state."""

    def __init__(self, config: LanRayBootstrapConfig) -> None:
        config.validate()
        self.config = config
        self.local_user = os.environ.get("USER") or Path.home().name
        self.home = Path.home()
        self.amdock_root = Path(__file__).resolve().parent.parent
        self.source_parent = self.amdock_root.parent
        self.ssh_key = self.home / ".ssh" / "id_ed25519"
        self.ssh_target = f"{config.ssh_user}@{config.worker_host}"
        self.projects_root = config.projects_root.expanduser().resolve()
        self.cluster_root = self.home / ".molsuite" / "clusters"
        self.head_ip = ""
        self.worker_ip = ""
        self.remote_home = ""
        self.remote_projects_root = ""
        self.worker_cpus = 0
        self.local_conda = ""
        self.remote_conda = ""
        self.env_python = ""
        self.cluster_attempted = False
        self.shared_mounted = False
        self.wheelhouse: Path | None = None
        self.probe_root: Path | None = None

    @property
    def ssh_options(self) -> list[str]:
        return [
            "-i", str(self.ssh_key),
            "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=accept-new",
        ]

    @staticmethod
    def log(message: str) -> None:
        print(f"\n==> {message}", flush=True)

    @staticmethod
    def run_command(
        argv: list[str],
        *,
        capture: bool = False,
        check: bool = True,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            argv,
            text=True,
            input=input_text,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            env=env,
            check=False,
        )
        if check and result.returncode:
            detail = (result.stderr or result.stdout or "").strip()
            message = f"Command failed ({result.returncode}): {shlex.join(argv)}"
            raise RuntimeError(f"{message}: {detail}" if detail else message)
        return result

    def ssh_shell(
        self,
        command: str,
        *,
        tty: bool = False,
        capture: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        argv = ["ssh"]
        if tty:
            argv.append("-tt")
        argv.extend([*self.ssh_options, self.ssh_target, command])
        return self.run_command(argv, capture=capture, check=check)

    def ssh_run(self, *argv: str, **kwargs) -> subprocess.CompletedProcess[str]:
        return self.ssh_shell(shlex.join(argv), **kwargs)

    def remote_conda_run(self, *argv: str, **kwargs) -> subprocess.CompletedProcess[str]:
        return self.ssh_run(
            self.remote_conda,
            "run",
            "--no-capture-output",
            "-n",
            self.config.env_name,
            *argv,
            **kwargs,
        )

    def ensure_local_packages(self) -> None:
        commands = ("ssh", "sshd", "ssh-copy-id", "scp", "rsync")
        missing = [command for command in commands if shutil.which(command) is None]
        if not missing:
            return
        if shutil.which("apt-get") is None:
            raise RuntimeError(
                f"Missing local commands: {', '.join(missing)}; automatic install supports apt only."
            )
        self.log(f"Installing local SSH prerequisites: {', '.join(missing)}")
        self.run_command(["sudo", "apt-get", "update"])
        self.run_command(["sudo", "apt-get", "install", "-y", "openssh-client", "openssh-server", "rsync"])

    def ensure_key_authentication(self) -> None:
        self.ssh_key.parent.mkdir(parents=True, exist_ok=True)
        self.ssh_key.parent.chmod(0o700)
        if not self.ssh_key.exists():
            self.run_command([
                "ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                "-f", str(self.ssh_key), "-C", f"molsuite@{socket.gethostname()}",
            ])
        public_key = self.ssh_key.with_suffix(".pub")
        if not public_key.exists():
            material = self.run_command(["ssh-keygen", "-y", "-f", str(self.ssh_key)], capture=True).stdout
            public_key.write_text(material, encoding="utf-8")
            public_key.chmod(0o644)

        if self.ssh_run("true", capture=True, check=False).returncode:
            self.log(f"Installing the public key on {self.ssh_target}; SSH will request the password once")
            self.run_command([
                "ssh-copy-id", "-i", str(public_key),
                "-o", "StrictHostKeyChecking=accept-new", self.ssh_target,
            ])
        self.ssh_run("true", capture=True)

    def authorize_local_key(self, public_key: str) -> None:
        ssh_dir = self.home / ".ssh"
        authorized = ssh_dir / "authorized_keys"
        ssh_dir.mkdir(parents=True, exist_ok=True)
        ssh_dir.chmod(0o700)
        existing = authorized.read_text(encoding="utf-8").splitlines() if authorized.exists() else []
        if public_key not in existing:
            needs_newline = (
                authorized.exists()
                and authorized.stat().st_size
                and not authorized.read_text(encoding="utf-8").endswith("\n")
            )
            with authorized.open("a", encoding="utf-8") as stream:
                if needs_newline:
                    stream.write("\n")
                stream.write(public_key + "\n")
        authorized.chmod(0o600)

    def resolve_hosts(self) -> None:
        try:
            self.worker_ip = socket.gethostbyname(self.config.worker_host)
        except socket.gaierror as exc:
            raise RuntimeError(f"Could not resolve worker host: {self.config.worker_host}") from exc
        route = self.run_command(["ip", "route", "get", self.config.worker_host], capture=True).stdout.split()
        try:
            self.head_ip = route[route.index("src") + 1]
        except (ValueError, IndexError) as exc:
            raise RuntimeError(f"Could not determine the local IP used to reach {self.config.worker_host}.") from exc
        self.remote_home = self.ssh_shell('printf "%s" "$HOME"', capture=True).stdout.strip()
        if not self.remote_home.startswith("/"):
            raise RuntimeError("Could not determine the worker home directory.")
        self.worker_cpus = int(self.ssh_run("nproc", capture=True).stdout.strip())
        if self.worker_cpus <= self.config.head_cpus:
            raise RuntimeError(
                f"Worker CPUs ({self.worker_cpus}) must exceed head_cpus ({self.config.head_cpus}) "
                "so probes can be forced onto the worker."
            )
        default_root = self.home / "molsuite_projects"
        self.remote_projects_root = (
            str(PurePosixPath(self.remote_home) / "molsuite_projects")
            if self.projects_root == default_root
            else str(self.projects_root)
        )

    def prepare_directories(self) -> None:
        self.log("Preparing project roots and MolSuite state directories")
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.cluster_root.mkdir(parents=True, exist_ok=True)
        self.ssh_run("mkdir", "-p", self.remote_projects_root, f"{self.remote_home}/.molsuite")

    def ensure_remote_packages(self) -> None:
        commands = ["ssh", "rsync"]
        packages = ["openssh-client", "rsync"]
        if self.config.shared_fs:
            commands.extend(["sshfs", "fusermount3", "mountpoint"])
            packages.extend(["sshfs", "fuse3"])
        check = "for c in " + " ".join(commands) + "; do command -v \"$c\" >/dev/null || printf '%s ' \"$c\"; done"
        missing = self.ssh_shell(check, capture=True).stdout.strip()
        if not missing:
            return
        self.log(f"Installing remote SSH prerequisites: {missing}")
        command = (
            "command -v apt-get >/dev/null || { echo 'Automatic remote install supports apt only.' >&2; exit 1; }; "
            f"sudo apt-get update && sudo apt-get install -y {' '.join(packages)}"
        )
        self.ssh_shell(command, tty=True)

    def ensure_head_ssh(self) -> None:
        public_key = self.ssh_key.with_suffix(".pub").read_text(encoding="utf-8").strip()
        self.authorize_local_key(public_key)
        target = f"{self.local_user}@{self.head_ip}"
        command = ["ssh", *self.ssh_options, target, "true"]
        if self.run_command(command, capture=True, check=False).returncode:
            self.run_command(["sudo", "systemctl", "enable", "--now", "ssh"])
        self.run_command(command, capture=True)

    @staticmethod
    def conda_candidates(home: Path) -> tuple[Path, ...]:
        return (
            home / "programs/miniconda3/bin/conda",
            home / "miniconda3/bin/conda",
            home / "anaconda3/bin/conda",
        )

    def find_local_conda(self) -> str | None:
        on_path = shutil.which("conda")
        if on_path:
            return on_path
        return next(
            (str(path) for path in self.conda_candidates(self.home) if os.access(path, os.X_OK)),
            None,
        )

    def find_remote_conda(self) -> str | None:
        candidates = [
            f"{self.remote_home}/programs/miniconda3/bin/conda",
            f"{self.remote_home}/miniconda3/bin/conda",
            f"{self.remote_home}/anaconda3/bin/conda",
        ]
        command = (
            "for c in " + " ".join(map(shlex.quote, candidates))
            + '; do test -x "$c" && { printf "%s" "$c"; exit; }; done'
        )
        result = self.ssh_shell(command, capture=True, check=False)
        return result.stdout.strip() or None

    @staticmethod
    def normalized_arch(raw: str) -> str:
        return "aarch64" if raw in {"aarch64", "arm64"} else raw

    def download_miniconda(self, arch: str, destination: Path) -> None:
        arch = self.normalized_arch(arch)
        checksum = MINICONDA.get(arch)
        if checksum is None:
            raise RuntimeError(f"Unsupported architecture for Miniconda: {arch}")
        url = f"https://repo.anaconda.com/miniconda/{MINICONDA_RELEASE}-{arch}.sh"
        self.log(f"Downloading verified Miniconda for {arch}")
        urllib.request.urlretrieve(url, destination)
        digest = hashlib.sha256()
        with destination.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != checksum:
            raise RuntimeError(f"Miniconda checksum mismatch: expected {checksum}, got {actual}")

    def ensure_conda(self) -> None:
        self.local_conda = self.find_local_conda() or ""
        if not self.local_conda:
            with tempfile.TemporaryDirectory(prefix="molsuite-miniconda-") as temporary:
                installer = Path(temporary) / "miniconda.sh"
                self.download_miniconda(os.uname().machine, installer)
                self.run_command(["bash", str(installer), "-b", "-p", str(self.home / "miniconda3")])
            self.local_conda = str(self.home / "miniconda3/bin/conda")

        self.remote_conda = self.find_remote_conda() or ""
        if not self.remote_conda:
            remote_arch = self.ssh_run("uname", "-m", capture=True).stdout.strip()
            with tempfile.TemporaryDirectory(prefix="molsuite-miniconda-") as temporary:
                installer = Path(temporary) / "miniconda.sh"
                self.download_miniconda(remote_arch, installer)
                remote_installer = f"/tmp/molsuite-miniconda-{uuid.uuid4().hex}.sh"
                self.run_command([
                    "scp", *self.ssh_options, str(installer),
                    f"{self.ssh_target}:{remote_installer}",
                ])
                install = (
                    f"trap 'rm -f {shlex.quote(remote_installer)}' EXIT; "
                    f"bash {shlex.quote(remote_installer)} -b -p "
                    f"{shlex.quote(self.remote_home + '/miniconda3')}"
                )
                self.ssh_shell(install)
            self.remote_conda = f"{self.remote_home}/miniconda3/bin/conda"

    def ensure_environment(self) -> None:
        env_name = self.config.env_name
        local_check = [self.local_conda, "run", "-n", env_name, "python", "-V"]
        if self.run_command(local_check, capture=True, check=False).returncode:
            self.log(f"Creating local environment {env_name}")
            self.run_command([
                self.local_conda, "create", "-y", "-n", env_name,
                "-c", "conda-forge", "python=3.12", "pip",
            ])
        remote_check = self.ssh_run(
            self.remote_conda, "run", "-n", env_name, "python", "-V",
            capture=True, check=False,
        )
        if remote_check.returncode:
            self.log(f"Creating remote environment {env_name}")
            self.ssh_run(
                self.remote_conda, "create", "-y", "-n", env_name,
                "-c", "conda-forge", "python=3.12", "pip",
            )

        self.env_python = self.run_command(
            [
                self.local_conda, "run", "--no-capture-output", "-n", env_name,
                "python", "-c", "import sys; print(sys.executable)",
            ],
            capture=True,
        ).stdout.strip()
        local_bin = self.home / ".local/bin"
        local_bin.mkdir(parents=True, exist_ok=True)
        conda_link = local_bin / "conda"
        if not os.path.lexists(conda_link):
            conda_link.symlink_to(self.local_conda)
        remote_bin = f"{self.remote_home}/.local/bin"
        self.ssh_shell(
            f"mkdir -p {shlex.quote(remote_bin)}; test -e {shlex.quote(remote_bin + '/conda')} || "
            f"test -L {shlex.quote(remote_bin + '/conda')} || "
            f"ln -s {shlex.quote(self.remote_conda)} {shlex.quote(remote_bin + '/conda')}"
        )

        login_check = f"source ~/.bashrc; conda run -n {shlex.quote(env_name)} python -V"
        shell_argv = shlex.join(["bash", "--login", "-c", "-i", login_check])
        head_target = f"{self.local_user}@{self.head_ip}"
        self.run_command(["ssh", *self.ssh_options, head_target, shell_argv], capture=True)
        self.ssh_shell(shell_argv, capture=True)

    def local_env_run(self, *argv: str, **kwargs) -> subprocess.CompletedProcess[str]:
        return self.run_command([
            self.local_conda,
            "run",
            "--no-capture-output",
            "-n",
            self.config.env_name,
            *argv,
        ], **kwargs)

    def build_and_install_stack(self) -> None:
        sources = [self.source_parent / "ms_flow"]
        if self.config.with_amdock:
            sources.extend([
                self.source_parent / "ms_components",
                self.source_parent / "ms_contactmap",
                self.amdock_root,
            ])
        missing = [str(source) for source in sources if not (source / "pyproject.toml").is_file()]
        if missing:
            raise RuntimeError(f"Missing source repositories: {', '.join(missing)}")

        self.wheelhouse = Path(tempfile.mkdtemp(prefix="molsuite-wheels-"))
        self.local_env_run("python", "-m", "pip", "install", "-q", "--upgrade", "pip", "build")
        self.local_env_run(
            "python", "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(self.wheelhouse),
            *map(str, sources),
        )
        wheels = sorted(self.wheelhouse.glob("*.whl"))
        if not wheels:
            raise RuntimeError("No wheels were built.")

        remote_cache = f"{self.remote_home}/.molsuite/wheels/{self.config.cluster_name}"
        self.ssh_run("mkdir", "-p", remote_cache)
        ssh_transport = shlex.join(["ssh", *self.ssh_options])
        self.run_command([
            "rsync", "-a", "--delete", "-e", ssh_transport,
            f"{self.wheelhouse}/", f"{self.ssh_target}:{remote_cache}/",
        ])

        self.log(f"Installing Ray {self.config.ray_version} and the identical MolSuite stack on both hosts")
        ray_requirement = f"ray[default]=={self.config.ray_version}"
        self.local_env_run("python", "-m", "pip", "install", ray_requirement)
        self.remote_conda_run("python", "-m", "pip", "install", ray_requirement)
        self.local_env_run("python", "-m", "pip", "install", *map(str, wheels))
        remote_wheels = [str(PurePosixPath(remote_cache) / wheel.name) for wheel in wheels]
        self.remote_conda_run("python", "-m", "pip", "install", *remote_wheels)

        if self.config.with_amdock:
            extras = ("bblean>=0.10", "jdk4py~=21.0", "openmm>=8.1", "pdbfixer>=1.9", "prody>=2.4")
            self.local_env_run("python", "-m", "pip", "install", *extras)
            self.remote_conda_run("python", "-m", "pip", "install", *extras)
            for remote in (False, True):
                try:
                    if remote:
                        self.ssh_run(
                            self.remote_conda, "install", "-y", "-n", self.config.env_name,
                            "-c", "conda-forge", "autogrid", "reduce",
                        )
                    else:
                        self.run_command([
                            self.local_conda, "install", "-y", "-n", self.config.env_name,
                            "-c", "conda-forge", "autogrid", "reduce",
                        ])
                except RuntimeError as exc:
                    print(f"WARNING: optional autogrid/reduce install failed: {exc}", file=sys.stderr)

    def install_managed_tools(self) -> None:
        for tool in self.config.managed_tools:
            self.log(f"Installing AMDock-managed tool: {tool}")
            code = (
                "from amdockvs import AMDockVSRuntime; "
                "from amdockvs.integrations.registry import install_steps; "
                "from ms_flow.core.executor.provisioning import run_steps; "
                f"rt=AMDockVSRuntime(); run_steps(install_steps(rt, {tool!r}), on_line=print); rt.shutdown()"
            )
            self.local_env_run("python", "-c", code)
            self.remote_conda_run("python", "-c", code)

    def ensure_reverse_key(self) -> None:
        command = (
            'mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"; '
            'test -f "$HOME/.ssh/id_ed25519" || '
            'ssh-keygen -q -t ed25519 -N "" -f "$HOME/.ssh/id_ed25519" -C "molsuite-worker@$(hostname)"'
        )
        self.ssh_shell(command)
        remote_key = self.ssh_shell('cat "$HOME/.ssh/id_ed25519.pub"', capture=True).stdout.strip()
        self.authorize_local_key(remote_key)
        self.ssh_run(
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=accept-new", f"{self.local_user}@{self.head_ip}", "true",
        )

    def mount_shared_projects(self) -> None:
        if not self.config.shared_fs:
            return
        if self.remote_projects_root != str(self.projects_root):
            raise RuntimeError(f"SSHFS needs the same absolute project path on both hosts: {self.projects_root}")
        if not self.ssh_run("mountpoint", "-q", self.remote_projects_root, check=False).returncode:
            raise RuntimeError(f"Remote project root is already mounted: {self.remote_projects_root}")
        nonempty = self.ssh_shell(
            f"find {shlex.quote(self.remote_projects_root)} -mindepth 1 -maxdepth 1 -print -quit",
            capture=True,
        ).stdout.strip()
        if nonempty:
            raise RuntimeError(f"Remote project root is not empty: {self.remote_projects_root}")

        self.log("Mounting the head project root on the worker through SSHFS")
        source = f"{self.local_user}@{self.head_ip}:{self.projects_root}"
        self.ssh_run(
            "sshfs", source, self.remote_projects_root,
            "-o", "reconnect,ServerAliveInterval=15,ServerAliveCountMax=3,StrictHostKeyChecking=yes",
        )
        self.shared_mounted = True
        with tempfile.NamedTemporaryFile(dir=self.projects_root, prefix=".molsuite-shared-probe-") as marker:
            self.ssh_run("test", "-f", marker.name)

    def assert_ray_stopped(self) -> None:
        try:
            with socket.create_connection((self.head_ip, RAY_PORT), timeout=2):
                raise RuntimeError(f"Port {self.head_ip}:{RAY_PORT} is already in use.")
        except (ConnectionRefusedError, TimeoutError, OSError):
            pass
        local = self.run_command(["pgrep", "-u", self.local_user, "-x", "raylet"], capture=True, check=False)
        local_gcs = self.run_command(["pgrep", "-u", self.local_user, "-x", "gcs_server"], capture=True, check=False)
        if local.returncode == 0 or local_gcs.returncode == 0:
            raise RuntimeError("Ray is already running locally; refusing to replace it.")
        remote = self.ssh_shell(
            'pgrep -u "$USER" -x raylet >/dev/null || pgrep -u "$USER" -x gcs_server >/dev/null',
            check=False,
        )
        if remote.returncode == 0:
            raise RuntimeError("Ray is already running on the worker; refusing to replace it.")

    def cluster_action(self, action: str) -> None:
        env = os.environ.copy()
        env.update({
            "MOLSUITE_CLUSTER_ACTION": action,
            "MOLSUITE_CLUSTER_NAME": self.config.cluster_name,
            "MOLSUITE_CLUSTER_ROOT": str(self.cluster_root),
            "MOLSUITE_HEAD_IP": self.head_ip,
            "MOLSUITE_WORKER_IP": self.worker_ip,
            "MOLSUITE_SSH_USER": self.config.ssh_user,
            "MOLSUITE_CONDA_ENV": self.config.env_name,
            "MOLSUITE_HEAD_CPUS": str(self.config.head_cpus),
            "MOLSUITE_SHARED_FS": "1" if self.config.shared_fs else "0",
            "PATH": f"{Path(self.env_python).parent}:{self.home}/.local/bin:{os.environ.get('PATH', '')}",
        })
        self.run_command([self.env_python, "-"], input_text=CLUSTER_CODE, env=env)

    def smoke_test(self) -> None:
        self.probe_root = Path(tempfile.mkdtemp(prefix=".molsuite-cluster-probe-", dir=self.projects_root))
        probe_file = self.probe_root / "staging_input.txt"
        probe_file.write_text("molsuite-ray-object-staging-ok\n", encoding="utf-8")
        env = os.environ.copy()
        env.update({
            "MOLSUITE_PROJECT_ROOT": str(self.probe_root),
            "MOLSUITE_PROBE_FILE": str(probe_file),
            "MOLSUITE_RAY_ADDRESS": f"{self.head_ip}:{RAY_PORT}",
            "MOLSUITE_WORKER_IP": self.worker_ip,
            "MOLSUITE_PROBE_CPUS": str(self.config.head_cpus + 1),
            "MOLSUITE_TEST_SHARED": "1" if self.config.shared_fs else "0",
        })
        self.run_command([self.env_python, "-"], input_text=PROBE_CODE, env=env)

    def execute(self) -> None:
        self.ensure_local_packages()
        self.ensure_key_authentication()
        self.resolve_hosts()
        self.prepare_directories()
        self.ensure_remote_packages()
        self.ensure_head_ssh()
        self.ensure_conda()
        self.ensure_environment()
        self.build_and_install_stack()
        self.install_managed_tools()
        if self.config.shared_fs:
            self.ensure_reverse_key()
            self.mount_shared_projects()
        self.assert_ray_stopped()
        self.log("Starting the local Ray head and laptop worker through MolSuite")
        self.cluster_attempted = True
        self.cluster_action("up")
        self.log("Running MolSuite shared/non-shared transport probes on the worker")
        self.smoke_test()
        self.log("All probes passed")

    def cleanup(self) -> None:
        if self.cluster_attempted and self.env_python:
            try:
                self.log("Stopping Ray head and workers through MolSuite")
                self.cluster_action("down")
            except Exception as exc:  # cleanup must continue to the SSHFS mount
                print(f"WARNING: Ray cleanup failed: {exc}", file=sys.stderr)
        if self.shared_mounted:
            try:
                self.log("Unmounting the temporary SSHFS project share")
                self.ssh_run("fusermount3", "-u", self.remote_projects_root)
            except Exception as exc:
                print(f"WARNING: SSHFS cleanup failed: {exc}", file=sys.stderr)
        if self.wheelhouse:
            shutil.rmtree(self.wheelhouse, ignore_errors=True)
        if self.probe_root and self.probe_root.name.startswith(".molsuite-cluster-probe-"):
            shutil.rmtree(self.probe_root, ignore_errors=True)


def parse_args(argv: list[str] | None = None) -> LanRayBootstrapConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="Laptop/worker IP or hostname; prompted when omitted")
    parser.add_argument("--user", default=os.environ.get("USER") or Path.home().name, help="SSH user on both nodes")
    parser.add_argument("--projects-root", type=Path, default=Path.home() / "molsuite_projects")
    parser.add_argument("--env", default="molsuite_env", dest="env_name")
    parser.add_argument("--ray-version", default="2.52.0")
    parser.add_argument("--head-cpus", type=int, default=1)
    parser.add_argument("--tools", default="", help="Comma-separated: openbabel,pkasso,p2rank")
    parser.add_argument("--ray-only", action="store_true", help="Install only MolSuite and Ray")
    parser.add_argument("--no-shared-fs", action="store_true", help="Test only Ray object staging")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip initial confirmation")
    args = parser.parse_args(argv)
    host = args.host or input("Worker IP or hostname: ").strip()
    tools = tuple(filter(None, (item.strip().lower() for item in args.tools.split(","))))
    config = LanRayBootstrapConfig(
        worker_host=host,
        ssh_user=args.user,
        projects_root=args.projects_root,
        env_name=args.env_name,
        ray_version=args.ray_version,
        head_cpus=args.head_cpus,
        shared_fs=not args.no_shared_fs,
        with_amdock=not args.ray_only,
        managed_tools=tools,
        assume_yes=args.yes,
    )
    config.validate()
    return config


def confirm(config: LanRayBootstrapConfig) -> bool:
    if config.assume_yes:
        return True
    print(
        f"Worker:      {config.ssh_user}@{config.worker_host}\n"
        f"Projects:    {config.projects_root.expanduser().resolve()}\n"
        f"Environment: {config.env_name}\n"
        f"Ray:         {config.ray_version}\n"
        f"Stack:       {'AMDockVS' if config.with_amdock else 'MolSuite/Ray only'}\n"
        f"SSHFS:       {'test' if config.shared_fs else 'disabled'}\n"
        f"Tools:       {','.join(config.managed_tools) or 'none'}"
    )
    return input("Continue? [y/N] ").strip().lower() == "y"


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    if not confirm(config):
        return 0
    bootstrap = LanRayBootstrap(config)
    previous_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: sys.exit(143))
    try:
        bootstrap.execute()
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        bootstrap.cleanup()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
