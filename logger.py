#!/usr/bin/env python3
"""Logger provozu displeje Vmoto Stash (Carbit / EasyConn).

Pi se tváří jako telefon: poslouchá na TCP 10920-10922 (displej se připojuje sám)
a na UDP 10500/14500. Vše, co displej pošle, se uloží:
  logs/<čas>/events.jsonl        rozparsované pakety (hlavička + payload)
  logs/<čas>/conn<N>_<port>.rx   surová data od displeje
  logs/<čas>/conn<N>_<port>.tx   surová data, která jsme poslali my

Hlavička EasyConn (16 B, little-endian):
  u32 cmd | u32 total_len (vč. hlavičky) | u32 cmd ^ total_len | u32 rezerva
Odpověď má cmd + 1.

Jediné, co logger aktivně posílá: na první paket každého spojení na portu 10922
(typ kanálu, např. 0x10000) odpoví cmd+1 se stejným payloadem - stejně jako
aplikace Carbit. Vypnout přes --no-handshake.
"""
import argparse
import asyncio
import base64
import datetime as dt
import json
import os
import socket
import struct
import sys
import time
from pathlib import Path

HDR = struct.Struct("<IIII")
MAX_LEN = 16 * 1024 * 1024

CONN_TYPES = {
    0x00010000: "C2P (displej -> telefon)",
    0x00020000: "P2C (telefon -> displej)",
    0x00030000: "conn 0x30000",
    0x00033000: "conn 0x33000",
    0x00034000: "conn 0x34000",
    0x00040000: "conn 0x40000",
    0x01010000: "conn 0x01010000",
    0x01020000: "H2P",
    0x01030000: "V2P",
    0x01040000: "conn 0x01040000",
    0x01050000: "conn 0x01050000",
    0x01060000: "conn 0x01060000",
    0x01070000: "conn 0x01070000",
    0x01080000: "conn 0x01080000",
    0x01090000: "conn 0x01090000",
}


CLIENT_INFO_CMD = 0x00010010
KEY_FILE = Path.home() / ".vmoto_phone_key.pem"


class PhoneIdentity:
    """Vlastní RSA-1024 pár "telefonu" (aplikace Carbit si ho také generuje sama).
    pubkey = base64 DER SubjectPublicKeyInfo, encryptedHUID = HUID zašifrované
    soukromým klíčem (RSA PKCS#1 v1.5, typ bloku 1), base64 bez zalomení."""

    def __init__(self, key_file: Path = KEY_FILE):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        if key_file.exists():
            self.key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
        else:
            self.key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
            key_file.write_bytes(self.key.private_bytes(serialization.Encoding.PEM,
                                                        serialization.PrivateFormat.PKCS8,
                                                        serialization.NoEncryption()))
            key_file.chmod(0o600)
        der = self.key.public_key().public_bytes(serialization.Encoding.DER,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo)
        self.pubkey_b64 = base64.b64encode(der).decode()

    def encrypt_huid(self, huid: str) -> str:
        nums = self.key.private_numbers()
        n, d = nums.public_numbers.n, nums.d
        k = (n.bit_length() + 7) // 8
        msg = huid.encode()
        em = b"\x00\x01" + b"\xff" * (k - 3 - len(msg)) + b"\x00" + msg
        c = pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")
        return base64.b64encode(c).decode()


def client_info_reply(req: dict, ident: "PhoneIdentity", args) -> dict:
    return {
        "pxcVersion": "1.0.2",
        "phoneUUID": args.phone_uuid,
        "phoneBrand": "Raspberry",
        "phoneModel": "Pi-Logger",
        "phoneOsVersion": "34",
        "phoneOs": "Android",
        "package": args.package,
        "versionCode": 47,
        "token": 0,
        "pubkey": ident.pubkey_b64,
        "encryptedHUID": ident.encrypt_huid(str(req.get("HUID", ""))),
        "bluetoothName": "Pi-Logger",
        "supportH264IFrame": True,
        "supportFunction": req.get("supportFunction", 0),
        "appVersionFingerPrint": "V:2.4(47)-pilogger-release",
    }


MEDIA_HDR = struct.Struct("<HHI")  # u16 cmd | u16 délka těla | u32 token
MEDIA_CTRL_PORT = 10921
MEDIA_DATA_PORT = 10920
MEDIA_NAMES = {
    0x10: "REQ_RV_CONFIG_CAPTURE", 0x20: "REQ_SCREEN_EVENT", 0x30: "REQ_GET_VERSION",
    0x40: "REQ_HEARTBEAT", 0x50: "REQ_OPERATE_SCREEN", 0x60: "REQ_CONFIGCAPTUREREXTEND",
    0x70: "REQ_RV_DATA_START", 0x72: "REQ_RV_DATA_NEXT", 0x80: "REQ_START_H264",
    0x110: "REQ_EXECUTE_SHELL", 0x300: "REQ_DISPLAY_SIZE",
}


def parse_config_capture(body: bytes) -> dict:
    w, h, fps, enc, codec, qmin, qmax, br = struct.unpack_from("<hhiiiHHi", body)
    out = {"width": w, "height": h, "wantFps": fps, "wantEncoder": enc, "supportCodec": codec,
           "minQuality": qmin, "maxQuality": qmax, "bitRate": br}
    if len(body) >= 32:
        cap, touch, orient, disp, vtype, ext = struct.unpack_from("<6B", body, 24)
        out.update(capScreenMode=cap, touchMode=touch, orientation=orient, displayId=disp,
                   videoType=vtype, supportExtendProtocol=ext)
    if len(body) > 32:
        out["encryptedHUID"] = body[32:].decode("utf-8", "replace")
    return out


def load_h264_aus(path: str) -> list:
    """Rozdělí Annex-B soubor na access units podle AUD (00 00 00 01 09)."""
    data = Path(path).read_bytes()
    marks = []
    i = data.find(b"\x00\x00\x00\x01\x09")
    while i != -1:
        marks.append(i)
        i = data.find(b"\x00\x00\x00\x01\x09", i + 5)
    if not marks:
        return [data]
    marks.append(len(data))
    return [data[a:b] for a, b in zip(marks, marks[1:])]


