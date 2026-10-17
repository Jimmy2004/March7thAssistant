"""Install a verified, minimal snapshot; never run upstream Windows launchers."""
from __future__ import annotations

import hashlib
import json
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads(Path(__file__).with_name("engine.json").read_text())
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024


def _keep(path: PurePosixPath) -> bool:
    if path.as_posix() in {"LICENSE", "version.txt"}:
        return True
    if len(path.parts) < 2:
        return False
    if path.parts[0] == "map":
        return path.suffix == ".json"
    if path.parts[0] == "picture":
        return path.suffix.lower() in {".webp", ".png", ".jpg"}
    return (path.parts[0] == "utils" and path.suffix == ".py"
            and (len(path.parts) == 2 or path.parts[1] in {"core", "config", "flows", "vision"})
            and path.name not in {"get_angle.py", "viewer.py", "notify.py", "requests.py", "log.py"})


def install_archive(archive: Path, destination: Path) -> Path:
    """Validate first, extract regular allowlisted files to a fresh directory."""
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != MANIFEST["sha256"]:
        raise ValueError("锄大地资源校验失败，请重新下载")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fhoe-install-", dir=destination.parent) as tmp:
        stage = Path(tmp) / "engine"
        stage.mkdir()
        total = 0
        hashes = {}
        with tarfile.open(archive, "r:gz") as tar:
            for item in tar:
                parts = PurePosixPath(item.name).parts
                if not parts or parts[0] != "Fhoe-Rail-" + MANIFEST["revision"]:
                    raise ValueError("锄大地归档根目录不匹配")
                path = PurePosixPath(*parts[1:])
                if path.is_absolute() or ".." in path.parts or "\\" in item.name:
                    raise ValueError("锄大地归档路径无效")
                if not _keep(path):
                    continue
                if not item.isfile():
                    if item.isdir():
                        continue
                    raise ValueError("锄大地归档包含链接或特殊文件")
                total += item.size
                if total > MAX_EXPANDED_BYTES:
                    raise ValueError("锄大地资源大小超出限制")
                target = stage.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(item)
                if source is None:
                    raise ValueError("锄大地资源缺失")
                content = source.read()
                target.write_bytes(content)
                hashes[path.as_posix()] = hashlib.sha256(content).hexdigest()
        for name in ("utils/flows/map_operations.py", "picture/finish_fighting.webp", "LICENSE"):
            if not (stage / name).is_file():
                raise ValueError(f"锄大地资源缺失: {name}")
        for version in MANIFEST["route_versions"]:
            if not list((stage / "map" / version).glob("*.json")):
                raise ValueError(f"锄大地路线缺失: {version}")
        (stage / ".snapshot.json").write_text(json.dumps({
            "revision": MANIFEST["revision"], "files": hashes,
        }), encoding="utf-8")
        # Immutable versioned paths: interrupted installs never replace good resources.
        if destination.exists():
            raise FileExistsError(f"资源目录已存在且校验不通过，请移走后重试: {destination}")
        stage.rename(destination)
    return destination


def verify_resources(destination: Path) -> bool:
    try:
        data = json.loads((destination / ".snapshot.json").read_text())
        if data["revision"] != MANIFEST["revision"] or not data["files"]:
            return False
        for name, digest in data["files"].items():
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                return False
            candidate = destination.joinpath(*path.parts)
            if candidate.is_symlink() or hashlib.sha256(candidate.read_bytes()).hexdigest() != digest:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def prepare_resources(root: Path | None = None) -> Path:
    root = Path(root) if root is not None else PROJECT_ROOT
    destination = root / "3rdparty" / "Fhoe-Rail-cloud" / MANIFEST["revision"]
    if verify_resources(destination):
        return destination
    if destination.exists():
        raise RuntimeError(f"锄大地资源被修改或损坏: {destination}")
    import requests
    url = f"https://codeload.github.com/{MANIFEST['repository']}/tar.gz/{MANIFEST['revision']}"
    print("正在下载锄大地路线与识图引擎…", flush=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="download-", dir=destination.parent) as tmp:
        archive = Path(tmp) / "engine.tar.gz"
        size = 0
        with requests.get(url, stream=True, timeout=(15, 60)) as response:
            response.raise_for_status()
            with archive.open("wb") as output:
                for block in response.iter_content(65536):
                    size += len(block)
                    if size > MAX_ARCHIVE_BYTES:
                        raise ValueError("锄大地下载超出大小限制")
                    output.write(block)
        return install_archive(archive, destination)
