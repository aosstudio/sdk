#!/usr/bin/env python3
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile

from inputs import RELEASE, release
from install_sdk import default_packages
from sdk_runtime import sandbox
from sdk_feed import ASSET_LIST, sha256

ROOT = Path(__file__).resolve().parents[1]
CERT_DIGEST = re.compile(
    r"^Signer #\d+ certificate SHA-256 digest: ([0-9a-f]{64})$", re.M
)


def run(command: list[str], log: Path) -> None:
    with log.open("xb") as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        raise ValueError(f"Command failed with status {result.returncode}, see {log}")


def sample_apk(module: str) -> Path:
    return Path(f"{module}/build/outputs/apk/debug/{module}-debug.apk")


def build_samples(
    args: argparse.Namespace,
    sdk: Path,
    output: Path,
    gradle_cache: Path,
    repeats: int,
    debug_key: Path | None = None,
) -> dict[str, dict[str, str]]:
    (build_tools_package,) = (
        package for package in default_packages() if package.startswith("build-tools;")
    )
    build_tools = "/mnt/sdk/" + build_tools_package.replace(";", "/")
    target_sdk = release()["platform"]["api_level"]
    state = output / "state"
    (state / "home").mkdir(parents=True)
    (state / "tmp").mkdir()
    if debug_key is not None:
        key_dir = state / "home/.android"
        key_dir.mkdir()
        shutil.copy2(debug_key, key_dir / "debug.keystore")
    command, java_home = sandbox(args.runtime, args.jdk)
    environment = {
        "JAVA_HOME": java_home,
        "PATH": f"{java_home}/bin:/usr/bin:/bin",
        "HOME": "/mnt/state/home",
        "ANDROID_USER_HOME": "/mnt/state/home/.android",
        "ANDROID_HOME": "/mnt/sdk",
        "GRADLE_USER_HOME": "/mnt/cache",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }
    isolated = [
        *command,
        "--ro-bind",
        str(args.inputs),
        "/mnt/inputs",
        "--ro-bind",
        str(args.gradle),
        "/mnt/gradle",
        "--bind",
        str(gradle_cache),
        "/mnt/cache",
        "--ro-bind",
        str(sdk),
        "/mnt/sdk",
        "--bind",
        str(state),
        "/mnt/state",
        "--bind",
        str(state / "tmp"),
        "/tmp",
    ]
    if args.native_libs:
        isolated.extend(["--ro-bind", str(args.native_libs), "/mnt/native-libs"])
        environment["LD_LIBRARY_PATH"] = "/mnt/native-libs"
    for key, value in environment.items():
        isolated.extend(["--setenv", key, value])
    if args.reference_sdk is not None:
        run(
            [
                *isolated,
                "--chdir",
                "/mnt/state",
                "--",
                "/mnt/gradle/bin/gradle",
                "--version",
            ],
            output / "gradle-version.log",
        )

    results: dict[str, dict[str, str]] = {}
    for mode, source, modules in (
        ("built-in", args.project, ("java-app", "kotlin-app")),
        ("external", args.external_kotlin_project, ("kotlin-app",)),
    ):
        digests: dict[str, str] = {}
        for number in range(1, repeats + 1):
            job = f"{mode}-{number}"
            project = output / job
            shutil.copytree(
                source,
                project,
                ignore=shutil.ignore_patterns(
                    ".gradle", ".kotlin", "build", "local.properties"
                ),
            )
            command = [
                *isolated,
                "--bind",
                str(project),
                "/mnt/project",
                "--chdir",
                "/mnt/project",
                "--",
            ]
            run(
                [
                    *command,
                    "/mnt/gradle/bin/gradle",
                    "--offline",
                    "--no-daemon",
                    "--no-build-cache",
                    "--dependency-verification",
                    "strict",
                    "-Dorg.gradle.vfs.watch=false",
                    "-Duser.home=/mnt/state/home",
                    "-Djava.io.tmpdir=/mnt/state/tmp",
                    f"-Pandroid.aapt2FromMavenOverride={build_tools}/aapt2",
                    "-Pandroid.builder.sdkDownload=false",
                    *(f":{module}:assembleDebug" for module in modules),
                ],
                output / f"{job}.log",
            )
            for module in modules:
                relative = sample_apk(module)
                apk = project / relative
                verify_log = output / f"{job}-{module}-signature.log"
                run(
                    [
                        *command,
                        f"{build_tools}/apksigner",
                        "verify",
                        "--verbose",
                        "--print-certs",
                        f"/mnt/project/{relative}",
                    ],
                    verify_log,
                )
                if (
                    "Verified using v2 scheme (APK Signature Scheme v2): true"
                    not in verify_log.read_text()
                ):
                    raise ValueError(f"APK Signature Scheme v2 required: {apk}")
                with ZipFile(apk) as archive:
                    entries = archive.namelist()
                    if "classes.dex" not in entries or any(
                        name.startswith("lib/") and name.endswith(".so")
                        for name in entries
                    ):
                        raise ValueError(f"Expected a non-native Android app: {apk}")
                badging = output / f"{job}-{module}-badging.log"
                run(
                    [
                        *command,
                        f"{build_tools}/aapt",
                        "dump",
                        "badging",
                        f"/mnt/project/{relative}",
                    ],
                    badging,
                )
                expected_package = "demo." + module.replace("-", "")
                identity = apk_identity(badging)
                if identity != {
                    "package": expected_package,
                    "sdkVersion": "24",
                    "targetSdkVersion": str(target_sdk),
                }:
                    raise ValueError(
                        f"Unexpected Android manifest for {apk}: {identity}"
                    )
                digest = sha256(apk)
                if module in digests and digests[module] != digest:
                    raise ValueError(
                        f"Independent offline APKs differ: {mode}/{module}"
                    )
                digests[module] = digest
        results[mode] = digests
    return results