class LiveCamera:
    """Živý H.264 z kamery přes rpicam-vid (HW enkodér Pi 4), rozdělený na access units."""

    def __init__(self, extra_args: str = "", intra: int = 10, bitrate: int = 4000000, queue: int = 8):
        self.extra = extra_args.split() if extra_args else []
        self.intra = intra
        self.bitrate = bitrate
        self.proc = None
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue)
        self.wait_idr = False
        self.frames = 0
        self.dropped = 0
        self.age_sum = 0.0       # jak dlouho snímek čekal od enkodéru do odeslání
        self.age_n = 0
        self.age_max = 0.0
        self.last_age = 0.0
        self.last_size = 0

    def age_stats(self) -> str:
        avg = self.age_sum / self.age_n * 1000 if self.age_n else 0
        s = f"stáří snímku průměr {avg:.0f} ms, max {self.age_max * 1000:.0f} ms"
        self.age_sum, self.age_n, self.age_max = 0.0, 0, 0.0
        return s

    async def start(self, width: int, height: int, fps: int):
        if self.proc:
            return
        cmd = ["rpicam-vid", "-n", "-t", "0", "--width", str(width), "--height", str(height),
               "--framerate", str(fps), "--codec", "h264", "--profile", "baseline", "--level", "4.1",
               "--intra", str(self.intra), "--inline", "--flush", "--bitrate", str(self.bitrate),
               "-o", "-"] + self.extra
        print("[camera] " + " ".join(cmd), flush=True)
        self.proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.DEVNULL)
        asyncio.get_running_loop().create_task(self._reader())

    def _push(self, au: bytes, idr: bool):
        self.frames += 1
        if self.intra == 1:
            # všechny snímky úplné: displej vždy dostane ten nejnovější, starší se zahodí bez rozbití obrazu
            while not self.queue.empty():
                self.queue.get_nowait()
                self.dropped += 1
            self.queue.put_nowait((time.monotonic(), au))
            return
        if self.wait_idr and not idr:
            self.dropped += 1
            return
        self.wait_idr = False
        if self.queue.full():
            # zpoždění roste -> zahodit staré snímky a počkat na další IDR
            while not self.queue.empty():
                self.queue.get_nowait()
                self.dropped += 1
            if not idr:
                self.wait_idr = True
                self.dropped += 1
                return
        self.queue.put_nowait((time.monotonic(), au))

    async def _reader(self):
        buf = bytearray()
        cur, cur_vcl, cur_idr = [], False, False
        while True:
            chunk = await self.proc.stdout.read(65536)
            if not chunk:
                print("[camera] rpicam-vid skončil", flush=True)
                return
            buf += chunk
            starts = []
            i = buf.find(b"\x00\x00\x01")
            while i != -1:
                s = i - 1 if i > 0 and buf[i - 1] == 0 else i
                starts.append((s, i + 3))
                i = buf.find(b"\x00\x00\x01", i + 3)
            if len(starts) < 2:
                continue
            for (s, p), (s2, _) in zip(starts, starts[1:]):
                nal = bytes(buf[p:s2])
                if not nal:
                    continue
                t = nal[0] & 0x1F
                first_slice = t in (1, 5) and len(nal) > 1 and (nal[1] & 0x80)
                if cur_vcl and (t in (7, 8, 9) or first_slice):
                    self._push(b"".join(cur), cur_idr)
                    cur, cur_vcl, cur_idr = [], False, False
                cur.append(b"\x00\x00\x00\x01" + nal)
                if t in (1, 5):
                    cur_vcl = True
                    cur_idr = cur_idr or t == 5
            del buf[:starts[-1][0]]

    def flush(self):
        while not self.queue.empty():
            self.queue.get_nowait()
        self.wait_idr = True

    async def next_frame(self, timeout: float = 0.25):
        try:
            t, au = await asyncio.wait_for(self.queue.get(), timeout)
        except asyncio.TimeoutError:
            return None
        age = time.monotonic() - t
        self.last_age = age
        self.last_size = len(au)
        self.age_sum += age
        self.age_n += 1
        self.age_max = max(self.age_max, age)
        return au


class PicamCamera(LiveCamera):
    """Kamera přes picamera2: enkodér předá každý snímek celý hned po zakódování,
    takže se nečeká na začátek dalšího snímku jako u rpicam-vid (-1 snímek zpoždění)."""

    def __init__(self, intra: int = 10, bitrate: int = 4000000, queue: int = 2, sensor: str = ""):
        super().__init__("", intra, bitrate, queue)
        self.sensor = sensor
        self.picam = None

    async def start(self, width: int, height: int, fps: int):
        if self.picam:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._start_blocking, width, height, fps, loop)

    def _start_blocking(self, width, height, fps, loop):
        from picamera2 import Picamera2
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import Output

        cam = self

        class _Out(Output):
            def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
                loop.call_soon_threadsafe(cam._push, bytes(frame), bool(keyframe))

        self.picam = Picamera2()
        kw = {"main": {"size": (width, height), "format": "YUV420"},
              "controls": {"FrameRate": fps}, "buffer_count": 4}
        if self.sensor:
            sw, sh = (int(x) for x in self.sensor.lower().split("x"))
            kw["sensor"] = {"output_size": (sw, sh)}
        self.picam.configure(self.picam.create_video_configuration(**kw))
        enc = H264Encoder(bitrate=self.bitrate, repeat=True, iperiod=self.intra, framerate=fps,
                          profile="baseline")
        self.picam.start_recording(enc, _Out())
        print(f"[camera] picamera2 {width}x{height} @{fps} fps, IDR/{self.intra}, {self.bitrate} b/s, "
              f"senzor {self.picam.camera_configuration().get('sensor')}", flush=True)


