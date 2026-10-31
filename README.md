# vmoto-carbit-cam

Turn a **Raspberry Pi + USB camera** into a wireless rear-view camera for motorcycles whose TFT dash supports
**phone projection via Carbit / EasyConn** (e.g. Vmoto Stash, and other brands using the Carbit "Carbit Ride" /
EasyConn mirroring). The Pi pretends to be the phone app, connects to the dash over Wi-Fi Direct and streams
live H.264 video to it – no phone, no app, no modification of the bike.

- Plug in power → the Pi finds **any Carbit dash in range** (strongest signal wins) and starts streaming
- Bike restarted / camera unplugged → recovers automatically
- **~150 ms** glass-to-glass latency, ~25 fps, 1024×592 (whatever the dash requests)
- Mirrored image (rear-view mirror), digital zoom 1–2× with pan, WDR, brightness/contrast/gamma…
- Status & settings web page over Ethernet, changes apply **live** on the dash, *Save* makes them permanent

> [!WARNING]
> **Vibe-coded, for testing only.** This project was put together quickly with an AI assistant, tested on one
> bike and one camera. It may contain bugs, it is not a finished product and it comes with no warranty.
> Use it for experiments and demos, not as safety equipment on the road.

> **Česky:** viz [níže](#česky).

## Hardware

| Part | Tested |
|---|---|
| Raspberry Pi | **Pi 4** (Pi 3 A+ has the same Wi-Fi chip and HW encoder and should work) |
| Camera | **RunCam WebCam Nano** (UVC, MJPEG 1280×720@30, 170° FOV) – any UVC MJPEG camera should work |
| Dash | **Vmoto Stash** (Carbit `sdkVersion 0.9.23.3`, Wi-Fi Direct on 5 GHz channel 149) |
| Access | Ethernet cable to a PC (Wi-Fi is fully used by the dash link) |

Pi 5 / Pi Zero 2 W are not suitable as-is (no HW H.264 encoder / no 5 GHz Wi-Fi).

## Install (a few clicks)

1. **Raspberry Pi Imager** → *Raspberry Pi OS Lite (64-bit)* → in *Edit settings* set user, enable SSH.
   Wi-Fi is not needed (connect the Pi by Ethernet for the install, it needs internet once).
2. On the Pi:
   ```bash
   curl -L https://github.com/fiedlji2/VMoto_Stash_RPI_USB_Camera/archive/refs/heads/main.tar.gz | tar xz
   cd VMoto_Stash_RPI_USB_Camera-main
   sudo ./install.sh
   sudo reboot
   ```
   The installer sets Ethernet to a **static 192.168.1.10/24** for a direct cable to your PC
   (`sudo ETH_STATIC= ./install.sh` keeps DHCP).
3. Plug the USB camera in, switch the bike's dash to *phone connection* mode, power the Pi. Done.

Settings page: **http://192.168.1.10/** (set the PC's Ethernet to e.g. `192.168.1.20 / 255.255.255.0`).

Uninstall: `sudo ./uninstall.sh`.

## Web page

- Connection state, dash name/MAC/signal/channel, frames per second, real bitrate, frame age, Pi temperature
  and **under-voltage** warning, recent log
- Camera: mirror, digital zoom + pan, bitrate (quality), WDR, brightness, contrast, saturation, gamma, sharpness,
  white balance, exposure, anti-flicker – all **live**; *Save* writes `camera.conf` / `view.json`
- **EN / CS** language switch (English by default, remembered in the browser)
- JSON API: `/api/status`, `/api/camera`, `/api/view` (see `web.py`)

## How it works

`vmoto-auto.sh` runs its own `wpa_supplicant` on `wlan0`, discovers the dash (Wi-Fi Direct device `RM_*`),
joins its group with WPS push-button, then starts `logger.py`, which implements the phone side of the
Carbit/EasyConn protocol (mDNS discovery, PXC control channel with its own RSA key, media channels) and serves
H.264 frames from the camera (GStreamer: MJPEG → decode → mirror → crop/zoom → scale → HW H.264, intra-only,
newest frame on every request). Protocol notes: [docs/PROTOCOL.md](docs/PROTOCOL.md).

Files: `logger.py` (protocol + camera), `vmoto-auto.sh` (Wi-Fi Direct loop), `web.py` (status page),
`systemd/`, `config/`, `install.sh`.

## Notes & legal

- **Wi-Fi country:** the dash uses 5 GHz channel 149 (5745 MHz). The Pi's Broadcom firmware disables
  channels 149–165 for EU country codes, so the installer sets country **US** and caps TX power to **13 dBm
  (~20 mW)**. In the EU, 5725–5875 MHz is allowed for short-range devices up to 25 mW e.i.r.p. You are
  responsible for complying with the radio rules in your country.
- Not affiliated with Carbit, EasyConn or any motorcycle maker. Names are trademarks of their owners.
  The protocol was analysed for interoperability with the owner's own device; no vendor keys or code are included.
- Watching a screen while riding is your responsibility – use it as a mirror, not as entertainment.
- The Pi needs a stable 5 V / 3 A supply; the page shows under-voltage.

## License

GPL-3.0 – see [LICENSE](LICENSE).

---

## Česky

> [!WARNING]
> **Vibecoded projekt, jen pro testovací účely.** Vzniklo rychle s pomocí AI asistenta a vyzkoušené je jen
> na jedné motorce a jedné kameře. Může obsahovat chyby, není to hotový produkt a není na to žádná záruka.
> Je to na zkoušení a předvádění, ne jako bezpečnostní výbava do provozu.

Raspberry Pi s USB kamerou jako bezdrátová zpětná kamera pro motorky s displejem Carbit / EasyConn
(např. Vmoto Stash). Pi se tváří jako aplikace v telefonu, připojí se k displeji přes Wi-Fi Direct a posílá
živý obraz (~150 ms zpoždění).

**Instalace:** Raspberry Pi OS Lite (64-bit) přes Raspberry Pi Imager (zapnout SSH), Pi připojit kabelem
k internetu a na něm:
```bash
curl -L https://github.com/fiedlji2/VMoto_Stash_RPI_USB_Camera/archive/refs/heads/main.tar.gz | tar xz
cd VMoto_Stash_RPI_USB_Camera-main
sudo ./install.sh
sudo reboot
```
Pak zapojit USB kameru, displej motorky přepnout do režimu pro připojení mobilu a zapnout Pi.
Nastavení na **http://192.168.1.10/** (PC na ethernetu např. `192.168.1.20 / 255.255.255.0`):
zrcadlení, zoom, datový tok, WDR, jas, kontrast… – změny se projeví hned, tlačítko *Uložit* je zachová.
Stránka je anglicky, přepínačem **CS** vpravo nahoře se přepne do češtiny (prohlížeč si volbu pamatuje).

Pi se připojí k jakémukoli displeji Carbit v dosahu (nejsilnější signál). Pevný displej:
`DASH_MAC=xx:xx:xx:xx:xx:xx` v `systemctl edit vmoto-auto`. Podrobnosti protokolu: [docs/PROTOCOL.md](docs/PROTOCOL.md).
