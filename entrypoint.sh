#!/bin/bash
set -e

mkdir -p /run/dbus
rm -f /run/dbus/pid /run/dbus/system_bus_socket

dbus-daemon --system --fork >/dev/null 2>&1 || true
avahi-daemon --no-drop-root -D >/dev/null 2>&1 || true
sleep 2

exec python3 /app/app/app.py