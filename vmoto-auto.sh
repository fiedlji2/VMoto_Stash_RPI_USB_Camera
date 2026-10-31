#!/usr/bin/env bash
# Automatický režim: Wi-Fi (wlan0) patří jen displeji Vmoto, přístup k Pi je přes ethernet.
# NetworkManager wlan0 nespravuje (/etc/NetworkManager/conf.d/99-vmoto.conf), na wlan0 běží
# trvale vlastní wpa_supplicant. Smyčka: najdi displej -> připoj se -> pusť kameru ->
# když displej zmizí (vypnutá motorka), ukliď a hledej znovu.
# Pozastavení: touch <instalační složka>/DISABLE   (výchozí /opt/vmoto-carbit-cam/DISABLE)
# Demo režim: bez DASH_MAC se připojí k jakémukoli displeji Carbit v dosahu (Wi-Fi Direct název RM_*),
# při více displejích k tomu s nejsilnějším signálem. DASH_MAC=xx:xx:.. = jen ten jeden displej.
export PATH="$PATH:/usr/sbin:/sbin"
DIR="$(cd "$(dirname "$0")" && pwd)"
FIXED_MAC="${DASH_MAC:-}"
DASH=""
DASH_FREQ=""
LOGGER_ARGS="${LOGGER_ARGS:---camera}"
CTRL=/run/wpa_vmoto
CONF=/run/vmoto_p2p.conf
PIDF=/run/vmoto_wpa.pid
W="wpa_cli -p $CTRL -i p2p-dev-wlan0"
cd "$DIR"

log() { echo "$(date '+%F %T') [auto] $*"; }

# stav pro webovou stránku (webui.py): /run/vmoto/auto.json
mkdir -p /run/vmoto
DASH_NAME=""; DASH_LVL=""; GIF=""; HU=""; SESS_START=0; SESSIONS=0
state() {
  cat > /run/vmoto/auto.json.tmp <<EOF
{"state":"$1","since":$(date +%s),"session_start":$SESS_START,"sessions":$SESSIONS,
 "fixed_mac":"$FIXED_MAC","dash_name":"$DASH_NAME","dash_mac":"$DASH","signal":"$DASH_LVL",
 "freq":"$DASH_FREQ","iface":"$GIF","hu_ip":"$HU","note":"${2:-}"}
EOF
  mv /run/vmoto/auto.json.tmp /run/vmoto/auto.json
}

drop_p2p_ifaces() {
  for x in $(iw dev | awk '/Interface p2p-wlan0-/{print $2}'); do
    wpa_cli -p $CTRL -i p2p-dev-wlan0 p2p_group_remove "$x" >/dev/null 2>&1
    iw dev "$x" del 2>/dev/null
  done
}

supplicant_up() {
  if [ -f "$PIDF" ] && kill -0 "$(cat $PIDF)" 2>/dev/null && $W ping >/dev/null 2>&1; then return 0; fi
  log "spouštím wpa_supplicant na wlan0"
  [ -f "$PIDF" ] && kill "$(cat $PIDF)" 2>/dev/null
  sleep 1
  cat > "$CONF" <<EOF
ctrl_interface=DIR=$CTRL
update_config=0
country=US
device_name=Pi-Camera
device_type=10-0050F204-5
config_methods=virtual_push_button physical_push_button
p2p_go_intent=0
EOF
  ip link set wlan0 up
  wpa_supplicant -B -i wlan0 -c "$CONF" -D nl80211 -P "$PIDF"
  sleep 3
  iw reg set US; sleep 1
  iw dev wlan0 set txpower fixed 1300 2>/dev/null
}

# vybere displej: pevná MAC, jinak nejsilnější zařízení s názvem RM_* (displeje Carbit/EasyConn)
pick_dash() {
  local best="" best_lvl=-999 best_freq="" best_name=""
  for p in $($W p2p_peers 2>/dev/null); do
    local info name lvl freq
    info="$($W p2p_peer "$p" 2>/dev/null)"
    name="$(echo "$info" | sed -n 's/^device_name=//p')"
    lvl="$(echo "$info" | sed -n 's/^level=//p')"; lvl="${lvl:--999}"
    freq="$(echo "$info" | sed -n 's/^listen_freq=//p')"
    if [ -n "$FIXED_MAC" ]; then
      [ "${p,,}" = "${FIXED_MAC,,}" ] || continue
    else
      case "$name" in RM_*) ;; *) continue ;; esac
    fi
    if [ "$lvl" -gt "$best_lvl" ]; then best="$p"; best_lvl="$lvl"; best_freq="$freq"; best_name="$name"; fi
  done
  [ -n "$best" ] || return 1
  DASH="$best"; DASH_FREQ="$best_freq"; DASH_NAME="$best_name"; DASH_LVL="$best_lvl"
  log "displej $best_name ($best), signál $best_lvl dBm, kanál ${best_freq:-?} MHz"
}

