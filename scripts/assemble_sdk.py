#!/usr/bin/env python3
import argparse
import gzip
import hashlib
from pathlib import Path
import shutil
import subprocess
import tarfile
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as ET

from inputs import Release, release
from sdk_feed import ASSET_LIST, sha256, validate_indexes

XSI = "http://www.w3.org/2001/XMLSchema-instance"
GENERIC = "http://schemas.android.com/repository/android/generic/02"


def write_indexes(feed: Path, lock: Release) -> None:
    ET.register_namespace("xsi", XSI)
    for schema in (3, 4):
        namespace = (
            f"http://schemas.android.com/sdk/android/repo/repository2/{schema:02}"
        )
        ET.register_namespace("s", namespace)
        root = ET.Element(f"{{{namespace}}}sdk-repository", {"xmlns:g": GENERIC})
        for entry in lock["components"].values():
            if "package" not in entry:
                continue
            package = entry["package"]
            remote = ET.SubElement(root, "remotePackage", {"path": package})
            type_name = (
                "s:platformDetailsType"
                if package.startswith("platforms;")
                else "g:genericDetailsType"
            )
            details = ET.SubElement(
                remote, "type-details", {f"{{{XSI}}}type": type_name}
            )
            if package.startswith("platforms;"):
                version = lock["platform"]
                ET.SubElement(details, "api-level").text = str(version["api_level"])
                ET.SubElement(details, "extension-level").text = str(
                    version["extension_level"]
                )
                ET.SubElement(details, "base-extension").text = "true"
                ET.SubElement(
                    details, "layoutlib", {"api": str(version["layoutlib_api"])}
                )
            revision = ET.SubElement(remote, "revision")
            for key, number in zip(("major", "minor", "micro"), entry["revision"]):
                ET.SubElement(revision, key).text = str(number)
            ET.SubElement(remote, "display-name").text = package
            archive = ET.SubElement(ET.SubElement(remote, "archives"), "archive")
            complete = ET.SubElement(archive, "complete")
            asset = feed / entry["name"]
            ET.SubElement(complete, "size").text = str(asset.stat().st_size)
            with asset.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha1").hexdigest()
            ET.SubElement(complete, "checksum", {"type": "sha1"}).text = digest
            ET.SubElement(complete, "url").text = asset.name
            if not package.startswith("platforms;"):
                ET.SubElement(archive, "host-os").text = "linux"
                ET.SubElement(archive, "host-arch").text = "x64"
        ET.indent(root)
        ET.ElementTree(root).write(
            feed / f"repository2-{schema}.xml", encoding="utf-8", xml_declaration=True
        )


def pack_file(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = info.gid = info.mtime = 0
    info.uname = info.gname = ""
    info.mode = 0o755 if info.isdir() else 0o644
    return info


def assemble(
    inputs: Path, output: Path, key: Path | None, trusted_key: Path | None
) -> Path:
    lock = release()
    output.mkdir(parents=True, exist_ok=True)
    name = f"aosstudio-sdk-{lock['series']}-linux-x86_64.tar.gz"
    with TemporaryDirectory(prefix=".assemble-", dir=output) as temporary:
        stage = Path(temporary)
        bundle = stage / "bundle"
        feed = bundle / "feed"
        feed.mkdir(parents=True)
        if key is None:
            key = stage / "test-private.pem"
            subprocess.run(
                ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)],
                check=True,
            )
        public_key = bundle / "feed-public.pem"
        subprocess.run(
            ["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(public_key)],
            check=True,
        )
        if (
            trusted_key is not None
            and trusted_key.read_bytes() != public_key.read_bytes()
        ):
            raise ValueError("Signing key does not match the trusted SDK public key")

        for entry in lock["components"].values():
            if "package" in entry:
                asset = feed / entry["name"]
                shutil.copyfile(inputs / entry["name"], asset)
                if sha256(asset) != entry["sha256"]:
                    raise ValueError(f"SHA-256 mismatch for {asset.name}")
        write_indexes(feed, lock)
        assets = {asset.name: sha256(asset) for asset in sorted(feed.iterdir())}
        validate_indexes(feed, assets, lock["default_packages"])
        listing = feed / ASSET_LIST
        listing.write_text(
            "".join(f"{digest}  {file}\n" for file, digest in assets.items()),
            encoding="ascii",
        )
        subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-sign",
                "-inkey",
                str(key),
                "-rawin",
                "-in",
                str(listing),
                "-out",
                str(feed / f"{ASSET_LIST}.sig"),
            ],
            check=True,
        )

        archive_path = stage / name
        with (
            archive_path.open("wb") as stream,
            gzip.GzipFile(
                filename="", mode="wb", fileobj=stream, mtime=0, compresslevel=1
            ) as compressed,
            tarfile.open(fileobj=compressed, mode="w|") as archive,
        ):
            archive.add(feed, arcname="feed", filter=pack_file)
            archive.add(public_key, arcname=public_key.name, filter=pack_file)
        return archive_path.replace(output / name)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assemble and sign the SDK feed bundle."
    )
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--signing-key", type=Path)
    parser.add_argument("--trusted-key", type=Path)
    args = parser.parse_args()
    print(assemble(args.inputs, args.out, args.signing_key, args.trusted_key))


if __name__ == "__main__":
    main()
