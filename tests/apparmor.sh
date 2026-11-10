#!/bin/bash
# Runs the image under the app's AppArmor profile and checks what the profile
# promises: the app starts and answers, the service may use the network,
# ffmpeg, ffprobe and Deno run in the child profile "offline" and reach no
# address, but still convert files and run JavaScript.
#
# Needs a kernel with AppArmor and the profile loaded first:
#   sudo apparmor_parser -r -W media_fetcher/apparmor.txt
# Usage: tests/apparmor.sh <image>
set -euo pipefail
image=$1
data=$(mktemp -d)
echo '{"token": "", "max_jobs": 2, "keep_minutes": 60, "max_size_mb": 2000, "allow_any_site": false, "log_level": "debug"}' \
    > "${data}/options.json"
cid=$(docker run -d --security-opt apparmor=media_fetcher -v "${data}:/data" "${image}")
cleanup() {
    echo "--- app log"
    docker logs "${cid}" 2>&1 | tail -n 30
    docker rm -f "${cid}" > /dev/null
}
trap cleanup EXIT

echo "--- the app starts under the profile and answers"
for _ in $(seq 1 30); do
    if docker exec "${cid}" python3 -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8099/api/v1/health', timeout=2)" 2> /dev/null; then
        break
    fi
    sleep 1
done
docker exec "${cid}" python3 -c \
    "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8099/api/v1/health', timeout=2).read().decode())"

docker exec -i "${cid}" bash -s << 'INSIDE'
set -euo pipefail
fail() { echo "FAILED: $*"; exit 1; }
profile() { tr -d '\0' < "/proc/$1/attr/current"; }

for pid in /proc/[0-9]*; do
    if grep -qa "media_fetcher" "${pid}/cmdline" 2> /dev/null && grep -qa "python" "${pid}/cmdline"; then
        service=${pid#/proc/}
    fi
done
[[ -n "${service:-}" ]] || fail "the service does not run"
[[ "$(profile "${service}")" == "media_fetcher (enforce)" ]] || fail "service runs as $(profile "${service}")"
echo "ok: the service runs as media_fetcher (enforce)"

mkdir -p /tmp/www
ffmpeg -v error -y -f lavfi -i sine=d=1 /tmp/www/a.m4a || fail "ffmpeg cannot convert under the profile"
echo "ok: ffmpeg converts"
python3 -m http.server 8000 --bind 127.0.0.1 -d /tmp/www > /dev/null 2>&1 &
sleep 1
python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/a.m4a', timeout=2)" \
    || fail "the service's profile has no network"
echo "ok: the service's profile reaches the network"

ffmpeg -v error -re -f lavfi -i sine=d=10 -f null - &
sleep 1
child=$(profile $!)
kill $! 2> /dev/null || true
[[ "${child}" == "media_fetcher//offline (enforce)" ]] || fail "ffmpeg runs as ${child}"
echo "ok: ffmpeg runs as ${child}"

ffprobe -v error -show_entries format=duration http://127.0.0.1:8000/a.m4a > /tmp/probe.log 2>&1 &
probe=$!
sleep 0.2
echo "ffprobe runs as $(profile ${probe} 2> /dev/null || echo gone)"
if wait "${probe}"; then
    cat /tmp/probe.log
    fail "ffprobe reached the network"
fi
out=$(cat /tmp/probe.log)
echo "ok: ffprobe reaches no address (${out})"
if out=$(ffmpeg -v error -y -i http://127.0.0.1:8000/a.m4a /tmp/b.m4a 2>&1); then
    fail "ffmpeg reached the network"
fi
echo "ok: ffmpeg reaches no address (${out})"

[[ "$(deno eval 'console.log(6 * 7)')" == "42" ]] || fail "Deno does not run under the profile"
echo "ok: Deno runs JavaScript"
if out=$(deno eval --allow-net 'await fetch("http://127.0.0.1:8000/a.m4a")' 2>&1); then
    fail "Deno reached the network"
fi
echo "ok: Deno reaches no address"
INSIDE