def apk_identity(log: Path) -> dict[str, str]:
    return dict(
        re.findall(
            r"^(package|sdkVersion|targetSdkVersion):[ ]*(?:name=)?'([^']+)'",
            log.read_text(),
            re.M,
        )
    )


def apk_entries(apk: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    with ZipFile(apk) as archive:
        for entry in archive.infolist():
            if entry.filename in entries:
                raise ValueError(f"Duplicate ZIP entry in {apk}: {entry.filename}")
            entries[entry.filename] = hashlib.sha256(archive.read(entry)).hexdigest()
    return entries


def compare_reference(
    args: argparse.Namespace,
    candidate_sdk: Path,
    output: Path,
    candidate: dict[str, dict[str, str]],
) -> None:
    reference_sdk: Path = args.reference_sdk
    package_paths = [
        package
        for package in default_packages()
        if package.startswith(("platforms;", "build-tools;"))
    ]
    metadata = {
        label: {
            package: (
                sdk.joinpath(*package.split(";"), "source.properties")
                .read_text()
                .strip()
            )
            for package in package_paths
        }
        for label, sdk in (("aosstudio", candidate_sdk), ("google", reference_sdk))
    }
    reference_output = output / "google-reference"
    reference_output.mkdir()
    cache = reference_output / "gradle-cache"
    cache.mkdir()
    reference = build_samples(
        args,
        reference_sdk,
        reference_output,
        cache,
        repeats=1,
        debug_key=output / "state/home/.android/debug.keystore",
    )
    comparisons = []
    for mode, modules in candidate.items():
        for module, candidate_digest in modules.items():
            reference_digest = reference[mode][module]
            job = f"{mode}-1"
            relative = Path(job) / sample_apk(module)
            left = apk_entries(output / relative)
            right = apk_entries(reference_output / relative)
            changed = sorted(
                name for name in left.keys() & right.keys() if left[name] != right[name]
            )
            added = sorted(left.keys() - right.keys())
            missing = sorted(right.keys() - left.keys())
            signature_log = f"{job}-{module}-signature.log"
            left_certs = CERT_DIGEST.findall((output / signature_log).read_text())
            right_certs = CERT_DIGEST.findall(
                (reference_output / signature_log).read_text()
            )
            same_certificate = bool(left_certs) and left_certs == right_certs
            badging_log = f"{job}-{module}-badging.log"
            same_identity = apk_identity(output / badging_log) == apk_identity(
                reference_output / badging_log
            )
            comparisons.append(
                {
                    "sample": f"{mode}/{module}",
                    "aosstudio_sha256": candidate_digest,
                    "google_sha256": reference_digest,
                    "byte_identical": candidate_digest == reference_digest,
                    "aosstudio_only_entries": added,
                    "google_only_entries": missing,
                    "changed_entries": changed,
                    "same_signing_certificate": same_certificate,
                    "same_manifest_identity": same_identity,
                }
            )
    toolchain = {
        label: {
            "gradle_and_jdk": (root / "gradle-version.log").read_text().strip(),
            "plugins": sorted(
                {
                    line
                    for mode in candidate
                    for line in (root / f"{mode}-1.log").read_text().splitlines()
                    if line.startswith(
                        ("AGP loaded from ", "External KGP loaded from ")
                    )
                }
            ),
        }
        for label, root in (("aosstudio", output), ("google", reference_output))
    }
    report = output / "reference-comparison.json"
    report.write_text(
        json.dumps(
            {"sdk_packages": metadata, "toolchain": toolchain, "apks": comparisons},
            indent=2,
        )
        + "\n"
    )
    if any(
        not (entry["same_manifest_identity"] and entry["same_signing_certificate"])
        for entry in comparisons
    ):
        raise ValueError(
            f"APK identity differs from the Google SDK build. See {report}"
        )
    print(report)


def test_sdk(args: argparse.Namespace) -> Path:
    lock_digest = sha256(RELEASE)
    output: Path = args.out.parent.resolve(strict=True) / args.out.name
    protected = [args.project, args.external_kotlin_project]
    if args.reference_sdk is not None:
        protected.append(args.reference_sdk)
    if any(output.is_relative_to(path) for path in protected):
        raise ValueError(
            "Place the output outside the input projects and reference SDK"
        )
    for path in (output.parent, args.gradle_cache):
        fs = subprocess.check_output(
            ["findmnt", "-n", "-o", "FSTYPE", "--target", str(path)], text=True
        ).strip()
        if fs in {"tmpfs", "ramfs"}:
            raise ValueError(f"Use a disk-backed build location: {path}")

    output.mkdir()
    sdk = output / "sdk"
    asset_list = args.feed / ASSET_LIST
    feed_digest = sha256(asset_list)
    run(
        [
            sys.executable,
            str(ROOT / "scripts/install_sdk.py"),
            "--feed",
            str(args.feed),
            "--public-key",
            str(args.public_key),
            "--runtime" if args.runtime else "--jdk",
            str(args.runtime or args.jdk),
            "--manager",
            str(args.manager),
            "--destination",
            str(sdk),
        ],
        output / "install.log",
    )
    if sha256(asset_list) != feed_digest:
        raise ValueError("Signed asset list changed during installation")
    results = build_samples(args, sdk, output, args.gradle_cache, repeats=2)

    manifest = output / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "format": 1,
                "component": "sdk",
                "state": "verified-offline-app-builds",
                "lock_sha256": lock_digest,
                "signed_asset_list_sha256": feed_digest,
                "apk_sha256": results,
            },
            indent=2,
        )
        + "\n"
    )
    if args.reference_sdk is not None:
        compare_reference(args, sdk, output, results)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test the locked headless Android SDK."
    )
    for name in (
        "manager",
        "gradle",
        "gradle-cache",
        "inputs",
        "project",
        "external-kotlin-project",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--trusted-key", type=Path)
    parser.add_argument("--feed", type=Path)
    parser.add_argument("--public-key", type=Path)
    java = parser.add_mutually_exclusive_group(required=True)
    java.add_argument("--runtime", type=Path)
    java.add_argument("--jdk", type=Path)
    parser.add_argument("--native-libs", type=Path)
    parser.add_argument(
        "--reference-sdk", type=Path, help="Installed Google SDK for APK comparison"
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        for name in (
            "runtime",
            "jdk",
            "manager",
            "gradle",
            "gradle_cache",
            "inputs",
            "project",
            "external_kotlin_project",
            "native_libs",
            "reference_sdk",
        ):
            value = getattr(args, name)
            if value is not None:
                setattr(args, name, value.resolve(strict=True))
        if args.bundle:
            if args.feed or args.public_key:
                parser.error("--bundle already supplies --feed and --public-key")
            with TemporaryDirectory(
                prefix=".verify-feed-", dir=args.out.parent
            ) as work:
                bundle = Path(work)
                with tarfile.open(args.bundle) as archive:
                    archive.extractall(bundle, filter="data")
                args.feed = bundle / "feed"
                args.public_key = bundle / "feed-public.pem"
                if args.trusted_key:
                    if args.trusted_key.read_bytes() != args.public_key.read_bytes():
                        raise ValueError(
                            "Bundle key does not match the trusted SDK public key"
                        )
                    args.public_key = args.trusted_key
                print(test_sdk(args))
        elif args.feed and args.public_key:
            print(test_sdk(args))
        else:
            parser.error("Specify --bundle or both --feed and --public-key")
    except (
        OSError,
        ValueError,
        BadZipFile,
        tarfile.TarError,
        subprocess.SubprocessError,
    ) as error:
        print(f"test-component: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