find_dash() {
  $W p2p_flush >/dev/null
  $W p2p_find 10 >/dev/null
  for _ in $(seq 1 10); do
    sleep 1
    if pick_dash; then $W p2p_stop_find >/dev/null; return 0; fi
  done
  $W p2p_stop_find >/dev/null
  return 1
}

session() {
  log "displej nalezen, připojuji"
  state connecting
  drop_p2p_ifaces
  $W p2p_prov_disc "$DASH" pbc join >/dev/null
  sleep 3
  local fopt=""
  [ -n "$DASH_FREQ" ] && [ "$DASH_FREQ" != "0" ] && fopt="freq=$DASH_FREQ"
  $W p2p_connect "$DASH" pbc join $fopt >/dev/null
  local gif="" lpid=""
  for _ in $(seq 1 40); do
    for x in $(iw dev | awk '/Interface p2p-wlan0-/{print $2}'); do
      wpa_cli -p $CTRL -i "$x" status 2>/dev/null | grep -q 'wpa_state=COMPLETED' && { gif=$x; break 2; }
    done
    sleep 1
  done
  if [ -z "$gif" ]; then
    log "připojení k displeji se nepovedlo"
    state searching "poslední připojení se nepovedlo"
    $W p2p_cancel >/dev/null 2>&1
    drop_p2p_ifaces
    return
  fi
  log "připojeno na $gif"
  local lease=/run/vmoto_dhcp_$gif.lease
  rm -f "$lease"
  timeout 20 dhclient -1 -lf "$lease" "$gif" && ip -4 -br addr show "$gif"
  # IP displeje = DHCP server (u Vmoto 192.168.2.1, jinde může být jiná); port a aplikaci doplní mDNS
  local hu
  hu="$(sed -n 's/.*option dhcp-server-identifier \([0-9.]*\);.*/\1/p' "$lease" 2>/dev/null | tail -1)"
  hu="${hu:-192.168.2.1}"
  log "displej na $hu"
  GIF="$gif"; HU="$hu"; SESS_START=$(date +%s); SESSIONS=$((SESSIONS + 1))
  state connected
  mkdir -p logs
  python3 -u logger.py --outdir logs --ec-init "$hu:10930" $LOGGER_ARGS &
  lpid=$!
  local bad=0
  while kill -0 "$lpid" 2>/dev/null; do
    sleep 2
    if wpa_cli -p $CTRL -i "$gif" status 2>/dev/null | grep -q 'wpa_state=COMPLETED'; then bad=0; else bad=$((bad + 1)); fi
    [ -f "$DIR/DISABLE" ] && { log "DISABLE -> konec relace"; break; }
    [ "$bad" -ge 3 ] && { log "Wi-Fi Direct spojení s displejem skončilo"; break; }
  done
  kill "$lpid" 2>/dev/null; wait "$lpid" 2>/dev/null
  pkill -f rpicam-vid 2>/dev/null
  dhclient -r "$gif" 2>/dev/null
  pkill -f "dhclient.*$gif" 2>/dev/null
  drop_p2p_ifaces
  GIF=""; HU=""; SESS_START=0
  state searching "spojení s displejem skončilo $(date +%H:%M:%S)"
  rm -f /run/vmoto/logger.json /run/vmoto/frame.h264
  log "relace ukončena, hledám displej znovu"
}

trap 'log "konec služby"; pkill -f rpicam-vid; drop_p2p_ifaces; [ -f "$PIDF" ] && kill "$(cat $PIDF)" 2>/dev/null' EXIT

log "start, hledám ${FIXED_MAC:-jakýkoli displej Carbit (RM_*)}"
state searching
while true; do
  if [ -f "$DIR/DISABLE" ]; then state paused "pozastaveno (DISABLE)"; sleep 5; continue; fi
  [ "$(sed -n 's/.*"state":"\([a-z]*\)".*/\1/p' /run/vmoto/auto.json 2>/dev/null)" = paused ] && state searching
  supplicant_up
  if find_dash; then
    session
    sleep 2
  else
    sleep 2
  fi
done
