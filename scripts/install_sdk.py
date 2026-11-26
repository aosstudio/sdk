import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from fcntl import LOCK_EX, flock
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import NamedTuple
from xml.etree import ElementTree

from sdk_runtime import sandbox
from sdk_feed import (
    SHA256,
    rename_directory,
    index_digests,
    stage_provider,
)

PACKAGE_PATH = re.compile(r"[a-zA-Z0-9._-]+(?:;[a-zA-Z0-9._-]+)*")
RELEASE = Path(__file__).resolve().parents[1] / "locks/release.json"
REPAIR_CANDIDATE = ".aosstudio-repair-candidate"


@contextmanager
def lock_sdk_parent(destination: Path) -> Iterator[None]:
    lock = destination.parent / ".aosstudio-sdk.lock"
    # Keep the lock file so every invocation locks the same inode.
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o660)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"Expected a regular SDK lock file: {lock}")
        flock(fd, LOCK_EX)
        yield
    finally:
        os.close(fd)


def default_packages() -> list[str]:
    packages = json.loads(RELEASE.read_text(encoding="utf-8"))["default_packages"]
    if not isinstance(packages, list) or not all(
        isinstance(package, str) for package in packages
    ):
        raise ValueError("Invalid default SDK package list")
    return packages


def install_packages(
    runtime: Path | None,
    jdk: Path | None,
    manager: Path,
    feed: Path,
    state: Path,
    packages: list[str],
) -> None:
    command, java_home = sandbox(runtime, jdk)
    environment = {
        "JAVA_HOME": java_home,
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "TZ": "UTC",
        "ANDROID_USER_HOME": "/mnt/state/user",
        "TMPDIR": "/mnt/state/tmp",
        "JAVA_TOOL_OPTIONS": "-Duser.home=/mnt/state/user -Djava.io.tmpdir=/mnt/state/tmp",
        "SDK_TEST_BASE_URL": "file:///mnt/feed/",
    }
    subprocess.run(
        [
            *command,
            "--ro-bind",
            str(manager),
            "/mnt/manager",
            "--ro-bind",
            str(feed),
            "/mnt/feed",
            "--bind",
            str(state),
            "/mnt/state",
            "--chdir",
            "/mnt/state",
            *(
                arg
                for key, value in environment.items()
                for arg in ("--setenv", key, value)
            ),
            "--",
            "/mnt/manager/bin/sdkmanager",
            "--sdk_root=/mnt/state/sdk",
            "--install",
            *packages,
        ],
        check=True,
    )


class RepairCandidate(NamedTuple):
    destination: Path
    backup: Path
    operation: str


def read_repair_candidate(directory: Path) -> RepairCandidate | None:
    if directory.is_symlink() or not directory.is_dir():
        return None
    marker = directory / REPAIR_CANDIDATE
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        data = json.loads(marker.read_bytes())
    except ValueError:
        return None
    if not isinstance(data, list) or len(data) != 5:
        return None
    destination, backup, device, inode, operation = data
    info = directory.stat()
    if (
        (device, inode) != (info.st_dev, info.st_ino)
        or not isinstance(destination, str)
        or not isinstance(backup, str)
        or not isinstance(operation, str)
        or not SHA256.fullmatch(operation)
    ):
        return None
    return RepairCandidate(Path(destination), Path(backup), operation)


def write_repair_candidate(directory: Path, candidate: RepairCandidate) -> None:
    info = directory.stat()
    with (directory / REPAIR_CANDIDATE).open("x", encoding="utf-8") as marker:
        json.dump(
            [
                str(candidate.destination),
                str(candidate.backup),
                info.st_dev,
                info.st_ino,
                candidate.operation,
            ],
            marker,
        )


