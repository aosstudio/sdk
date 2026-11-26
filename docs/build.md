# Build and install the SDK

Run the commands from the repository root on Linux x86-64. Install Python 3.14, curl, OpenSSL, make, unzip and Bubblewrap.

## Assemble

```sh
make build
```

The command fetches the exact assets in [the release lock](../locks/release.json), checks their SHA-256 digests, and packages the four SDK ZIPs with SDK Manager indexes and a signed manifest. It writes the bundle to `out/dist/aosstudio-sdk-37.0-linux-x86_64.tar.gz`. The AOSStudio JDK and AGP, Gradle and locked Maven dependencies support verification and stay outside the SDK bundle.

Local builds use a temporary signing key. To sign a distribution with an existing Ed25519 key, pass `SIGNING_KEY=/path/to/private.pem`. Also pass `TRUSTED_KEY=/path/to/public.pem` to check that the signing key matches the expected public key.

## Install

Extract the SDK Release bundle and the AOSStudio JDK Release. Verify the feed with the [AOSStudio public key](../keys/feed-public.pem) from a trusted checkout before running the SDK Manager. The example below uses the signed Command-Line Tools ZIP as the bootstrap.

```sh
cd /path/to/sdk/repository
bundle=/path/to/extracted-bundle
jdk=/path/to/extracted-jdk
openssl pkeyutl -verify -pubin -inkey keys/feed-public.pem -rawin \
  -in "$bundle/feed/release-assets.sha256" \
  -sigfile "$bundle/feed/release-assets.sha256.sig"
(cd "$bundle/feed" && sha256sum --check release-assets.sha256)
unzip "$bundle"/feed/aosstudio-cmdline-tools-*-linux-x86_64.zip -d /path/to/bootstrap
python3 scripts/install_sdk.py \
  --feed "$bundle/feed" --public-key keys/feed-public.pem \
  --jdk "$jdk" --manager /path/to/bootstrap/cmdline-tools \
  --destination /path/to/new-sdk
```

The destination's parent must already exist. Append exact SDK package IDs to select packages, for example `'platforms;android-37.0'`. Run `python3 scripts/install_sdk.py --default-packages` to list the default IDs. Pass `--add` with an existing AOSStudio SDK path to install additional signed packages.

## Verify

```sh
make verify
```

Verification installs the just-assembled bundle into a fresh directory. It builds the Java, built-in Kotlin and external Kotlin samples twice offline and checks APK identity, v2 signing and pairwise reproducibility. The manifest and logs are under `out/verification`.

Pass `SDK_BUNDLE=/path/to/downloaded-bundle.tar.gz TRUSTED_KEY=keys/feed-public.pem` to verify a published bundle after `make fetch`. Choose a new `SDK_TEST_OUTPUT` directory for each run.

### Compare with an installed Google SDK

The reference SDK needs the same platform and Build Tools versions. A read-only sandbox uses the reference SDK's own tools with the same AOSStudio JDK and AGP, Gradle and samples.

```sh
make verify SDK_TEST_OUTPUT=out/reference-verification REFERENCE_SDK=/path/to/google-sdk
```

The comparison records both SDK configurations, APK SHA-256 digests, ZIP entry differences, signing certificates and the application ID and API levels. A matching manifest identity and signing certificate are required. Archive-level and entry-level byte differences are reported.
