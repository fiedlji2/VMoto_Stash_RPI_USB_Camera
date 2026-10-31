#!/usr/bin/env bash
# Odinstalace: vypne služby, vrátí Wi-Fi NetworkManageru. Balíčky ani nastavení ethernetu nemění.
set -euo pipefail
INSTALL_DIR="${INSTALL_DIR:-/opt/vmoto-carbit-cam}"
[ "$(id -u)" -eq 0 ] || { echo "Spusť jako root: sudo ./uninstall.sh" >&2; exit 1; }
systemctl disable --now vmoto-auto vmoto-web vmoto-wifi-power 2>/dev/null || true
rm -f /etc/systemd/system/vmoto-{auto,web,wifi-power}.service
rm -f /etc/NetworkManager/conf.d/99-vmoto.conf /etc/dhcp/dhclient-enter-hooks.d/vmoto-p2p /etc/logrotate.d/vmoto-auto
systemctl daemon-reload
systemctl restart NetworkManager
rm -rf "$INSTALL_DIR"
echo "Odinstalováno. Wi-Fi zemi vrátíš: sudo raspi-config nonint do_wifi_country CZ && sudo reboot"