class SbsCamera(LiveCamera):
    """Dvě kamery vedle sebe (CSI vlevo, USB vpravo) přes GStreamer, kvůli srovnání zpoždění.
    Každý buffer z appsink je jeden celý snímek H.264 (jen úplné snímky, posílá se nejnovější).
    Pozn.: hardwarový v4l2convert se s v4l2jpegdec + v4l2h264enc zasekává, zmenšení USB je proto softwarové."""

    CSI_NAME = "/base/soc/i2c0mux/i2c@1/ov5647@36"
    USB_STALL = 2.5   # s bez snímku z USB kamery -> znovu otevřít

    def __init__(self, bitrate: int = 8000000, usb_dev: str = "auto",
                 usb_mode: str = "1280x720", queue: int = 2):
        super().__init__("", 1, bitrate, queue)
        self.usb_dev, self.usb_mode = usb_dev, usb_mode
        self.pipeline = None
        self.params = None
        self.loop = None
        self.Gst = None
        self.usb_path = None     # zařízení použité v aktuální pipeline (None = USB chybí, černá výplň)
        self.usb_last = 0.0
        self.need_restart = False
        self.restarts = 0
        self.last_restart = ""
        self.usb_dec = "sw"      # "sw" = jpegdec (nižší zpoždění), "hw" = v4l2jpegdec
        self.usb_ctrl = ""       # v4l2-ctl nastavení kamery, např. "backlight_compensation=1,gamma=100"

    def _find_usb(self):
        """Stabilní cesta podle ID (po přepojení se /dev/videoN mění)."""
        import glob
        if self.usb_dev != "auto":
            return self.usb_dev if Path(self.usb_dev).exists() else None
        found = sorted(glob.glob("/dev/v4l/by-id/usb-*-video-index0"))
        return found[0] if found else None

    async def start(self, width: int, height: int, fps: int):
        if self.params:
            return
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        self.Gst, self.params, self.loop = Gst, (width, height, fps), asyncio.get_running_loop()
        self._build()
        self.loop.create_task(self._watchdog())

    def _enc_controls(self) -> str:
        return f"controls,video_bitrate={self.bitrate},h264_i_frame_period=1,repeat_sequence_header=1"

    def _encoder(self, width: int, height: int, fps: int) -> str:
        return (f"video/x-raw,width={width},height={height},framerate={fps}/1,format=I420,pixel-aspect-ratio=1/1 ! "
                f'v4l2h264enc name=enc extra-controls="{self._enc_controls()}" ! '
                f"video/x-h264,level=(string)4,profile=(string)baseline ! "
                f"h264parse ! video/x-h264,stream-format=byte-stream,alignment=au ! "
                f"appsink name=sink emit-signals=true sync=false max-buffers=2 drop=true ")

    def _desc(self, width: int, height: int, fps: int) -> str:
        hw, hh = (width // 2) & ~15, ((width // 2) * 9 // 16) & ~1     # polovina šířky, 16:9
        y = ((height - hh) // 2) & ~1
        uw, uh = (int(v) for v in self.usb_mode.lower().split("x"))
        self.usb_path = self._find_usb()
        if self.usb_path:
            usb = (f"v4l2src device={self.usb_path} ! image/jpeg,width={uw},height={uh},framerate=30/1 ! "
                   f"v4l2jpegdec ! videoconvert ! videoscale ! video/x-raw,width={hw},height={hh},format=I420 ! "
                   f"queue name=usbq leaky=downstream max-size-buffers=1 ! "
                   f'textoverlay text="USB RunCam" valignment=top halignment=left font-desc="Sans 14" ! mix.sink_1')
        else:
            usb = (f"videotestsrc is-live=true pattern=black ! "
                   f"video/x-raw,width={hw},height={hh},framerate={fps}/1,format=I420 ! "
                   f'textoverlay text="USB kamera odpojena" valignment=center halignment=center '
                   f'font-desc="Sans 16" ! mix.sink_1')
        print(f"[camera] SBS {width}x{height} @{fps}: CSI {hw}x{hh} | USB {self.usb_path or 'nenalezena'}"
              f" MJPEG {uw}x{uh} -> {hw}x{hh}", flush=True)
        return (
            f"compositor name=mix background=black sink_0::xpos=0 sink_0::ypos={y} "
            f"sink_1::xpos={width // 2} sink_1::ypos={y} ! "
            + self._encoder(width, height, fps)
            + f"libcamerasrc camera-name={self.CSI_NAME} ! "
            f"video/x-raw,width={hw},height={hh},framerate={fps}/1,format=NV12 ! "
            f"queue leaky=downstream max-size-buffers=1 ! "
            f'textoverlay text="CSI OV5647" valignment=top halignment=left font-desc="Sans 14" ! mix.sink_0 '
            + usb
        )

    def _build(self):
        Gst = self.Gst
        width, height, fps = self.params
        pipeline = Gst.parse_launch(self._desc(width, height, fps))
        loop = self.loop

        def on_sample(s):
            buf = s.emit("pull-sample").get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if ok:
                data = bytes(info.data)
                buf.unmap(info)
                loop.call_soon_threadsafe(self._push, data, True)
            return Gst.FlowReturn.OK

        pipeline.get_by_name("sink").connect("new-sample", on_sample)
        usbq = pipeline.get_by_name("usbq")
        if usbq:
            def on_usb_buffer(pad, info):
                self.usb_last = time.monotonic()
                return Gst.PadProbeReturn.OK
            usbq.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, on_usb_buffer)
        bus = pipeline.get_bus()

        def watch():  # bez GLib smyčky: chyby čteme vlastním vláknem
            while self.pipeline is pipeline:
                msg = bus.timed_pop_filtered(500 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.EOS)
                if not msg:
                    continue
                if msg.type == Gst.MessageType.ERROR:
                    err, _ = msg.parse_error()
                    print(f"[camera] GStreamer chyba ({msg.src.get_name()}): {err.message}", flush=True)
                else:
                    print("[camera] GStreamer EOS", flush=True)
                self.need_restart = True
                return

        self.need_restart = False
        self.usb_last = time.monotonic() + 3   # čas na rozjezd
        self.pipeline = pipeline
        import threading
        threading.Thread(target=watch, daemon=True).start()
        pipeline.set_state(Gst.State.PLAYING)
        if self.usb_path:
            # UVC kamery si nastavení po spuštění streamu někdy resetují -> nastavit až po rozjezdu
            self.loop.call_later(1.5, self._apply_ctrl, self.usb_path)

    CAMERA_CONF = Path(__file__).resolve().parent / "camera.conf"

    def _apply_ctrl(self, dev: str):
        """Nastavení uložené ze stavové stránky (camera.conf) má přednost před --usb-ctrl."""
        import subprocess
        ctrl, src = self.usb_ctrl, "--usb-ctrl"
        try:
            saved = self.CAMERA_CONF.read_text().strip()
            if saved:
                ctrl, src = saved, "camera.conf"
        except OSError:
            pass
        if not ctrl:
            return
        r = subprocess.run(["v4l2-ctl", "-d", dev, "-c", ctrl], capture_output=True, text=True)
        msg = r.stderr.strip() or "OK"
        print(f"[camera] nastavení kamery ({src}) {ctrl}: {msg}", flush=True)

    async def _watchdog(self):
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            reason = None
            if self.need_restart:
                reason = "chyba pipeline"
            elif self.usb_path and now - self.usb_last > self.USB_STALL:
                reason = f"USB kamera {self.USB_STALL:.1f} s bez snímku"
            elif not self.usb_path and self._find_usb():
                reason = "USB kamera znovu připojena"
            if not reason:
                continue
            self.restarts += 1
            self.last_restart = f"{time.strftime('%H:%M:%S')} {reason}"
            print(f"[camera] restart SBS #{self.restarts}: {reason}", flush=True)
            old, self.pipeline = self.pipeline, None
            if old:
                try:
                    await asyncio.wait_for(self.loop.run_in_executor(None, old.set_state, self.Gst.State.NULL), 5)
                except asyncio.TimeoutError:
                    # ovladač zaseknutý - ukončit proces, vmoto-auto.sh relaci rozjede znovu
                    print("[camera] pipeline nejde zastavit, restartuji logger", flush=True)
                    os._exit(3)
            await asyncio.sleep(1)
            try:
                self._build()
            except Exception as e:  # noqa: BLE001 - zkusí se znovu za sekundu
                print(f"[camera] start SBS selhal: {e}", flush=True)
                self.need_restart = True


class UsbCamera(SbsCamera):
    """Jen USB kamera (RunCam WebCam Nano, UVC MJPEG) přes celou obrazovku.
    MJPEG -> HW dekódování -> SW zmenšení se zachováním poměru stran -> HW H.264 (jen úplné snímky).
    Hlídání zaseknutí/přepojení a náhradní obraz "USB kamera odpojena" dědí ze SbsCamera.
    Digitální zoom/posun výřezu: videocrop se mění za běhu podle view (stavová stránka)."""

    VIEW_SAVED = Path(__file__).resolve().parent / "view.json"   # uloženo (Uložit na stránce)
    VIEW_LIVE = Path("/run/vmoto/view-live.json")                 # živé změny ze stránky (do restartu)

    def _load_view(self, path: Path):
        try:
            v = json.loads(path.read_text())
            return {"zoom": min(2.0, max(1.0, float(v.get("zoom", 1)))),
                    "pan_x": min(1.0, max(-1.0, float(v.get("pan_x", 0)))),
                    "pan_y": min(1.0, max(-1.0, float(v.get("pan_y", 0)))),
                    "mirror": bool(v.get("mirror", True)),
                    "bitrate_mbps": min(25.0, max(2.0, float(v.get("bitrate_mbps", self.bitrate / 1e6))))}
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _crop(self, uw: int, uh: int, width: int, height: int) -> tuple:
        """left, right, top, bottom: poměr displeje, zoom a posun výřezu (pan -1..1)."""
        v = getattr(self, "view", None) or {"zoom": 1.0, "pan_x": 0.0, "pan_y": 0.0}
        if uw * height > uh * width:
            bw, bh = uh * width // height, uh
        else:
            bw, bh = uw, uw * height // width
        w = max(64, int(bw / v["zoom"])) & ~1
        h = max(64, int(bh / v["zoom"])) & ~1
        left = int((uw - w) / 2 * (1 + v["pan_x"])) & ~1
        top = int((uh - h) / 2 * (1 + v["pan_y"])) & ~1
        return left, uw - w - left, top, uh - h - top

    def _desc(self, width: int, height: int, fps: int) -> str:
        uw, uh = (int(v) for v in self.usb_mode.lower().split("x"))
        if not hasattr(self, "view"):
            self.view = self._load_view(self.VIEW_LIVE) or self._load_view(self.VIEW_SAVED)
            self.view_mtime = 0.0
            self.loop.create_task(self._view_watch())
        if self.view:
            self.bitrate = int(self.view["bitrate_mbps"] * 1e6)
        l, r, t, b = self._crop(uw, uh, width, height)
        self.usb_path = self._find_usb()
        if self.usb_path:
            # SW jpegdec: HW v4l2jpegdec si drží snímek, naměřeno 80 -> 49 ms (zachycení -> zakódováno),
            # CPU ~1/3 jádra
            dec = "jpegdec" if self.usb_dec == "sw" else "v4l2jpegdec"
            # zrcadlení před výřezem, aby posun výřezu odpovídal tomu, co je vidět na displeji
            flip = "horiz" if (self.view or {}).get("mirror", True) else "identity"
            src = (f"v4l2src device={self.usb_path} ! image/jpeg,width={uw},height={uh},framerate=30/1 ! "
                   f"{dec} ! videoflip name=flip video-direction={flip} ! "
                   f"videocrop name=crop left={l} right={r} top={t} bottom={b} ! "
                   f"videoconvert ! videoscale ! "
                   f"queue name=usbq leaky=downstream max-size-buffers=1 ! ")
        else:
            src = (f"videotestsrc is-live=true pattern=black ! "
                   f'textoverlay text="USB kamera odpojena" valignment=center halignment=center '
                   f'font-desc="Sans 24" ! ')
        z = (self.view or {}).get("zoom", 1.0)
        mir = "zrcadleno" if (self.view or {}).get("mirror", True) else "nezrcadleno"
        print(f"[camera] USB {self.usb_path or 'nenalezena'} MJPEG {uw}x{uh}, výřez {uw - l - r}x{uh - t - b} "
              f"(zoom {z:.1f}x, {mir}) -> {width}x{height}", flush=True)
        return src + self._encoder(width, height, 30 if self.usb_path else fps)

    async def _view_watch(self):
        """Sleduje živé změny výřezu ze stránky a hned je promítne do videocrop."""
        while True:
            await asyncio.sleep(0.2)
            try:
                mt = self.VIEW_LIVE.stat().st_mtime
            except OSError:
                continue
            if mt == self.view_mtime:
                continue
            self.view_mtime = mt
            v = self._load_view(self.VIEW_LIVE)
            if not v or v == self.view:
                continue
            old, self.view = self.view or {}, v
            flip = self.pipeline.get_by_name("flip") if self.pipeline else None
            if flip and old.get("mirror", True) != v["mirror"]:
                flip.set_property("video-direction", 4 if v["mirror"] else 0)   # 4 = horiz, 0 = identity
                print(f"[camera] zrcadlení {'zapnuto' if v['mirror'] else 'vypnuto'}", flush=True)
            enc = self.pipeline.get_by_name("enc") if self.pipeline else None
            new_br = int(v["bitrate_mbps"] * 1e6)
            if enc and new_br != self.bitrate:
                self.bitrate = new_br
                # v4l2h264enc pošle extra-controls do běžícího enkodéru (bez restartu)
                enc.set_property("extra-controls", self.Gst.Structure.new_from_string(self._enc_controls()))
                print(f"[camera] datový tok {new_br / 1e6:.1f} Mbit/s", flush=True)
            crop = self.pipeline.get_by_name("crop") if self.pipeline else None
            if crop and self.params:
                uw, uh = (int(x) for x in self.usb_mode.lower().split("x"))
                l, r, t, b = self._crop(uw, uh, self.params[0], self.params[1])
                for k, val in (("left", l), ("right", r), ("top", t), ("bottom", b)):
                    crop.set_property(k, val)
                print(f"[camera] zoom {v['zoom']:.1f}x posun {v['pan_x']:+.2f}/{v['pan_y']:+.2f} "
                      f"-> výřez {uw - l - r}x{uh - t - b}", flush=True)


class Session:
    def __init__(self, outdir: Path):
        self.dir = outdir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events = open(self.dir / "events.jsonl", "a", encoding="utf-8")
        self.t0 = time.monotonic()
        self.conn_seq = 0
        self.ident = None
        self.capture = None      # poslední ReqConfigCapture od displeje
        self.aus = []            # snímky H.264 k odeslání
        self.au_idx = 0
        self.frames_sent = 0
        self.camera = None       # LiveCamera, pokud --camera
        self.p2c = None          # spojení P2C (telefon -> displej)
        self.ctrl_conns = 0      # aktivní spojení displeje na 10922
        self.ctrl_last = 0.0     # kdy naposledy skončilo spojení na 10922
        # telemetrie pro další nástroje (/run/vmoto/logger.json)
        self.started = time.time()
        self.polls_total = 0
        self.empty_total = 0
        self.bytes_sent = 0
        self.last_au = None
        self.dash_info = None    # CLIENT_INFO od displeje
        self.hu = None           # (ip, port, packagename, huname) z mDNS
        self.ec_status = ""      # výsledek posledního ohlášení na displej
        self.data_conns = 0      # aktivní spojení na 10920 (video)
        self.pending_sn = None   # požadavek 0x103E0 čekající na odpověď přes P2C

    def next_conn(self) -> int:
        self.conn_seq += 1
        return self.conn_seq

    def event(self, **kw):
        kw = {"t": round(time.monotonic() - self.t0, 4), **kw}
        self.events.write(json.dumps(kw, ensure_ascii=False) + "\n")
        self.events.flush()


def describe_payload(data: bytes) -> dict:
    if not data:
        return {}
    out = {}
    try:
        text = data.decode("utf-8")
        if text.strip() and all(c.isprintable() or c in "\r\n\t" for c in text):
            out["text"] = text
            try:
                out["json"] = json.loads(text)
            except ValueError:
                pass
            return out
    except UnicodeDecodeError:
        pass
    if data[:4] == b"\x00\x00\x00\x01" or data[:3] == b"\x00\x00\x01":
        out["hint"] = "H.264 Annex-B?"
    out["hex"] = data[:256].hex(" ")
    if len(data) > 256:
        out["hex_truncated"] = True
    return out


def short(info: dict) -> str:
    if "text" in info:
        s = info["text"].replace("\n", " ")
        return s[:200] + ("…" if len(s) > 200 else "")
    if "hex" in info:
        return info["hex"][:120] + ("…" if len(info["hex"]) > 120 else "")
    return ""


class LazyFile:
    """Soubor se vytvoří až při prvním zápisu (prázdná spojení na 54321 nedělají soubory)."""

    def __init__(self, path: str):
        self.path, self.f = path, None

    def write(self, data: bytes):
        if self.f is None:
            self.f = open(self.path, "ab")
        self.f.write(data)

    def flush(self):
        if self.f:
            self.f.flush()

    def close(self):
        if self.f:
            self.f.close()


class Conn:
    def __init__(self, sess: Session, args, port: int, reader, writer):
        self.sess, self.args, self.port = sess, args, port
        self.reader, self.writer = reader, writer
        peer = writer.get_extra_info("peername")
        self.peer = f"{peer[0]}:{peer[1]}" if peer else "?"
        self.id = sess.next_conn()
        base = sess.dir / f"conn{self.id}_{port}"
        self.rx = LazyFile(str(base) + ".rx")
        self.tx = LazyFile(str(base) + ".tx")
        self.buf = bytearray()
        self.raw_mode = False
        self.packets = 0
        self.conn_type = None
        self.hb_task = None
        self.pending_polls = 0   # žádosti 0x72 čekající na snímek z kamery
        self.polls = 0
        self.empty = 0
        self.stat_t = time.monotonic()
        self.partial_logged = False

    def log(self, msg: str):
        print(f"[{self.port} #{self.id} {self.peer}] {msg}", flush=True)

    def send(self, data: bytes, note: str):
        self.writer.write(data)
        self.tx.write(data)
        self.tx.flush()
        self.sess.event(port=self.port, conn=self.id, dir="tx", note=note, hex=data[:256].hex(" "), len=len(data))
        self.log(f"TX {note} ({len(data)} B)")

    def handle_packet(self, cmd: int, total: int, chk: int, res: int, payload: bytes):
        self.packets += 1
        info = describe_payload(payload)
        name = CONN_TYPES.get(cmd, "") if self.packets == 1 else ""
        self.sess.event(port=self.port, conn=self.id, dir="rx", seq=self.packets,
                        cmd=f"0x{cmd:08X}", len=total, res=f"0x{res:08X}", conn_type=name or None,
                        payload_len=len(payload), **info)
        if cmd not in (0x70000000, 0x70000001):  # heartbeaty jen do events.jsonl
            self.log(f"RX #{self.packets} cmd=0x{cmd:08X} {name} len={total} res=0x{res:08X} {short(info)}")

        is_ctrl = self.port == self.args.ctrl_port
        if self.packets == 1 and is_ctrl and not self.args.no_handshake:
            reply_cmd = (cmd + 1) & 0xFFFFFFFF
            ln = HDR.size + len(payload)
            self.send(HDR.pack(reply_cmd, ln, reply_cmd ^ ln, 0) + payload, f"ack cmd=0x{reply_cmd:08X}")
        elif self.packets == 1 and self.args.ack_first_all and not is_ctrl:
            reply_cmd = (cmd + 1) & 0xFFFFFFFF
            ln = HDR.size + len(payload)
            self.send(HDR.pack(reply_cmd, ln, reply_cmd ^ ln, 0) + payload, f"ack cmd=0x{reply_cmd:08X}")
        elif self.packets > 1 and is_ctrl and self.conn_type == 0x00010000:
            self.reply_c2p(cmd, info)
        if self.packets == 1:
            self.conn_type = cmd
            if is_ctrl and cmd == 0x00020000 and not self.args.no_handshake:
                self.sess.p2c = self
                self.hb_task = asyncio.get_running_loop().create_task(self.p2c_heartbeat())
                if self.args.foreground:
                    asyncio.get_running_loop().call_later(0.5, self.send_foreground)
        if is_ctrl and self.conn_type == 0x00010000 and cmd == 0x000103E0 and isinstance(info.get("json"), dict):
            self.sess.pending_sn = info["json"]

    def send_p2c(self, cmd: int, body: dict, note: str):
        data = json.dumps(body, separators=(",", ":")).encode()
        ln = HDR.size + len(data)
        self.send(HDR.pack(cmd, ln, cmd ^ ln, 0) + data, f"P2C {note} cmd=0x{cmd:08X}")
        self.sess.event(port=self.port, conn=self.id, dir="tx", cmd=f"0x{cmd:08X}", json=body)

    def send_foreground(self):
        cap = self.sess.capture or {}
        w, h = cap.get("width", 1024), cap.get("height", 592)
        self.send_p2c(0x00020020, {"displayRotation": 1, "width": max(w, h), "height": min(w, h),
                                   "enableAccessibility": False, "enableAOAHid": False},
                      "APPSTATUS_FOREGROUND")

    async def p2c_heartbeat(self):
        """Telefon na kanálu P2C posílá heartbeat 0x70000000 a čeká 0x70000001."""
        n = 0
        try:
            while not self.writer.is_closing():
                await asyncio.sleep(self.args.hb_interval)
                sn = self.sess.pending_sn
                if sn is not None:
                    # stejně jako telefon bez internetu: kontrolu SN nelze ověřit, pokračuje se
                    self.sess.pending_sn = None
                    self.send_p2c(0x000201C0, {"isOk": True, "errCode": -100, "errMsg": "Network Not Available",
                                               "id": sn.get("sn", ""), "client_set": sn.get("client_set", "")},
                                  "CHECK_SN reply")
                ln = HDR.size
                self.writer.write(HDR.pack(0x70000000, ln, 0x70000000 ^ ln, 0))
                self.tx.write(HDR.pack(0x70000000, ln, 0x70000000 ^ ln, 0))
                n += 1
                if n <= 2 or n % 20 == 0:
                    self.log(f"TX P2C heartbeat #{n}")
                await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass

    def reply_c2p(self, cmd: int, info: dict):
        """Odpovědi na požadavky displeje (kanál C2P): cmd+1 a JSON."""
        reply_cmd = (cmd + 1) & 0xFFFFFFFF
        if cmd == 0x70000000:
            ln = HDR.size
            self.writer.write(HDR.pack(reply_cmd, ln, reply_cmd ^ ln, 0))
            return
        if cmd == CLIENT_INFO_CMD and isinstance(info.get("json"), dict):
            self.sess.dash_info = info["json"]
        if cmd == CLIENT_INFO_CMD and isinstance(info.get("json"), dict) and self.sess.ident:
            body = client_info_reply(info["json"], self.sess.ident, self.args)
            note = "CLIENT_INFO reply"
        elif self.args.auto_reply:
            body = {}
            note = "auto-reply {}"
        else:
            return
        data = json.dumps(body, separators=(",", ":")).encode()
        ln = HDR.size + len(data)
        self.send(HDR.pack(reply_cmd, ln, reply_cmd ^ ln, 0) + data, f"{note} cmd=0x{reply_cmd:08X}")
        self.sess.event(port=self.port, conn=self.id, dir="tx", cmd=f"0x{reply_cmd:08X}", json=body)

    def media_send(self, cmd: int, body: bytes = b"", note: str = ""):
        self.send(MEDIA_HDR.pack(cmd, len(body), 0) + body, note or f"media 0x{cmd:04X}")

    def handle_media_ctrl(self, cmd: int, body: bytes):
        name = MEDIA_NAMES.get(cmd, "")
        ev = {"port": self.port, "conn": self.id, "dir": "rx", "media_cmd": f"0x{cmd:04X}", "name": name,
              "len": len(body)}
        if cmd == 0x10:
            cfg = parse_config_capture(body)
            self.sess.capture = cfg
            ev["config"] = cfg
            self.sess.event(**ev)
            self.log(f"RX {name} {cfg.get('width')}x{cfg.get('height')} enc={cfg.get('wantEncoder')} "
                     f"fps={cfg.get('wantFps')} br={cfg.get('bitRate')}")
            w, h = cfg["width"] & ~15, cfg["height"] & ~15
            self.media_send(0x11, struct.pack("<ihh", 2, w, h), f"RLY_RV_CONFIG_CAPTURE H.264 {w}x{h}")
            if self.sess.camera:
                fps = cfg.get("wantFps") or self.args.cam_fps
                asyncio.get_running_loop().create_task(self.sess.camera.start(w, h, fps))
            return
        ev.update(describe_payload(body))
        self.sess.event(**ev)
        self.log(f"RX media 0x{cmd:04X} {name} len={len(body)} {short(describe_payload(body))}")
        if cmd == 0x30:
            self.media_send(0x31, struct.pack("<ii", 5002, 1), "RLY_GET_VERSION")
        elif cmd == 0x40:
            self.media_send(0x41, b"", "RLY_HEARTBEAT")
        elif cmd == 0x60:
            self.media_send(0x61, b'{"supportFunction":0}', "RLY_CONFIGCAPTUREREXTEND")
        elif cmd == 0x70:
            self.media_send(0x71, b"", "RLY_RV_DATA_START")
        elif cmd == 0x110:
            self.media_send(0x111, b"OK\x00", "RLY_EXECUTE_SHELL")
        elif cmd == 0x20:
            pass  # dotyk / tlačítka - jen logujeme
        else:
            self.media_send(0x7F02, b"", "RLY_UN_SUPPORT")

    def handle_media_data(self, cmd: int, body: bytes):
        if cmd != 0x72:
            self.sess.event(port=self.port, conn=self.id, dir="rx", media_cmd=f"0x{cmd:04X}", hex=body[:64].hex(" "))
            self.log(f"RX data 0x{cmd:04X} (nečekané) len={len(body)}")
            self.media_send(0x7F02, b"", "RLY_UN_SUPPORT")
            return
        if self.sess.frames_sent < 3 or self.sess.frames_sent % 100 == 0:
            self.sess.event(port=self.port, conn=self.id, dir="rx", media_cmd="0x0072",
                            ext=body.hex(" "), frame=self.sess.frames_sent)
            self.log(f"RX REQ_RV_DATA_NEXT ext={body.hex(' ')} (snímek {self.sess.frames_sent})")
        if self.sess.camera:
            self.pending_polls += 1
            return
        aus = self.sess.aus
        if not aus:
            self.writer.write(b"\x00\x00\x00\x00")
            return
        au = aus[self.sess.au_idx % len(aus)]
        self.sess.au_idx += 1
        self.sess.frames_sent += 1
        self.writer.write(struct.pack("<I", len(au)) + au)

    def parse_media(self):
        while len(self.buf) >= MEDIA_HDR.size:
            cmd, ln, token = MEDIA_HDR.unpack_from(self.buf)
            if len(self.buf) < MEDIA_HDR.size + ln:
                return
            body = bytes(self.buf[MEDIA_HDR.size:MEDIA_HDR.size + ln])
            del self.buf[:MEDIA_HDR.size + ln]
            self.packets += 1
            if self.port == MEDIA_CTRL_PORT:
                self.handle_media_ctrl(cmd, body)
            else:
                self.handle_media_data(cmd, body)

    def parse(self):
        if self.port in (MEDIA_CTRL_PORT, MEDIA_DATA_PORT) and not self.args.media_raw:
            self.parse_media()
            return
        while not self.raw_mode and len(self.buf) >= HDR.size:
            cmd, total, chk, res = HDR.unpack_from(self.buf)
            if (cmd ^ total) != chk or total < HDR.size or total > MAX_LEN:
                self.raw_mode = True
                self.sess.event(port=self.port, conn=self.id, dir="rx", note="neplatná EasyConn hlavička, přepínám na raw",
                                hex=bytes(self.buf[:256]).hex(" "))
                self.log(f"neplatná hlavička -> raw režim: {bytes(self.buf[:48]).hex(' ')}")
                break
            if len(self.buf) < total:
                return
            payload = bytes(self.buf[HDR.size:total])
            del self.buf[:total]
            self.partial_logged = False
            self.handle_packet(cmd, total, chk, res, payload)
        if self.raw_mode and self.buf:
            self.sess.event(port=self.port, conn=self.id, dir="rx", raw=True, len=len(self.buf),
                            hex=bytes(self.buf[:256]).hex(" "))
            self.log(f"RAW {len(self.buf)} B: {bytes(self.buf[:48]).hex(' ')}")
            self.buf.clear()

    async def run(self):
        quiet = self.port == 54321  # displej sem jen "ťukne" každé ~4 s
        if not quiet:
            self.log("připojeno")
        self.sess.event(port=self.port, conn=self.id, event="connect", peer=self.peer)
        is_ctrl = self.port == self.args.ctrl_port
        if is_ctrl:
            self.sess.ctrl_conns += 1
        if self.port == MEDIA_DATA_PORT:
            self.sess.data_conns += 1
            if self.sess.camera:
                self.sess.camera.flush()  # nové spojení začne od čerstvého IDR snímku
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(self.reader.read(65536), timeout=1.0)
                except asyncio.TimeoutError:
                    if self.buf and not self.partial_logged:
                        # např. 8 B na 10920 - kratší než hlavička, displej čeká na nás
                        self.partial_logged = True
                        self.sess.event(port=self.port, conn=self.id, dir="rx", note="nekompletní data, displej čeká",
                                        len=len(self.buf), hex=bytes(self.buf).hex(" "))
                        self.log(f"čeká se, v bufferu {len(self.buf)} B: {bytes(self.buf[:48]).hex(' ')}")
                    continue
                if not chunk:
                    break
                if self.port != MEDIA_DATA_PORT:
                    self.rx.write(chunk)
                    self.rx.flush()
                self.buf += chunk
                self.parse()
                cam = self.sess.camera
                while cam and self.pending_polls:
                    self.pending_polls -= 1
                    self.polls += 1
                    au = await cam.next_frame()
                    self.sess.polls_total += 1
                    if au is None:
                        self.empty += 1
                        self.sess.empty_total += 1
                        self.writer.write(b"\x00\x00\x00\x00")
                    else:
                        self.sess.frames_sent += 1
                        self.sess.bytes_sent += len(au)
                        self.sess.last_au = au
                        self.writer.write(struct.pack("<I", len(au)) + au)
                    now = time.monotonic()
                    if now - self.stat_t >= 10:
                        dt_ = now - self.stat_t
                        self.log(f"kamera 10s: žádostí {self.polls / dt_:.1f}/s, odesláno celkem {self.sess.frames_sent}, "
                                 f"z kamery {cam.frames}, zahozeno {cam.dropped}, prázdných {self.empty}, "
                                 f"fronta {cam.queue.qsize()}, {cam.age_stats()}")
                        self.sess.event(port=self.port, conn=self.id, stat="camera", polls_per_s=round(self.polls / dt_, 1),
                                        sent=self.sess.frames_sent, cam_frames=cam.frames, dropped=cam.dropped,
                                        empty=self.empty, queue=cam.queue.qsize())
                        self.stat_t, self.polls, self.empty = now, 0, 0
                await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError) as e:
            self.log(f"chyba spojení: {e}")
        finally:
            if self.hb_task:
                self.hb_task.cancel()
            if is_ctrl:
                self.sess.ctrl_conns -= 1
                self.sess.ctrl_last = time.monotonic()
            if self.port == MEDIA_DATA_PORT:
                self.sess.data_conns -= 1
            if self.sess.p2c is self:
                self.sess.p2c = None
            if not quiet:
                self.log(f"odpojeno (paketů: {self.packets})")
            self.sess.event(port=self.port, conn=self.id, event="disconnect", packets=self.packets,
                            leftover_hex=bytes(self.buf[:256]).hex(" ") or None)
            self.rx.close()
            self.tx.close()
            self.writer.close()


