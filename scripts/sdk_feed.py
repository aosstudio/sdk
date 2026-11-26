from ctypes import CDLL, c_char_p, c_int, c_uint, get_errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from typing import BinaryIO, TypedDict
from xml.etree import ElementTree


ASSET_LIST = "release-assets.sha256"
CHUNK_MANIFEST = "release-chunks.json"
ASSET_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*\.(?:xml|zip)")
ASSET_ENTRY = re.compile(r"([0-9a-f]{64})  (\S+)")
SHA256 = re.compile(r"[0-9a-f]{64}")
REPOSITORY_INDEXES = ("repository2-3.xml", "repository2-4.xml")
ADDONS_LIST = "addons_list-7.xml"
SYSTEM_IMAGE_INDEX = "sys-img2-5.xml"
CHUNK_SIZE = 1024**3
BUFFER_SIZE = 1024**2


class Part(TypedDict):
    file: str
    size: int
    sha256: str


class ChunkedArchive(TypedDict):
    size: int
    sha256: str
    packages: list[str]
    parts: list[Part]


def open_asset(source: Path) -> BinaryIO:
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    stream = os.fdopen(fd, "rb")
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        stream.close()
        raise ValueError(f"Expected a regular file: {source}")
    return stream


def copy_asset(source: Path, destination: Path) -> None:
    with open_asset(source) as stream, destination.open("xb") as output:
        shutil.copyfileobj(stream, output, length=BUFFER_SIZE)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def part_name(archive: str, number: int) -> str:
    return f"{archive}.part-{number:06d}"


def rename_directory(
    source: Path, destination: Path, *, exchange: bool = False
) -> None:
    rename = CDLL(None, use_errno=True).renameat2
    rename.argtypes = (c_int, c_char_p, c_int, c_char_p, c_uint)
    rename.restype = c_int
    at_fdcwd = -100
    flags = 2 if exchange else 1  # RENAME_EXCHANGE or RENAME_NOREPLACE
    if rename(at_fdcwd, os.fsencode(source), at_fdcwd, os.fsencode(destination), flags):
        error = get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def read_chunk_manifest(
    path: Path, assets: dict[str, str]
) -> dict[str, ChunkedArchive]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or type(data.get("format")) is not int
        or data["format"] != 1
    ):
        raise ValueError("Unsupported SDK chunk manifest")
    archives = data.get("archives")
    if not isinstance(archives, dict):
        raise ValueError("Expected archive records in the SDK chunk manifest")
    result: dict[str, ChunkedArchive] = {}
    for name, archive in archives.items():
        if (
            not name.endswith(".zip")
            or name not in assets
            or not isinstance(archive, dict)
        ):
            raise ValueError(f"Unknown ZIP in chunk manifest: {name}")
        size, parts, packages = (
            archive.get(key) for key in ("size", "parts", "packages")
        )
        if (
            archive.get("sha256") != assets[name]
            or type(size) is not int
            or size <= 0
            or not isinstance(parts, list)
            or not parts
            or not isinstance(packages, list)
            or not packages
            or not all(isinstance(package, str) for package in packages)
        ):
            raise ValueError(f"Invalid chunk manifest archive record: {name}")
        verified: list[Part] = []
        for number, part in enumerate(parts, 1):
            expected_file = part_name(name, number)
            if (
                not isinstance(part, dict)
                or part.get("file") != expected_file
                or type(part.get("size")) is not int
                or not 0 < part["size"] <= CHUNK_SIZE
                or not isinstance(part.get("sha256"), str)
                or not SHA256.fullmatch(part["sha256"])
            ):
                raise ValueError(f"Invalid chunk record: {expected_file}")
            verified.append(
                {"file": expected_file, "size": part["size"], "sha256": part["sha256"]}
            )
        if sum(part["size"] for part in verified) != size:
            raise ValueError(
                f"Chunk sizes do not add up to the signed archive size: {name}"
            )
        result[name] = {
            "size": size,
            "sha256": assets[name],
            "packages": packages,
            "parts": verified,
        }
    return result