def install(
    destination: Path,
    provider: Path,
    runtime: Path | None,
    manager: Path,
    packages: list[str],
    repair_backup: Path | None = None,
    *,
    add_packages: bool = False,
    jdk: Path | None = None,
) -> Path:
    destination = destination.parent.resolve(strict=True) / destination.name
    if repair_backup is not None:
        repair_backup = sibling_path(destination, repair_backup)
    packages = list(dict.fromkeys(packages))
    if any(
        not PACKAGE_PATH.fullmatch(package)
        or any(part in {".", ".."} for part in package.split(";"))
        for package in packages
    ):
        raise ValueError("Invalid SDK package path")
    with (
        lock_sdk_parent(destination),
        TemporaryDirectory(prefix=".sdk-install-", dir=destination.parent) as work,
    ):
        interrupted_repair = False
        completed_repair = None
        live_candidate = None
        if repair_backup is not None or add_packages:
            validate_directory(destination)
            live_candidate = read_repair_candidate(destination)
            if live_candidate is not None and live_candidate.backup == destination:
                raise ValueError(
                    "Destination contains an interrupted repair. Retry the repair."
                )
        if repair_backup is not None:
            if repair_backup.exists() or repair_backup.is_symlink():
                validate_directory(repair_backup)
                backup_candidate = read_repair_candidate(repair_backup)
                if (
                    backup_candidate is not None
                    and backup_candidate.backup == repair_backup
                ):
                    if backup_candidate.destination != destination:
                        raise FileExistsError(
                            f"SDK backup belongs to a different repair: {repair_backup}"
                        )
                    interrupted_repair = True
                if not interrupted_repair:
                    if (
                        live_candidate is not None
                        and live_candidate.destination == destination
                        and live_candidate.backup == repair_backup
                    ):
                        completed_repair = live_candidate.operation
                    if completed_repair is None:
                        raise FileExistsError(
                            f"SDK backup already exists: {repair_backup}"
                        )
        elif not add_packages and (destination.exists() or destination.is_symlink()):
            raise FileExistsError(f"SDK destination already exists: {destination}")
        feed = Path(work) / "feed"
        feed.mkdir()
        stage_provider(provider, feed, packages)
        operation = hashlib.sha256(
            json.dumps([index_digests(feed), sorted(packages)]).encode("utf-8")
        ).hexdigest()
        if completed_repair is not None:
            if completed_repair != operation:
                raise FileExistsError(
                    f"SDK backup belongs to a different repair: {repair_backup}"
                )
            (destination / REPAIR_CANDIDATE).unlink()
            return destination
        state = Path(work) / "state"
        for name in ("tmp", "user"):
            (state / name).mkdir(parents=True)
        sdk = state / "sdk"
        if add_packages:
            shutil.copytree(destination, sdk, symlinks=True)
            if live_candidate is not None and live_candidate.destination == destination:
                (sdk / REPAIR_CANDIDATE).unlink()
                write_repair_candidate(sdk, live_candidate)
        else:
            sdk.mkdir()
        install_packages(runtime, jdk, manager, feed, state, packages)
        for package in packages:
            metadata = sdk.joinpath(*package.split(";"), "package.xml")
            root = ElementTree.parse(metadata).getroot()
            if root.find(f"localPackage[@path='{package}']") is None:
                raise ValueError(f"SDK Manager did not install {package}")
        if add_packages:
            rename_directory(sdk, destination, exchange=True)
        elif repair_backup is None:
            rename_directory(sdk, destination)
        else:
            if interrupted_repair:
                rename_directory(repair_backup, Path(work) / "interrupted-repair")
            write_repair_candidate(
                sdk, RepairCandidate(destination, repair_backup, operation)
            )
            rename_directory(sdk, repair_backup)
            # Before the exchange the backup path holds the verified candidate.
            # After it the same path retains the previous installation.
            try:
                rename_directory(repair_backup, destination, exchange=True)
            except OSError:
                rename_directory(repair_backup, sdk)
                raise
            if live_candidate is not None and live_candidate.destination == destination:
                (repair_backup / REPAIR_CANDIDATE).unlink()
            (destination / REPAIR_CANDIDATE).unlink()
    return destination


def sibling_path(destination: Path, backup: Path) -> Path:
    path = backup.parent.resolve(strict=True) / backup.name
    if path.parent != destination.parent or path == destination or not path.name:
        raise ValueError("Use a distinct backup directory next to the SDK")
    return path


def validate_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"Expected an existing SDK directory: {path}")


def restore(destination: Path, backup: Path) -> Path:
    destination = destination.parent.resolve(strict=True) / destination.name
    backup = sibling_path(destination, backup)
    with lock_sdk_parent(destination):
        validate_directory(destination)
        validate_directory(backup)
        live_candidate = read_repair_candidate(destination)
        backup_candidate = read_repair_candidate(backup)
        for directory, candidate in (
            (destination, live_candidate),
            (backup, backup_candidate),
        ):
            if candidate is not None and candidate.backup == directory:
                raise ValueError(
                    f"{directory} contains an interrupted repair. Retry the repair."
                )
        rename_directory(backup, destination, exchange=True)
        if live_candidate is not None and live_candidate.destination == destination:
            (backup / REPAIR_CANDIDATE).unlink()
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install or repair an SDK from a verified local feed."
    )
    parser.add_argument("--feed", type=Path)
    parser.add_argument("--destination", type=Path)
    java = parser.add_mutually_exclusive_group()
    java.add_argument("--runtime", type=Path, help="Builder JDK rootfs")
    java.add_argument("--jdk", type=Path, help="Extracted AOSStudio JDK")
    parser.add_argument("--manager", type=Path, help="SDK Manager package root")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--default-packages",
        action="store_true",
        help="Print the release's default SDK package IDs, one per line",
    )
    mode.add_argument(
        "--add",
        action="store_true",
        help="Install additional packages into an existing SDK",
    )
    mode.add_argument("--repair-backup", type=Path, help="Save the previous SDK here")
    mode.add_argument(
        "--restore-backup", type=Path, help="Exchange the SDK with this backup"
    )
    parser.add_argument("package", nargs="*")
    args = parser.parse_args()
    inputs = (args.feed, args.runtime or args.jdk, args.manager)
    try:
        if args.default_packages:
            if (
                any(value is not None for value in inputs)
                or args.destination
                or args.package
            ):
                parser.error("--default-packages takes no installation arguments")
            print("\n".join(default_packages()))
            return 0
        if args.destination is None:
            parser.error("Installation or restoration requires --destination")
        if args.restore_backup is not None:
            if any(value is not None for value in inputs) or args.package:
                parser.error(
                    "Backup restoration takes only --destination and --restore-backup"
                )
            print(restore(args.destination, args.restore_backup))
        else:
            if any(value is None for value in inputs):
                parser.error(
                    "Installation requires --feed, --manager and either --jdk or --runtime"
                )
            print(
                install(
                    args.destination,
                    args.feed,
                    args.runtime.resolve(strict=True) if args.runtime else None,
                    args.manager.resolve(strict=True),
                    args.package or default_packages(),
                    args.repair_backup,
                    add_packages=args.add,
                    jdk=args.jdk.resolve(strict=True) if args.jdk else None,
                )
            )
    except (
        OSError,
        ValueError,
        ElementTree.ParseError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"SDK installation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
