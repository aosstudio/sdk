from ctypes import CDLL, c_char_p, c_int, c_uint, get_errno
import hashlib
from io import FileIO
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import TypedDict
from xml.etree import ElementTree


CHUNK_MANIFEST = "release-chunks.json"
ZIP_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*\.zip")
SHA256 = re.compile(r"[0-9a-f]{64}")
REPOSITORY_INDEXES = ("repository2-3.xml", "repository2-4.xml")
ADDONS_LIST = "addons_list-7.xml"
SYSTEM_IMAGE_INDEX = "sys-img2-5.xml"
CHUNK_SIZE = 1024**3
BUFFER_SIZE = 1024**2


class Archive(TypedDict):
    size: int
    algorithm: str
    checksum: str


class Part(TypedDict):
    file: str
    size: int
    sha256: str


class ChunkedArchive(TypedDict):
    size: int
    sha256: str
    parts: list[Part]


def open_asset(source: Path) -> FileIO:
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    stream = FileIO(fd, "rb")
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
    feed: Path, records: dict[str, Archive]
) -> dict[str, ChunkedArchive]:
    with open_asset(feed / CHUNK_MANIFEST) as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or data.get("format") != 1:
        raise ValueError("Unsupported SDK chunk manifest")
    archives = data.get("archives")
    if not isinstance(archives, dict):
        raise ValueError("Expected archive records in the SDK chunk manifest")
    result: dict[str, ChunkedArchive] = {}
    for name, archive in archives.items():
        if name not in records or not isinstance(archive, dict):
            raise ValueError(f"Unknown ZIP in chunk manifest: {name}")
        size, checksum, parts = (
            archive.get(key) for key in ("size", "sha256", "parts")
        )
        if (
            type(size) is not int
            or size != records[name]["size"]
            or not isinstance(checksum, str)
            or not SHA256.fullmatch(checksum)
            or not isinstance(parts, list)
            or not parts
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
            raise ValueError(f"Chunk sizes do not add up to the archive size: {name}")
        result[name] = {"size": size, "sha256": checksum, "parts": verified}
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


def archive_record(complete: ElementTree.Element, index: str) -> tuple[str, Archive]:
    asset = complete.findtext("url", "")
    if not ZIP_NAME.fullmatch(asset):
        raise ValueError(f"Expected a local ZIP name in {index}: {asset!r}")
    checksum = complete.find("checksum")
    algorithm = checksum.attrib.get("type", "sha1") if checksum is not None else ""
    value = checksum.text if checksum is not None else None
    if algorithm not in {"sha1", "sha256"} or not value:
        raise ValueError(f"Expected an archive checksum for {asset}")
    length = hashlib.new(algorithm).digest_size * 2
    if not re.fullmatch(f"[0-9a-f]{{{length}}}", value):
        raise ValueError(f"Invalid archive checksum for {asset}")
    size = int(complete.findtext("size", "0"))
    if size <= 0:
        raise ValueError(f"Invalid archive size for {asset}")
    return asset, {"size": size, "algorithm": algorithm, "checksum": value}


def index_names(feed: Path) -> list[str]:
    return [
        name
        for name in (*REPOSITORY_INDEXES, ADDONS_LIST, SYSTEM_IMAGE_INDEX)
        if (feed / name).exists() or (feed / name).is_symlink()
    ]


def index_digests(feed: Path) -> dict[str, str]:
    return {name: sha256(feed / name) for name in index_names(feed)}


def read_indexes(feed: Path, packages: list[str]) -> dict[str, Archive]:
    records: dict[str, Archive] = {}

    def read_index(index: str) -> set[str]:
        root = ElementTree.parse(feed / index).getroot()
        available: set[str] = set()
        asset_names: set[str] = set()
        for package in root.findall("remotePackage"):
            path = package.attrib.get("path")
            if not path:
                continue
            for archive in package.findall("archives/archive"):
                complete = archive.find("complete")
                if complete is None:
                    continue
                name, record = archive_record(complete, index)
                asset_names.add(name)
                if records.setdefault(name, record) != record:
                    raise ValueError(f"Conflicting archive checksums for {name}")
                if (
                    archive.findtext("host-os", "linux") == "linux"
                    and archive.findtext("host-arch", "x64") == "x64"
                    and archive.findtext("jvm-bits", "64") == "64"
                ):
                    available.add(path)
        if any(url.text not in asset_names for url in root.iter("url")):
            raise ValueError(f"Unexpected archive URL in {index}")
        return available

    files = index_names(feed)
    indexes = [name for name in REPOSITORY_INDEXES if name in files]
    if not indexes:
        raise ValueError("The feed needs an SDK repository index")
    images: set[str] = set()
    if ADDONS_LIST in files:
        addons = ElementTree.parse(feed / ADDONS_LIST).getroot()
        if len(addons):
            urls = [url.text for url in addons.iter("url")]
            if urls != [SYSTEM_IMAGE_INDEX] or SYSTEM_IMAGE_INDEX not in files:
                raise ValueError("Expected a local system-image index")
            images = read_index(SYSTEM_IMAGE_INDEX)
    for index in indexes:
        available = read_index(index) | images
        if missing := set(packages).difference(available):
            raise ValueError(
                f"Missing packages in {index}: {', '.join(sorted(missing))}"
            )
    return records


def verify_archives(feed: Path, records: dict[str, Archive]) -> None:
    for name, record in records.items():
        with open_asset(feed / name) as stream:
            size = os.fstat(stream.fileno()).st_size
            if size != record["size"]:
                raise ValueError(f"Archive size differs from the SDK index: {name}")
            digest = hashlib.file_digest(stream, record["algorithm"]).hexdigest()
        if digest != record["checksum"]:
            raise ValueError(f"Archive checksum differs from the SDK index: {name}")


def stage_provider(source: Path, staged: Path, packages: list[str]) -> None:
    for name in index_names(source):
        copy_asset(source / name, staged / name)
    records = read_indexes(staged, packages)
    manifest = source / CHUNK_MANIFEST
    chunks = (
        read_chunk_manifest(source, records)
        if manifest.exists() or manifest.is_symlink()
        else {}
    )
    for name in records:
        if name in chunks:
            restore_archive(source, staged / name, chunks[name])
        else:
            copy_asset(source / name, staged / name)
    verify_archives(staged, records)
