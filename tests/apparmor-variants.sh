#!/bin/bash
# Temporary: which profile layout keeps the child off the network on this kernel
set -uo pipefail
dir=$(mktemp -d)
try() { # try <profile>
    if sudo aa-exec -p "$1" -- python3 -c 'import socket; s = socket.socket(); s.close()' 2> /dev/null; then
        echo "  $1: inet allowed"
    else
        echo "  $1: inet denied"
    fi
}
cat /sys/kernel/security/apparmor/features/network/af_mask 2> /dev/null | head -c 200; echo
ls /sys/kernel/security/apparmor/features/ ; ls /sys/kernel/security/apparmor/features/network* 2>/dev/null
echo "== A: the app's file (nested child)"
sudo apparmor_parser -r media_fetcher/apparmor.txt && { try media_fetcher; try media_fetcher//offline; }
echo "== B: nested child without deny, without network unix"
sed -e '/deny network inet/d' -e '/^    network unix,/d' -e 's/profile media_fetcher /profile mfb /' -e 's/-> offline/-> offline/' \
    media_fetcher/apparmor.txt > "$dir/b"
sudo apparmor_parser -r "$dir/b" && { try mfb; try mfb//offline; }
echo "== C: two top-level profiles"
cat > "$dir/c" << 'P'
#include <tunables/global>
profile mfc flags=(attach_disconnected,mediate_deleted) {
  #include <abstractions/base>
  network inet,
  network inet6,
  network unix,
  file,
  /usr/bin/curl Px -> mfc_offline,
}
profile mfc_offline flags=(attach_disconnected,mediate_deleted) {
  #include <abstractions/base>
  network unix,
  file,
}
P
sudo apparmor_parser -r "$dir/c" && { try mfc; try mfc_offline; }
echo "== D: nested with abi 4.0"
sed -e 's/profile media_fetcher /profile mfd /' -e '1a abi <abi/4.0>,' -e '/deny network inet/d' media_fetcher/apparmor.txt > "$dir/d"
sudo apparmor_parser -r "$dir/d" && { try mfd; try mfd//offline; }
echo "== E: nested, abi 3.0"
sed -e 's/profile media_fetcher /profile mfe /' -e '1a abi <abi/3.0>,' -e '/deny network inet/d' media_fetcher/apparmor.txt > "$dir/e"
sudo apparmor_parser -r "$dir/e" && { try mfe; try mfe//offline; }
grep -v '^#' /etc/apparmor/parser.conf | grep . || true