class UdpLogger(asyncio.DatagramProtocol):
    def __init__(self, sess: Session, port: int):
        self.sess, self.port = sess, port

    def datagram_received(self, data, addr):
        info = describe_payload(data)
        self.sess.event(port=self.port, proto="udp", dir="rx", peer=f"{addr[0]}:{addr[1]}", len=len(data), **info)
        print(f"[udp {self.port} {addr[0]}:{addr[1]}] {len(data)} B {short(info)}", flush=True)


EC_INIT_CMD = 0x70000010


async def ec_init(sess: Session, host: str, port: int, package: str, attempts: int = 10):
    """Úvod jako telefon: připojí se na port displeje (10930, z mDNS) a ohlásí se.
    Displej odpoví cmd+1 s {"status":"true"} a potom se sám připojí na naše porty."""
    payload = json.dumps({"phoneType": "Android", "packageName": package}, separators=(",", ":")).encode()
    ln = HDR.size + len(payload)
    pkt = HDR.pack(EC_INIT_CMD, ln, EC_INIT_CMD ^ ln, 0) + payload
    for n in range(1, attempts + 1):
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=3)
        except (OSError, asyncio.TimeoutError) as e:
            print(f"[ec-init] {host}:{port} pokus {n}: {e}", flush=True)
            await asyncio.sleep(2)
            continue
        w.write(pkt)
        await w.drain()
        sess.event(port=port, proto="ec-init", dir="tx", cmd=f"0x{EC_INIT_CMD:08X}", text=payload.decode())
        print(f"[ec-init] TX cmd=0x{EC_INIT_CMD:08X} {payload.decode()}", flush=True)
        try:
            hdr = await asyncio.wait_for(r.readexactly(HDR.size), timeout=5)
            cmd, total, chk, res = HDR.unpack(hdr)
            body = await asyncio.wait_for(r.readexactly(max(0, total - HDR.size)), timeout=5) if total > HDR.size else b""
            info = describe_payload(body)
            sess.event(port=port, proto="ec-init", dir="rx", cmd=f"0x{cmd:08X}", len=total,
                       chk_ok=(cmd ^ total) == chk, **info)
            print(f"[ec-init] RX cmd=0x{cmd:08X} len={total} {short(info)}", flush=True)
            w.close()
            return True
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError) as e:
            print(f"[ec-init] bez odpovědi: {e}", flush=True)
            sess.event(port=port, proto="ec-init", note=f"bez odpovědi: {e}")
            w.close()
            await asyncio.sleep(2)
    return False


