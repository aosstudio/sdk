#!/usr/bin/env python3
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from xml.etree import ElementTree

from install_sdk import RELEASE, default_packages
from sdk_feed import (
    ASSET_LIST,
    BUFFER_SIZE,
    CHUNK_MANIFEST,
    CHUNK_SIZE,
    ChunkedArchive,
    Part,
    part_name,
    rename_directory,
    sha256,
    validate_indexes,
    verify_provider,
)


def split_archive(archive: Path, output: Path, size: int, limit: int) -> list[Part]:
    parts: list[Part] = []
    with archive.open("rb") as stream:
        for number, start in enumerate(range(0, size, limit), 1):
            name = part_name(archive.name, number)
            length = remaining = min(limit, size - start)
            digest = hashlib.sha256()
            with (output / name).open("xb") as target:
                while remaining:
                    data = stream.read(min(remaining, BUFFER_SIZE))
                    if not data:
                        raise ValueError(
                            f"Archive truncated during packaging: {archive.name}"
                        )
                    target.write(data)
                    digest.update(data)
                    remaining -= len(data)
            parts.append({"file": name, "size": length, "sha256": digest.hexdigest()})
        if stream.read(1):
            raise ValueError(f"Archive grew during packaging: {archive.name}")
    return parts


def package(args: argparse.Namespace) -> Path:
    lock_digest = sha256(RELEASE)
    limit = args.chunk_size_mib * BUFFER_SIZE
    if not 1 <= limit <= CHUNK_SIZE:
        raise ValueError("Chunk size must be between 1 and 1024 MiB")
    destination: Path = args.out.parent.resolve(strict=True) / args.out.name
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Choose a new package output directory: {destination}")

    with TemporaryDirectory(
        prefix=".sdk-package-", dir=destination.parent
    ) as temporary:
        work = Path(temporary)
        verified = work / "verified"
        staged = work / "feed"
        verified.mkdir()
        staged.mkdir()
        assets = verify_provider(args.feed, verified, args.public_key)
        associations = validate_indexes(verified, assets, default_packages())
        assets.pop(CHUNK_MANIFEST, None)
        archives: dict[str, ChunkedArchive] = {}
        for name, digest in assets.items():
            source = verified / name
            size = source.stat().st_size
            if name.endswith(".zip") and size > limit:
                if name not in associations:
                    raise ValueError(
                        f"ZIP has no SDK package in the signed indexes: {name}"
                    )
                archives[name] = {
                    "size": size,
                    "sha256": digest,
                    "packages": sorted(associations[name]),
                    "parts": split_archive(source, staged, size, limit),
                }
            else:
                source.rename(staged / name)

        manifest = staged / CHUNK_MANIFEST
        manifest.write_text(
            json.dumps(
                {
                    "format": 1,
                    "component": "sdk",
                    "lock_sha256": lock_digest,
                    "source_asset_list_sha256": sha256(verified / ASSET_LIST),
                    "archives": archives,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        assets[CHUNK_MANIFEST] = sha256(manifest)
        listing = staged / ASSET_LIST
        listing.write_text(
            "".join(f"{assets[name]}  {name}\n" for name in sorted(assets)),
            encoding="ascii",
        )
        subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-inkey",
                str(args.signing_key),
                "-rawin",
                "-in",
                str(listing),
                "-out",
                str(listing.with_suffix(".sha256.sig")),
            ],
            check=True,
        )
        rename_directory(staged, destination)
    return destination / CHUNK_MANIFEST


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Package a verified local SDK feed into signed chunks."
    )
    parser.add_argument("--feed", type=Path, required=True)
    parser.add_argument(
        "--public-key", type=Path, required=True, help="Trusted input feed public key"
    )
    parser.add_argument(
        "--signing-key",
        type=Path,
        required=True,
        help="Ed25519 key for the output feed",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--chunk-size-mib", type=int, default=1024)
    try:
        print(package(parser.parse_args()))
    except (
        OSError,
        ValueError,
        ElementTree.ParseError,
        subprocess.SubprocessError,
    ) as error:
        print(f"SDK packaging failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
