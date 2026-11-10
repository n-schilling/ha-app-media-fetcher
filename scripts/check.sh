#!/bin/bash
# Runs locally what the CI workflow runs, so a push only confirms what already
# passed here: the changelog check, shellcheck, ruff, the tests with their
# coverage threshold under Python 3.13 (the image's) and 3.14, and the image
# build with its start checks. Keep the steps in step with
# .github/workflows/ci.yaml. The app configuration linter
# (frenck/action-addon-linter) runs in CI only.
#
# Usage: scripts/check.sh [--no-image]
#
# The tools come from .venv (Python 3.13) and .venv-314 (Python 3.14) when
# they exist, see README, section Development; otherwise from PATH.
# The shell linter comes from PATH or, without it, from its Docker image. The
# image is built for this machine's architecture only, CI builds both. The AppArmor step needs a
# Linux kernel with AppArmor and runs in CI only.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

tool() { # tool <venv> <name>: the one in the virtual environment, else the one on PATH
    if [[ -x "$1/bin/$2" ]]; then
        echo "$1/bin/$2"
    elif command -v "$2"; then
        return
    else
        echo "$2 not found, neither in $1 nor on PATH; see README, section Development." >&2
        return 1
    fi
}

RUFF=$(tool .venv ruff) || exit 2
PYTEST=$(tool .venv pytest) || exit 2
PYTHON=$(tool .venv python3) || exit 2

failed=()
skipped=()

run() { # run <name> <command...>
    local name=$1
    shift
    echo "==> $name"
    if "$@"; then
        echo "    ok: $name"
    else
        echo "    FAILED: $name"
        failed+=("$name")
    fi
    echo
}

no_image=false
for arg in "$@"; do
    case "$arg" in
        --no-image) no_image=true ;;
        *) echo "unknown option $arg" >&2; exit 2 ;;
    esac
done

version=$("$PYTHON" -c 'import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))["version"])' \
    media_fetcher/config.yaml)
docker=false
if docker info > /dev/null 2>&1; then
    docker=true
fi

run "Changelog has the version $version" bash -c \
    '.github/scripts/changelog-section.sh media_fetcher/CHANGELOG.md "$1" > /dev/null' _ "$version"
scripts=(media_fetcher/rootfs/etc/s6-overlay/s6-rc.d/fetcher/run
    media_fetcher/rootfs/etc/s6-overlay/s6-rc.d/fetcher/finish
    scripts/check.sh .github/scripts/changelog-section.sh tests/apparmor.sh)
if command -v shellcheck > /dev/null; then
    run "Shell scripts" shellcheck --shell=bash --severity=warning "${scripts[@]}"
elif $docker; then
    run "Shell scripts" docker run --rm -v "$PWD":/mnt:ro -w /mnt koalaman/shellcheck:v0.11.0 \
        --shell=bash --severity=warning "${scripts[@]}"
else
    skipped+=("Shell scripts (neither shellcheck nor Docker)")
fi
run "Ruff" "$RUFF" check media_fetcher/rootfs/usr/lib/media_fetcher tests scripts
run "Tests (Python 3.13)" "$PYTEST" -q -p no:cacheprovider \
    --cov=media_fetcher --cov-report=term-missing:skip-covered --cov-fail-under=85
if [[ -x .venv-314/bin/pytest ]]; then
    run "Tests (Python 3.14)" .venv-314/bin/pytest -q -p no:cacheprovider
else
    skipped+=("Tests (Python 3.14): no .venv-314")
fi

if $no_image; then
    skipped+=("Image (--no-image)")
elif ! $docker; then
    skipped+=("Image (Docker is not running)")
else
    run "Image build" docker build -q --build-arg "BUILD_VERSION=$version" -t media-fetcher:check media_fetcher
    run "yt-dlp finds Deno and ffmpeg" bash -c '
        out=$(docker run --rm --network none --entrypoint yt-dlp media-fetcher:check -v 2>&1 || true)
        grep -q "JS runtimes: deno-" <<< "$out" && grep -q "exe versions: ffmpeg" <<< "$out"'
    run "Start without options" bash -c '
        out=$(docker run --rm --network none -e PYTHONPATH=/usr/lib --entrypoint python3 \
            media-fetcher:check -m media_fetcher 2>&1 || true)
        grep -q "/data/options.json not readable" <<< "$out"'
fi
skipped+=("App configuration linter (CI only)" "AppArmor profile (CI only, needs a kernel with AppArmor)")

echo "===================="
# The ${arr[@]+"${arr[@]}"} form keeps an empty array from tripping `set -u` in the bash 3.2 of macOS
for name in ${skipped[@]+"${skipped[@]}"}; do
    echo "skipped: $name"
done
if ((${#failed[@]})); then
    echo "FAILED: ${failed[*]}"
    exit 1
fi
echo "All checks passed."
