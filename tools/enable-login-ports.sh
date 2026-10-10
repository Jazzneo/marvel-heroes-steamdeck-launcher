#!/bin/sh
# One-time setup: lets normal programs (the MH Launcher login bridge) use ports 80 and 443.
# The Marvel Heroes client always logs in on port 443, which Linux reserves for root by default.
# Undo with: sudo ./disable-login-ports.sh
set -e
if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo:  sudo \"$0\""
    echo "(Steam Deck: if you never set a password, run 'passwd' first.)"
    exit 1
fi
mkdir -p /etc/sysctl.d
echo "net.ipv4.ip_unprivileged_port_start=80" > /etc/sysctl.d/60-mhlauncher.conf
sysctl -w net.ipv4.ip_unprivileged_port_start=80 >/dev/null
echo "Done! Ports 80 and 443 can now be used by MH Launcher (setting survives reboots)."
