#!/usr/bin/env bash
# Instalace na čistý Raspberry Pi OS Lite (64-bit, Debian 13 Trixie), Raspberry Pi 4 / 3 A+.
# Spuštění ze složky repozitáře:   sudo ./install.sh
#
# Co udělá:
#   - doinstaluje balíčky (GStreamer, Python, Wi-Fi nástroje)
#   - zkopíruje program do /opt/vmoto-carbit-cam
#   - wlan0 předá displeji (NetworkManager ji přestane spravovat -> Pi nebude na domácí Wi-Fi!)
#   - Wi-Fi země US + výkon 13 dBm (kanál 149, viz README)
#   - ethernet nastaví na statickou adresu (výchozí 192.168.1.10/24) pro přímé připojení k PC
#   - zapne služby vmoto-auto (displej + kamera) a vmoto-web (stránka na portu 80)
#
# Volby (proměnné prostředí):
#   ETH_STATIC=192.168.1.10/24   statická adresa ethernetu; ETH_STATIC= (prázdné) = nechat DHCP
#   INSTALL_DIR=/opt/vmoto-carbit-cam
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/vmoto-carbit-cam}"
ETH_STATIC="${ETH_STATIC-192.168.1.10/24}"
SRC="$(cd "$(dirname "$0")" && pwd)"
export PATH="$PATH:/usr/sbin:/sbin"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Spusť jako root: sudo ./install.sh" >&2; exit 1; }
[ -f "$SRC/logger.py" ] || { echo "Spusť ze složky repozitáře (chybí logger.py)." >&2; exit 1; }

say "Balíčky"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
  python3 python3-cryptography python3-zeroconf python3-gi python3-gst-1.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-x v4l-utils iw wpasupplicant isc-dhcp-client >/dev/null

say "Program do $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/config"
install -m 755 "$SRC/vmoto-auto.sh" "$INSTALL_DIR/vmoto-auto.sh"
install -m 644 "$SRC/logger.py" "$SRC/web.py" "$INSTALL_DIR/"
install -m 755 "$SRC/config/vmoto-wifi-power" "$INSTALL_DIR/config/vmoto-wifi-power"

say "Konfigurace systému"
install -m 644 "$SRC/config/99-vmoto.conf" /etc/NetworkManager/conf.d/99-vmoto.conf
install -m 644 "$SRC/config/dhclient-vmoto-p2p" /etc/dhcp/dhclient-enter-hooks.d/vmoto-p2p
install -m 644 "$SRC/config/logrotate-vmoto-auto" /etc/logrotate.d/vmoto-auto
if command -v raspi-config >/dev/null; then
  raspi-config nonint do_wifi_country US || true      # země pro Wi-Fi (cmdline.txt), viz README
fi

WEB_BIND=0.0.0.0
[ -n "$ETH_STATIC" ] && WEB_BIND="${ETH_STATIC%/*}"
for s in vmoto-auto vmoto-web vmoto-wifi-power; do
  sed "s|/opt/vmoto-carbit-cam|$INSTALL_DIR|g; s|^Environment=WEB_BIND=.*|Environment=WEB_BIND=$WEB_BIND|" \
    "$SRC/systemd/$s.service" > "/etc/systemd/system/$s.service"
done
systemctl daemon-reload
systemctl enable vmoto-wifi-power vmoto-auto vmoto-web >/dev/null 2>&1
systemctl reload NetworkManager 2>/dev/null || systemctl restart NetworkManager

say "Spouštím služby"
systemctl restart vmoto-wifi-power vmoto-web vmoto-auto

if [ -n "$ETH_STATIC" ]; then
  CON="$(nmcli -t -f NAME,DEVICE con show 2>/dev/null | awk -F: '$2=="eth0"{print $1; exit}')"
  [ -z "$CON" ] && CON="$(nmcli -t -f NAME,TYPE con show | awk -F: '$2=="802-3-ethernet"{print $1; exit}')"
  if [ -z "$CON" ]; then
    CON=vmoto-eth
    nmcli con add type ethernet ifname eth0 con-name "$CON" >/dev/null
  fi
  nmcli con modify "$CON" ipv4.method manual ipv4.addresses "$ETH_STATIC" ipv4.gateway "" ipv4.dns "" \
    ipv4.never-default yes ipv6.method link-local
  say "Ethernet: statická adresa $ETH_STATIC (projeví se za 3 s; SSH přes ethernet se teď odpojí)"
  systemd-run --unit vmoto-eth-apply --collect --on-active=3 /usr/bin/nmcli con up "$CON" >/dev/null
fi

cat <<EOF

Hotovo.
  - Pi teď hledá jakýkoli displej Carbit/EasyConn v dosahu (motorka v režimu pro připojení mobilu).
  - Stránka:  http://${WEB_BIND/0.0.0.0/<adresa Pi>}/   (PC na stejném ethernetu, např. 192.168.1.20/24)
  - Log:      /var/log/vmoto-auto.log
  - Po restartu Pi se projeví i země Wi-Fi z cmdline.txt:  sudo reboot
EOF
