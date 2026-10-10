#!/bin/sh
# Reverts enable-login-ports.sh
set -e
if [ "$(id -u)" -ne 0 ]; then echo "Please run with sudo:  sudo \"$0\""; exit 1; fi
rm -f /etc/sysctl.d/60-mhlauncher.conf
sysctl -w net.ipv4.ip_unprivileged_port_start=1024 >/dev/null
echo "Reverted. Ports below 1024 are root-only again."