def discover_hu(timeout: float = 3.0):
    """Najde displej přes mDNS (_EasyConn._tcp): vrátí (ip, port, packagename) nebo None.
    Displeje různých značek mohou mít jinou IP/port a očekávat jinou aplikaci."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return None
    found = {}

    class _L:
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, 1500)
            if info and info.parsed_addresses() and "hu" not in found:
                props = {k.decode(errors="replace"): (v or b"").decode(errors="replace")
                         for k, v in (info.properties or {}).items()}
                ip = props.get("ip") or info.parsed_addresses()[0]
                found["hu"] = (ip, info.port or 10930, props.get("packagename") or "", props.get("huname") or name)

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, "_EasyConn._tcp.local.", _L())
        t_end = time.monotonic() + timeout
        while time.monotonic() < t_end and "hu" not in found:
            time.sleep(0.1)
    finally:
        zc.close()
    return found.get("hu")


async def ec_init_loop(sess: Session, host: str, port: int, package: str, retry: float = 3.0,
                       use_mdns: bool = True):
    """Ohlašuje se znovu, kdykoli displej nemá otevřené řídicí spojení
    (start, vypnuté a znovu zapnuté zrcadlení na motorce). IP, port a aplikaci vezme z mDNS displeje."""
    loop = asyncio.get_running_loop()
    while True:
        if sess.ctrl_conns == 0 and time.monotonic() - sess.ctrl_last > retry:
            if use_mdns:
                hu = await loop.run_in_executor(None, discover_hu, 3.0)
                if hu:
                    ip, p, pkg, name = hu
                    if (ip, p, pkg or package) != (host, port, package):
                        print(f"[mdns] displej {name}: {ip}:{p}, aplikace {pkg or '(neuvedena)'}", flush=True)
                        sess.event(event="mdns", ip=ip, port=p, packagename=pkg, huname=name)
                    host, port, package = ip, p, pkg or package
                    sess.hu = hu
            ok = await ec_init(sess, host, port, package, attempts=1)
            sess.ec_status = f"{'OK' if ok else 'bez odpovědi'} {host}:{port} ({time.strftime('%H:%M:%S')})"
            sess.ctrl_last = time.monotonic()
        await asyncio.sleep(1)


STATUS_DIR = Path("/run/vmoto")


async def status_writer(sess: Session, args):
    """Každou sekundu zapíše stav do /run/vmoto/logger.json a poslední snímek do frame.h264
    (pro další nástroje a stránky; /run je v RAM, SD karta se zbytečně neopotřebovává)."""
    try:
        STATUS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    prev = (time.monotonic(), 0, 0, 0)
    last_frame_written = None
    while True:
        await asyncio.sleep(1)
        now = time.monotonic()
        dt_ = max(now - prev[0], 1e-3)
        polls_s = (sess.polls_total - prev[1]) / dt_
        fps = (sess.frames_sent - prev[2]) / dt_
        kbps = (sess.bytes_sent - prev[3]) * 8 / dt_ / 1000
        prev = (now, sess.polls_total, sess.frames_sent, sess.bytes_sent)
        cam = sess.camera
        di = sess.dash_info or {}
        st = {
            "time": time.time(),
            "logger_started": sess.started,
            "dash": {k: di.get(k) for k in ("HUName", "HUID", "sdkVersion", "pxcVersion", "version_name",
                                            "flavor", "channel", "productType", "mirrorMode", "transportType",
                                            "supportScreenTouch", "supportBTCall", "currentHUTime")} if di else None,
            "mdns": dict(zip(("ip", "port", "packagename", "huname"), sess.hu)) if sess.hu else None,
            "ec_status": sess.ec_status,
            "channels": {"ctrl": sess.ctrl_conns, "video": sess.data_conns, "p2c": sess.p2c is not None},
            "capture": sess.capture and {k: sess.capture.get(k) for k in ("width", "height", "wantFps", "bitRate")},
            "stream": {"polls_per_s": round(polls_s, 1), "fps": round(fps, 1), "kbps": round(kbps),
                       "frames_sent": sess.frames_sent, "empty": sess.empty_total,
                       "frame_kb": round(getattr(cam, "last_size", 0) / 1000, 1) if cam else None,
                       "frame_age_ms": round(getattr(cam, "last_age", 0) * 1000, 1) if cam else None,
                       "cam_frames": getattr(cam, "frames", None), "dropped": getattr(cam, "dropped", None),
                       "queue": cam.queue.qsize() if cam else None},
            "camera": cam and {"type": type(cam).__name__, "device": getattr(cam, "usb_path", None),
                               "mode": getattr(cam, "usb_mode", None), "decoder": getattr(cam, "usb_dec", None),
                               "controls": getattr(cam, "usb_ctrl", None), "restarts": getattr(cam, "restarts", 0),
                               "last_restart": getattr(cam, "last_restart", ""), "bitrate": cam.bitrate},
        }
        try:
            tmp = STATUS_DIR / "logger.json.tmp"
            tmp.write_text(json.dumps(st, ensure_ascii=False))
            tmp.replace(STATUS_DIR / "logger.json")
            if sess.last_au is not None and sess.last_au is not last_frame_written:
                last_frame_written = sess.last_au
                tmpf = STATUS_DIR / "frame.h264.tmp"
                tmpf.write_bytes(sess.last_au)
                tmpf.replace(STATUS_DIR / "frame.h264")
        except OSError:
            pass


def start_mdns(port: int):
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except ImportError:
        print("mDNS: chybí balík zeroconf (pip install zeroconf), přeskakuji", flush=True)
        return None
    addrs = []
    for fam, _, _, _, sa in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        addrs.append(socket.inet_aton(sa[0]))
    info = ServiceInfo("_EasyConn._tcp.local.", "Pi-Logger._EasyConn._tcp.local.",
                       addresses=addrs or None, port=port, properties={})
    zc = Zeroconf()
    zc.register_service(info)
    print(f"mDNS: inzeruji _EasyConn._tcp na portu {port}", flush=True)
    return zc


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--tcp", default="54321,10922,10921,10920",
                    help="TCP porty k poslechu (54321: displej se na něj připojuje hned po Wi-Fi Direct)")
    ap.add_argument("--ctrl-port", type=int, default=10922, help="řídicí port (PXC), na kterém se potvrzuje typ kanálu")
    ap.add_argument("--udp", default="10500,14500", help="UDP porty k poslechu (prázdné = žádné)")
    ap.add_argument("--outdir", default="logs")
    ap.add_argument("--no-handshake", action="store_true", help="na 10922 nic neposílat, jen poslouchat")
    ap.add_argument("--ack-first-all", action="store_true", help="odpovědět cmd+1 i na první paket na 10921/10920")
    ap.add_argument("--mdns", action="store_true", help="inzerovat _EasyConn._tcp přes mDNS")
    ap.add_argument("--ec-init", default="192.168.2.1:10930", metavar="HOST:PORT",
                    help="po startu se ohlásit displeji jako telefon (prázdné = neohlašovat)")
    ap.add_argument("--package", default="net.easyconn.easyride.wws",
                    help="packageName v ohlášení (pokud ho displej neuvede v mDNS)")
    ap.add_argument("--no-mdns-discover", action="store_true",
                    help="nehledat displej přes mDNS, použít jen --ec-init a --package")
    ap.add_argument("--phone-uuid", default="pi-logger-0001", help="phoneUUID v odpovědi na CLIENT_INFO")
    ap.add_argument("--no-client-info", action="store_true", help="neodpovídat na CLIENT_INFO")
    ap.add_argument("--auto-reply", action=argparse.BooleanOptionalAction, default=True,
                    help="na ostatní požadavky displeje odpovídat cmd+1 s {} (výchozí zapnuto)")
    ap.add_argument("--h264", help="Annex-B H.264 soubor (s AUD) posílaný do displeje dokola")
    ap.add_argument("--media-raw", action="store_true", help="porty 10920/10921 jen logovat, neodpovídat")
    ap.add_argument("--hb-interval", type=float, default=1.0, help="interval P2C heartbeatu v sekundách")
    ap.add_argument("--camera", action="store_true", help="živý obraz z kamery Pi (rpicam-vid) místo --h264")
    ap.add_argument("--cam-fps", type=int, default=25,
                    help="snímková frekvence kamery (s --cam-intra 1 si displej řekne o ~23/s)")
    ap.add_argument("--cam-intra", type=int, default=1,
                    help="úplný snímek (IDR) každých N snímků; 1 = jen úplné snímky, posílá se vždy nejnovější "
                         "(nejnižší zpoždění, ~200 ms naměřeno 27. 10. 2026)")
    ap.add_argument("--cam-bitrate", type=int, default=12000000,
                    help="datový tok kamery v b/s (12 Mbit/s: bez viditelných přechodů, ~50 kB/snímek)")
    ap.add_argument("--cam-args", default="", help="další argumenty pro rpicam-vid (např. '--hflip --vflip')")
    ap.add_argument("--cam-backend", choices=["usb", "picamera2", "rpicam", "sbs"], default="usb",
                    help="usb = USB kamera (RunCam, výchozí); picamera2/rpicam = CSI kamera; "
                         "sbs = CSI + USB vedle sebe (srovnání)")
    ap.add_argument("--usb-dev", default="auto",
                    help="USB kamera (auto = /dev/v4l/by-id/usb-*-video-index0, přežije přepojení)")
    ap.add_argument("--usb-mode", default="1280x720", help="MJPEG rozlišení USB kamery")
    ap.add_argument("--usb-dec", choices=["sw", "hw"], default="sw",
                    help="dekódování MJPEG: sw = jpegdec (o ~30 ms nižší zpoždění), hw = v4l2jpegdec")
    ap.add_argument("--usb-ctrl", default="backlight_compensation=1,gamma=100",
                    help="nastavení USB kamery (v4l2-ctl -c), použije se při každém spuštění kamery; "
                         "backlight_compensation = WDR")
    ap.add_argument("--cam-queue", type=int, default=4,
                    help="max. snímků ve frontě, pohltí krátký výpadek Wi-Fi (víc = zahodit a čekat na IDR)")
    ap.add_argument("--cam-sensor", default="", help="režim snímače, např. 1296x972 (rychlejší vyčítání)")
    ap.add_argument("--foreground", action=argparse.BooleanOptionalAction, default=True,
                    help="po otevření P2C poslat APPSTATUS_FOREGROUND (0x20020)")
    args = ap.parse_args()

    sess = Session(Path(args.outdir) / dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    print(f"logy: {sess.dir}", flush=True)
    if args.camera:
        if args.cam_backend == "usb":
            sess.camera = UsbCamera(bitrate=args.cam_bitrate, usb_dev=args.usb_dev, usb_mode=args.usb_mode,
                                    queue=args.cam_queue)
            sess.camera.usb_dec = args.usb_dec
            sess.camera.usb_ctrl = args.usb_ctrl
        elif args.cam_backend == "sbs":
            sess.camera = SbsCamera(bitrate=args.cam_bitrate, usb_dev=args.usb_dev, usb_mode=args.usb_mode,
                                    queue=args.cam_queue)
        elif args.cam_backend == "picamera2":
            sess.camera = PicamCamera(intra=args.cam_intra, bitrate=args.cam_bitrate, queue=args.cam_queue,
                                      sensor=args.cam_sensor)
        else:
            sess.camera = LiveCamera(args.cam_args, intra=args.cam_intra, bitrate=args.cam_bitrate,
                                     queue=args.cam_queue)
        print(f"zdroj obrazu: kamera ({args.cam_backend})", flush=True)
    elif args.h264:
        sess.aus = load_h264_aus(args.h264)
        print(f"H.264: {len(sess.aus)} snímků z {args.h264}", flush=True)
    if not args.no_client_info:
        try:
            sess.ident = PhoneIdentity()
            print(f"RSA klíč telefonu: {KEY_FILE}", flush=True)
        except ImportError:
            print("chybí python3-cryptography, na CLIENT_INFO se neodpoví", flush=True)

    servers = []
    for p in [int(x) for x in args.tcp.split(",") if x.strip()]:
        async def handler(r, w, port=p):
            await Conn(sess, args, port, r, w).run()
        servers.append(await asyncio.start_server(handler, args.bind, p, reuse_address=True))
        print(f"TCP {args.bind}:{p} poslouchá", flush=True)

    loop = asyncio.get_running_loop()
    for p in [int(x) for x in args.udp.split(",") if x.strip()]:
        await loop.create_datagram_endpoint(lambda port=p: UdpLogger(sess, port), local_addr=(args.bind, p),
                                            allow_broadcast=True)
        print(f"UDP {args.bind}:{p} poslouchá", flush=True)

    zc = start_mdns(args.ctrl_port) if args.mdns else None
    sess.event(event="start", args=vars(args))
    tasks = [s.serve_forever() for s in servers]
    tasks.append(status_writer(sess, args))
    if args.ec_init:
        host, _, port = args.ec_init.rpartition(":")
        tasks.append(ec_init_loop(sess, host, int(port), args.package, use_mdns=not args.no_mdns_discover))
    try:
        await asyncio.gather(*tasks)
    finally:
        if zc:
            zc.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("konec", file=sys.stderr)