def restore_archive(feed: Path, destination: Path, archive: ChunkedArchive) -> None:
    whole = hashlib.sha256()
    with destination.open("xb") as output:
        for part in archive["parts"]:
            digest = hashlib.sha256()
            remaining = part["size"]
            with open_asset(feed / part["file"]) as stream:
                while remaining:
                    data = stream.read(min(BUFFER_SIZE, remaining))
                    if not data:
                        raise ValueError(f"Chunk size mismatch: {part['file']}")
                    remaining -= len(data)
                    digest.update(data)
                    whole.update(data)
                    output.write(data)
                if stream.read(1):
                    raise ValueError(f"Chunk size mismatch: {part['file']}")
            if digest.hexdigest() != part["sha256"]:
                raise ValueError(f"Chunk SHA-256 mismatch: {part['file']}")
    if whole.hexdigest() != archive["sha256"]:
        raise ValueError(f"Reconstructed archive SHA-256 mismatch: {destination.name}")


def verify_provider(feed: Path, staged: Path, public_key: Path) -> dict[str, str]:
    signature = f"{ASSET_LIST}.sig"
    for name in (ASSET_LIST, signature):
        copy_asset(feed / name, staged / name)
    subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-verify",
            "-pubin",
            "-inkey",
            str(public_key),
            "-rawin",
            "-in",
            str(staged / ASSET_LIST),
            "-sigfile",
            str(staged / signature),
        ],
        check=True,
    )
    assets: dict[str, str] = {}
    for line in (staged / ASSET_LIST).read_text(encoding="ascii").splitlines():
        entry = ASSET_ENTRY.fullmatch(line)
        if entry is None:
            raise ValueError(f"Invalid signed asset record: {line!r}")
        digest, name = entry.groups()
        if (
            name != CHUNK_MANIFEST and not ASSET_NAME.fullmatch(name)
        ) or name in assets:
            raise ValueError(f"Invalid or duplicate signed asset name: {name!r}")
        assets[name] = digest
    if not assets.keys() & REPOSITORY_INDEXES:
        raise ValueError("The feed needs a signed SDK repository index")

    def copy_verified(name: str) -> None:
        target = staged / name
        copy_asset(feed / name, target)
        if sha256(target) != assets[name]:
            raise ValueError(f"Signed asset SHA-256 mismatch: {name}")

    chunks: dict[str, ChunkedArchive] = {}
    if CHUNK_MANIFEST in assets:
        copy_verified(CHUNK_MANIFEST)
        chunks = read_chunk_manifest(staged / CHUNK_MANIFEST, assets)
    for name in assets:
        if name in chunks:
            restore_archive(feed, staged / name, chunks[name])
        elif name != CHUNK_MANIFEST:
            copy_verified(name)
    if chunks:
        associations = validate_indexes(staged, assets, ())
        for name, archive in chunks.items():
            if sorted(associations.get(name, set())) != archive["packages"]:
                raise ValueError(
                    f"Signed package paths do not match SDK indexes: {name}"
                )
    return assets


def validate_indexes(
    feed: Path, assets: dict[str, str], packages: tuple[str, ...] | list[str]
) -> dict[str, set[str]]:
    associations: dict[str, set[str]] = {}

    def read_index(index: str) -> set[str]:
        root = ElementTree.parse(feed / index).getroot()
        for url in root.iter("url"):
            name = url.text or ""
            if not name.endswith(".zip") or name not in assets:
                raise ValueError(
                    f"Archive URL must name a signed ZIP in {index}: {name!r}"
                )
        available: set[str] = set()
        for package in root.findall("remotePackage"):
            path = package.attrib.get("path")
            if not path:
                continue
            for url in package.iter("url"):
                associations.setdefault(url.text or "", set()).add(path)
            if any(
                archive.find("complete/url") is not None
                and archive.findtext("host-os", "linux") == "linux"
                and archive.findtext("host-arch", "x64") == "x64"
                and archive.findtext("host-bits", "64") == "64"
                and archive.findtext("jvm-bits", "64") == "64"
                for archive in package.findall("archives/archive")
            ):
                available.add(path)
        return available

    images: set[str] = set()
    if ADDONS_LIST in assets:
        addons = ElementTree.parse(feed / ADDONS_LIST).getroot()
        if len(addons):
            urls = [url.text for url in addons.iter("url")]
            if urls != [SYSTEM_IMAGE_INDEX] or SYSTEM_IMAGE_INDEX not in assets:
                raise ValueError("Expected a signed local system-image index")
            images = read_index(SYSTEM_IMAGE_INDEX)
    for index in REPOSITORY_INDEXES:
        if index not in assets:
            continue
        available = read_index(index) | images
        if missing := set(packages).difference(available):
            raise ValueError(
                f"Missing packages in {index}: {', '.join(sorted(missing))}"
            )
    return associations
