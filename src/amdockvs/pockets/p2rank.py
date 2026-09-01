"""Install, validate, run, and parse the optional P2Rank command-line tool."""

from __future__ import annotations

import csv
import gzip
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from amdockvs.models import BindingSite
from typing import Any, Iterable


P2RANK_VERSION = "2.5.1"
P2RANK_ARCHIVE_NAME = f"p2rank_{P2RANK_VERSION}.tar.gz"
P2RANK_DOWNLOAD_URL = (
    f"https://github.com/rdk/p2rank/releases/download/{P2RANK_VERSION}/{P2RANK_ARCHIVE_NAME}"
)
P2RANK_ARCHIVE_SHA256 = "d243f2d9036ac053fefb9407b5fe1c85f4fe077c519fd975ac585e995feab274"
P2RANK_DEVELOPMENT_ARCHIVE = Path.home() / "Downloads" / P2RANK_ARCHIVE_NAME
JAVA_INSTALL_HINT = "Install the Java runtime from Settings > External tools (or `pip install jdk4py`)."


@dataclass(frozen=True)
class P2RankInstallation:
    version: str
    home: Path
    command: Path
    java_command: Path | None
    installed: bool
    java_version: int | None
    message: str


def _data_home() -> Path:
    configured = str(os.environ.get("AMDOCK_TOOLS_HOME") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    xdg = str(os.environ.get("XDG_DATA_HOME") or "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return (base / "AMDockVS" / "tools").resolve()


def p2rank_home(version: str = P2RANK_VERSION) -> Path:
    configured = str(os.environ.get("AMDOCK_P2RANK_HOME") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return _data_home() / "p2rank" / str(version)


def find_java_command() -> Path | None:
    """Prefer the jdk4py wheel, then AMDock's Python prefix, then JAVA_HOME, then PATH."""
    candidates: list[Path] = []
    try:
        from jdk4py import JAVA  # a jlink-trimmed JDK shipped as a plain wheel
    except ImportError:
        pass
    else:
        candidates.append(Path(JAVA))
    candidates.append(Path(sys.prefix) / "bin" / "java")
    java_home = str(os.environ.get("JAVA_HOME") or "").strip()
    if java_home:
        candidates.append(Path(java_home).expanduser() / "bin" / "java")
    on_path = shutil.which("java")
    if on_path:
        candidates.append(Path(on_path))
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate.expanduser().resolve()
    return None


def java_major_version(command: str | Path | None = None) -> int | None:
    java = Path(command).expanduser().resolve() if command else find_java_command()
    if java is None:
        return None
    try:
        result = subprocess.run(
            [str(java), "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = f"{result.stdout}\n{result.stderr}"
    marker = 'version "'
    start = text.find(marker)
    if start < 0:
        return None
    version = text[start + len(marker):].split('"', 1)[0]
    first = version.split(".", 1)[0]
    if first == "1":
        fields = version.split(".")
        first = fields[1] if len(fields) > 1 else ""
    try:
        return int(first)
    except ValueError:
        return None


def p2rank_status(version: str = P2RANK_VERSION) -> P2RankInstallation:
    home = p2rank_home(version)
    command = home / "prank"
    java = find_java_command()
    java_version = java_major_version(java)
    tool_ok = command.is_file() and (home / "bin" / "p2rank.jar").is_file()
    if not tool_ok:
        message = f"P2Rank {version} is not installed."
    elif java is None:
        message = f"P2Rank is installed, but Java was not found. {JAVA_INSTALL_HINT}"
    elif java_version is None:
        message = f"P2Rank is installed, but Java at {java} could not be validated."
    elif java_version < 17:
        message = f"P2Rank is installed, but Java {java_version} is too old (17+ required)."
    else:
        message = f"P2Rank {version} and Java {java_version} are ready."
    return P2RankInstallation(
        version=str(version),
        home=home,
        command=command,
        java_command=java,
        installed=bool(tool_ok),
        java_version=java_version,
        message=message,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download_archive(destination: Path, *, url: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        str(url),
        headers={"User-Agent": f"AMDockVS/P2Rank-{P2RANK_VERSION}"},
    )
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)


def _safe_archive_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts:
            raise ValueError(f"Unsafe path in P2Rank archive: {member.name!r}")
        if member.issym() or member.islnk() or member.isdev():
            raise ValueError(f"Unsupported link/device in P2Rank archive: {member.name!r}")
    return members


def _extract_distribution(archive_path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".p2rank-install-", dir=str(destination.parent)) as temp:
        temp_root = Path(temp)
        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = _safe_archive_members(archive)
            archive.extractall(temp_root, members=members, filter="data")
        roots = [path for path in temp_root.iterdir() if path.is_dir()]
        source = roots[0] if len(roots) == 1 else temp_root
        if not (source / "prank").is_file() or not (source / "bin" / "p2rank.jar").is_file():
            raise ValueError("The archive is not a complete P2Rank distribution.")
        staging = destination.parent / f".{destination.name}.staging-{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(source, staging)
        (staging / "prank").chmod((staging / "prank").stat().st_mode | 0o111)
        if destination.exists():
            shutil.rmtree(destination)
        staging.replace(destination)


def ensure_p2rank(
    *,
    version: str = P2RANK_VERSION,
    archive_path: str | Path | None = None,
    download_url: str | None = None,
    expected_sha256: str | None = P2RANK_ARCHIVE_SHA256,
) -> P2RankInstallation:
    """Install P2Rank on demand and return a validated installation.

    A caller-supplied archive wins. During local development, the matching file in
    ``~/Downloads`` is reused. Otherwise the official GitHub release is downloaded.
    """
    current = p2rank_status(version)
    if current.installed and (current.java_version or 0) >= 17:
        return current

    selected = Path(archive_path).expanduser().resolve() if archive_path else None
    if selected is None and version == P2RANK_VERSION and P2RANK_DEVELOPMENT_ARCHIVE.is_file():
        selected = P2RANK_DEVELOPMENT_ARCHIVE.resolve()

    temporary_archive: Path | None = None
    if selected is None:
        cache_dir = _data_home() / "downloads"
        cache_dir.mkdir(parents=True, exist_ok=True)
        selected = cache_dir / f"p2rank_{version}.tar.gz"
        if not selected.is_file():
            partial = selected.with_suffix(selected.suffix + ".part")
            if partial.exists():
                partial.unlink()
            _download_archive(
                partial,
                url=download_url
                or f"https://github.com/rdk/p2rank/releases/download/{version}/p2rank_{version}.tar.gz",
            )
            partial.replace(selected)
            temporary_archive = selected

    if not selected.is_file():
        raise FileNotFoundError(f"P2Rank archive does not exist: {selected}")
    if expected_sha256:
        actual = _sha256(selected)
        if actual.lower() != str(expected_sha256).lower():
            if temporary_archive is not None:
                with suppress(OSError):
                    temporary_archive.unlink()
            raise ValueError(
                f"P2Rank archive checksum mismatch: expected {expected_sha256}, got {actual}."
            )

    _extract_distribution(selected, p2rank_home(version))
    installed = p2rank_status(version)
    if not installed.installed:
        raise RuntimeError("P2Rank extraction finished but the installation is incomplete.")
    if (installed.java_version or 0) < 17:
        raise RuntimeError(
            f"P2Rank requires Java 17+, found {installed.java_version or 'unknown'} "
            f"at {installed.java_command or 'PATH'}. {JAVA_INSTALL_HINT}"
        )
    return installed


def _run_managed(command: list[str], *, cwd: Path, env: dict[str, str]) -> tuple[str, str]:
    process: subprocess.Popen[str] | None = None

    def kill_child() -> None:
        if process is None or process.poll() is not None:
            return
        if os.name == "posix":
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
        else:
            with suppress(Exception):
                process.terminate()
        with suppress(Exception):
            process.wait(timeout=2.0)
        if process.poll() is None:
            with suppress(Exception):
                process.kill()

    def forward_shutdown(signum, _frame) -> None:
        kill_child()
        raise SystemExit(128 + int(signum))

    install_handlers = os.name == "posix" and threading.current_thread() is threading.main_thread()
    previous: dict[int, Any] = {}
    if install_handlers:
        previous = {
            signal.SIGTERM: signal.getsignal(signal.SIGTERM),
            signal.SIGINT: signal.getsignal(signal.SIGINT),
        }
        signal.signal(signal.SIGTERM, forward_shutdown)
        signal.signal(signal.SIGINT, forward_shutdown)
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=(os.name == "posix"),
        )
        stdout, stderr = process.communicate()
    finally:
        if install_handlers:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    if process is None or process.returncode != 0:
        code = None if process is None else process.returncode
        raise RuntimeError(
            f"P2Rank failed with exit code {code}: "
            f"{str(stderr or '').strip() or str(stdout or '').strip() or 'unknown error'}"
        )
    return str(stdout or ""), str(stderr or "")


def _find_single(root: Path, pattern: str, *, required: bool = True) -> Path | None:
    matches = sorted(root.rglob(pattern))
    if not matches:
        if required:
            raise RuntimeError(f"P2Rank did not create an expected {pattern!r} output in {root}.")
        return None
    return matches[0].resolve()


def _point_coordinates_by_rank(points_path: Path) -> dict[int, list[tuple[float, float, float]]]:
    by_rank: dict[int, list[tuple[float, float, float]]] = {}
    opener = gzip.open if points_path.suffix.lower() == ".gz" else open
    with opener(points_path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.startswith(("ATOM", "HETATM")) or len(line) < 54:
                continue
            try:
                rank = int(line[22:26].strip() or 0)
                point = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
            except ValueError:
                continue
            if rank > 0:
                by_rank.setdefault(rank, []).append(point)
    return by_rank


def _box_size(points: Iterable[tuple[float, float, float]], *, padding: float = 4.0) -> tuple[float, float, float]:
    rows = list(points)
    if not rows:
        return (22.0, 22.0, 22.0)
    spans = [max(point[i] for point in rows) - min(point[i] for point in rows) for i in range(3)]
    return tuple(max(18.0, min(30.0, float(span) + 2.0 * float(padding))) for span in spans)


def parse_p2rank_outputs(
    *,
    output_dir: str | Path,
    receptor_id: int,
    run_id: str,
    profile: str,
    version: str = P2RANK_VERSION,
) -> list[dict[str, Any]]:
    output = Path(output_dir).expanduser().resolve()
    predictions_path = _find_single(output, "*_predictions.csv")
    points_path = _find_single(output, "*_points.pdb.gz")
    pml_path = _find_single(output, "*.pml", required=False)
    points = _point_coordinates_by_rank(points_path)

    rows: list[dict[str, Any]] = []
    with predictions_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            record = {str(key or "").strip(): str(value or "").strip() for key, value in raw.items()}
            rank = int(record.get("rank") or 0)
            if rank <= 0:
                continue
            center = (
                float(record["center_x"]),
                float(record["center_y"]),
                float(record["center_z"]),
            )
            size = _box_size(points.get(rank, ()))
            rows.append(
                BindingSite.build_row(
                    molecule_id=int(receptor_id),
                    name=f"P2Rank pocket {rank}",
                    source="p2rank",
                    source_ref=str(rank),
                    center=center,
                    size=size,
                    extra_data={
                        "provider": "p2rank",
                        "version": str(version),
                        "profile": str(profile),
                        "run_id": str(run_id),
                        "rank": rank,
                        "score": float(record.get("score") or 0.0),
                        "probability": float(record.get("probability") or 0.0),
                        "sas_points": int(record.get("sas_points") or 0),
                        "surface_atoms": int(record.get("surf_atoms") or 0),
                        "residue_ids": str(record.get("residue_ids") or "").split(),
                        "surface_atom_ids": [
                            int(value)
                            for value in str(record.get("surf_atom_ids") or "").split()
                            if value.isdigit()
                        ],
                        "predictions_path": str(predictions_path),
                        "points_path": str(points_path),
                        "pymol_script_path": "" if pml_path is None else str(pml_path),
                    },
                )
            )
    return rows


def run_p2rank_prediction(payload: dict[str, Any]) -> list[dict[str, Any]]:
    receptor_path = Path(str(payload.get("receptor_path") or "")).expanduser().resolve()
    if not receptor_path.is_file():
        raise FileNotFoundError(f"P2Rank receptor file does not exist: {receptor_path}")
    output_dir = Path(str(payload.get("output_dir") or "")).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    command_path = Path(str(payload.get("p2rank_command") or "")).expanduser().resolve()
    if not command_path.is_file():
        raise FileNotFoundError(f"P2Rank command does not exist: {command_path}")
    java_path = Path(str(payload.get("java_command") or "")).expanduser().resolve()
    detected_java_version = java_major_version(java_path)
    if detected_java_version is None or detected_java_version < 17:
        raise RuntimeError(f"P2Rank requires Java 17+; invalid Java command: {java_path}")

    profile = str(payload.get("profile") or "default").strip().lower()
    command = [
        str(command_path),
        "predict",
        "-f",
        str(receptor_path),
        "-o",
        str(output_dir),
        "-threads",
        str(max(1, int(payload.get("threads") or 1))),
        "-visualizations",
        "1",
        "-vis_renderers",
        "pymol",
    ]
    if profile == "alphafold":
        command.extend(["-c", "alphafold"])
    env = dict(os.environ)
    java_home = java_path.parent.parent
    env["JAVA_HOME"] = str(java_home)
    env["PATH"] = f"{java_path.parent}{os.pathsep}{env.get('PATH', '')}"
    _run_managed(command, cwd=command_path.parent, env=env)
    return parse_p2rank_outputs(
        output_dir=output_dir,
        receptor_id=int(payload.get("receptor_id") or 0),
        run_id=str(payload.get("run_id") or ""),
        profile=profile,
        version=str(payload.get("version") or P2RANK_VERSION),
    )


__all__ = [
    "JAVA_INSTALL_HINT",
    "P2RANK_ARCHIVE_SHA256",
    "P2RANK_DOWNLOAD_URL",
    "P2RANK_VERSION",
    "P2RankInstallation",
    "ensure_p2rank",
    "find_java_command",
    "java_major_version",
    "p2rank_home",
    "p2rank_status",
    "parse_p2rank_outputs",
    "run_p2rank_prediction",
]
