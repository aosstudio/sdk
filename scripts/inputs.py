#!/usr/bin/env python3
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import tarfile
from tempfile import TemporaryDirectory
from typing import TypedDict
from zipfile import ZipFile

from sdk_feed import rename_directory, sha256

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "locks/release.json"
VERIFICATION = ROOT / "locks/verification.json"


class Download(TypedDict):
    url: str
    sha256: str


class Component(Download, total=False):
    name: str
    package: str
    revision: list[int]


class Platform(TypedDict):
    api_level: int
    extension_level: int
    layoutlib_api: int


class Release(TypedDict):
    series: str
    default_packages: list[str]
    platform: Platform
    components: dict[str, Component]


def release() -> Release:
    result: Release = json.loads(RELEASE.read_text(encoding="utf-8"))
    return result


def fetch(destination: Path, entry: Download) -> None:
    digest = entry["sha256"]
    if destination.is_file() and sha256(destination) == digest:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".fetch-", dir=destination.parent) as temporary:
        path = Path(temporary) / destination.name
        subprocess.run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--location",
                "--proto",
                "=https",
                "--proto-redir",
                "=https",
                "--retry",
                "3",
                "--connect-timeout",
                "30",
                "--header",
                "Accept: application/octet-stream",
                "--output",
                str(path),
                entry["url"],
            ],
            check=True,
        )
        if sha256(path) != digest:
            raise ValueError(f"SHA-256 mismatch for {destination.name}")
        path.replace(destination)


def fetch_all(downloads: list[tuple[Path, Download]]) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [pool.submit(fetch, path, entry) for path, entry in downloads]
        for job in jobs:
            job.result()


def unpack(input_file: Path, destination: Path, inner: str = ".") -> None:
    digest = sha256(input_file)
    receipt = destination / ".archive-sha256"
    if receipt.is_file() and receipt.read_text().strip() == digest:
        return
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Remove stale extracted input at {destination}")
    with TemporaryDirectory(prefix=".unpack-", dir=destination.parent) as temporary:
        work = Path(temporary)
        if input_file.name.endswith(".tar.gz"):
            with tarfile.open(input_file) as archive:
                archive.extractall(work, filter="data")
        else:
            with ZipFile(input_file) as archive:
                archive.extractall(work)
                for member in archive.infolist():
                    mode = (member.external_attr >> 16) & 0o777
                    if mode and not member.is_dir():
                        (work / member.filename).chmod(mode)
        tree = work / inner
        (tree / receipt.name).write_text(digest + "\n")
        rename_directory(tree, destination)


def prepare(directory: Path) -> None:
    components = release()["components"]
    files = {role: directory / entry["name"] for role, entry in components.items()}
    fetch_all([(files[role], entry) for role, entry in components.items()])
    records: dict[str, Download] = json.loads(VERIFICATION.read_text())
    maven = directory / "maven"
    maven.mkdir(exist_ok=True)
    fetch_all(
        [(maven / "dependencies" / name, record) for name, record in records.items()]
    )
    unpack(files["jdk"], directory / "jdk")
    unpack(files["agp"], maven / "agp", "aosstudio-agp-9.5.2-maven/maven")
    unpack(files["gradle"], directory / "gradle", "gradle-9.6.0")
    unpack(files["cmdline-tools"], directory / "manager", "cmdline-tools")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch the locked SDK and verification inputs."
    )
    parser.add_argument("--out", required=True, type=Path)
    prepare(parser.parse_args().out)


if __name__ == "__main__":
    main()
