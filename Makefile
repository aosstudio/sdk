PYTHON ?= python3
INPUTS ?= out/release-inputs
SDK_TEST_OUTPUT ?= out/verification
SDK_BUNDLE ?= $(wildcard out/dist/aosstudio-sdk-*-linux-x86_64.tar.gz)
SCRIPTS := $(wildcard scripts/*.py)

.PHONY: check fetch build verify

check:
	$(PYTHON) -m ruff format --check $(SCRIPTS)
	$(PYTHON) -m ruff check $(SCRIPTS)
	$(PYTHON) -m mypy --strict --scripts-are-modules $(SCRIPTS)

fetch:
	$(PYTHON) scripts/inputs.py --out "$(INPUTS)"

build: fetch
	$(PYTHON) scripts/assemble_sdk.py --inputs "$(INPUTS)" --out out/dist

verify:
	mkdir -p "$(dir $(SDK_TEST_OUTPUT))" "$(INPUTS)/gradle-cache"
	$(PYTHON) scripts/test_sdk.py --bundle "$(SDK_BUNDLE)" --jdk "$(INPUTS)/jdk" \
	  --manager "$(INPUTS)/manager" --gradle "$(INPUTS)/gradle" \
	  --gradle-cache "$(INPUTS)/gradle-cache" --inputs "$(INPUTS)/maven" \
	  --project examples/built-in --external-kotlin-project examples/external \
	  $(if $(REFERENCE_SDK),--reference-sdk "$(REFERENCE_SDK)") --out "$(SDK_TEST_OUTPUT)"
