#!/usr/bin/env python3
"""Stavová stránka Pi pro ethernet (port 80). Jen standardní knihovna.

  /                    HTML stránka, obnovuje se sama
  /api/status          stav jako JSON (zdroj pro stránku, dá se číst i skriptem)
  /api/camera          GET: ovládání USB kamery (rozsahy, aktuální a uložené hodnoty)
                       POST {"name": hodnota, ...}: nastaví hned (naživo na displeji)
  /api/camera/save     POST: uloží aktuální hodnoty do camera.conf a výřez do view.json
                       (logger je použije při každém startu kamery)
  /api/camera/defaults POST: výchozí hodnoty kamery a výřezu (naživo, bez uložení)
  /api/camera/revert   POST: vrátí uložené hodnoty (naživo)
  /api/view            POST {"zoom": 1-2, "pan_x": -1..1, "pan_y": -1..1, "mirror": true/false,
                       "bitrate_mbps": 2-25}: zoom, posun výřezu, zrcadlení a datový tok naživo
"""
import glob
import json
import os
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = os.environ.get("WEB_BIND", "192.168.1.10")
PORT = int(os.environ.get("WEB_PORT", "80"))
LOG = "/var/log/vmoto-auto.log"
DIR = os.environ.get("VMOTO_DIR", os.path.dirname(os.path.abspath(__file__)))
CAMERA_CONF = f"{DIR}/camera.conf"
VIEW_SAVED = f"{DIR}/view.json"            # digitální zoom/posun, uložené (Uložit)
# živé změny, logger je hned promítne; /run/vmoto vytváří služba (RuntimeDirectory), /dev/shm nejde:
# systemd (RemoveIPC) tam maže soubory uživatele pi po odhlášení poslední relace
VIEW_LIVE = "/run/vmoto/view-live.json"
VIEW_DEFAULT = {"zoom": 1.0, "pan_x": 0.0, "pan_y": 0.0, "mirror": True,   # kamera jako zpětné zrcátko
                "bitrate_mbps": 12.0}
# ovládání kamery, které stránka nabízí (pořadí = pořadí na stránce)
CAMERA_CTRLS = ["backlight_compensation", "brightness", "contrast", "saturation", "gamma", "sharpness", "hue",
                "white_balance_automatic", "white_balance_temperature", "auto_exposure",
                "exposure_time_absolute", "power_line_frequency"]
# opakující se řádky, které do přehledu logu nepatří
NOISE = re.compile(r"HEARTBEAT|heartbeat|REQ_RV_DATA_NEXT|kamera 10s|\[54321|^\s*$")


def sh(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def tail(path: str, n: int = 400, size: int = 120_000) -> list:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - size))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


def camera_dev():
    found = sorted(glob.glob("/dev/v4l/by-id/usb-*-video-index0"))
    return found[0] if found else None


def camera_ctrls(dev: str) -> list:
    """Rozparsuje `v4l2-ctl --list-ctrls-menus` na seznam ovládacích prvků."""
    out, ctrls = sh("v4l2-ctl", "-d", dev, "--list-ctrls-menus"), {}
    cur = None
    for line in out.splitlines():
        m = re.match(r"^\s*(\w+) 0x[0-9a-f]+ \((\w+)\)\s*: (.*)$", line)
        if m:
            name, typ, rest = m.groups()
            kv = dict(re.findall(r"(\w+)=([^\s]+)", rest))
            cur = {"name": name, "type": typ, "flags": kv.get("flags", ""), "options": []}
            for k in ("min", "max", "step", "default", "value"):
                if k in kv:
                    cur[k] = int(kv[k])
            ctrls[name] = cur
            continue
        m = re.match(r"^\s+(\d+): (.+)$", line)
        if m and cur and cur["type"] == "menu":
            cur["options"].append([int(m.group(1)), m.group(2).strip()])
    return [ctrls[n] for n in CAMERA_CTRLS if n in ctrls]


def saved_ctrls() -> dict:
    try:
        with open(CAMERA_CONF) as f:
            return {k: int(v) for k, v in (p.split("=") for p in f.read().strip().split(",") if "=" in p)}
    except (OSError, ValueError):
        return {}


def set_ctrls(dev: str, values: dict) -> str:
    allowed = {c["name"]: c for c in camera_ctrls(dev)}
    parts = []
    for name, val in values.items():
        c = allowed.get(name)
        if not c:
            continue
        val = int(val)
        if "min" in c:
            val = max(c["min"], min(c["max"], val))
        parts.append(f"{name}={val}")
    if not parts:
        return "nic k nastavení"
    # automatika expozice/vyvážení bílé musí jít první, jinak se ruční hodnota nenastaví
    parts.sort(key=lambda p: 0 if p.startswith(("auto_exposure", "white_balance_automatic")) else 1)
    r = subprocess.run(["v4l2-ctl", "-d", dev, "-c", ",".join(parts)], capture_output=True, text=True, timeout=3)
    return r.stderr.strip() or "OK"


def read_view(path: str):
    try:
        with open(path) as f:
            v = json.load(f)
        out = {k: float(v.get(k, VIEW_DEFAULT[k])) for k in ("zoom", "pan_x", "pan_y", "bitrate_mbps")}
        out["mirror"] = bool(v.get("mirror", VIEW_DEFAULT["mirror"]))
        return out
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def write_view(path: str, v: dict):
    v = {"zoom": round(min(2.0, max(1.0, float(v.get("zoom", 1)))), 2),
         "pan_x": round(min(1.0, max(-1.0, float(v.get("pan_x", 0)))), 3),
         "pan_y": round(min(1.0, max(-1.0, float(v.get("pan_y", 0)))), 3),
         "mirror": bool(v.get("mirror", True)),
         "bitrate_mbps": round(min(25.0, max(2.0, float(v.get("bitrate_mbps", 12)))), 1)}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(v, f)
    os.replace(tmp, path)
    return v


_tx = {"t": 0.0, "bytes": 0, "mbps": None}


def tx_rate():
    """Skutečný datový tok do displeje (bajty odeslané na Wi-Fi Direct rozhraní mezi dotazy)."""
    ifs = glob.glob("/sys/class/net/p2p-wlan0-*/statistics/tx_bytes")
    if not ifs:
        _tx.update(t=0.0, mbps=None)
        return None
    with open(ifs[0]) as f:
        b = int(f.read())
    now = time.monotonic()
    if _tx["t"] and now - _tx["t"] >= 1 and b >= _tx["bytes"]:
        _tx["mbps"] = round((b - _tx["bytes"]) * 8 / (now - _tx["t"]) / 1e6, 1)
    if not _tx["t"] or now - _tx["t"] >= 1:
        _tx.update(t=now, bytes=b)
    return _tx["mbps"]


def views() -> dict:
    saved = read_view(VIEW_SAVED)
    return {"live": read_view(VIEW_LIVE) or saved or dict(VIEW_DEFAULT), "saved": saved}


def last(lines: list, pattern: str):
    rx = re.compile(pattern)
    for line in reversed(lines):
        m = rx.search(line)
        if m:
            return m
    return None


def status() -> dict:
    lines = tail(LOG)
    # řádky o připojení bývají daleko zpátky (přenos loguje každých 10 s), proto větší kus logu
    big = tail(LOG, 100_000, 3_000_000)
    auto = [l for l in big if "[auto]" in l]
    state = "neznámý"
    if auto:
        a = auto[-1]
        if "připojeno na" in a or "displej na" in a:
            state = "připojeno k displeji"
        elif "hledám" in a or "relace ukončena" in a:
            state = "hledá displej"
        elif "nepovedlo" in a:
            state = "připojení selhalo, zkouší znovu"
        elif "konec služby" in a:
            state = "služba zastavena"
    if os.path.exists(f"{DIR}/DISABLE"):
        state = "pozastaveno (DISABLE)"

    dash = last(auto, r"displej (\S+) \(([0-9a-f:]{17})\), signál (-?\d+) dBm, kanál (\S+) MHz")
    cam = last(lines, r"kamera 10s: žádostí ([\d.]+)/s, odesláno celkem (\d+), z kamery (\d+), zahozeno (\d+), "
                      r"prázdných (\d+), fronta (\d+)(?:, stáří snímku průměr (\d+) ms, max (\d+) ms)?")
    cam_src = last(big, r"\[camera\] (USB .*|SBS .*|picamera2 .*)")
    # restarty kamery jen v aktuální relaci (od posledního připojení k displeji)
    since = max((i for i, l in enumerate(big) if "připojeno na" in l), default=0)
    restarts = sum(1 for l in big[since:] if "[camera] restart" in l)

    temp = sh("vcgencmd", "measure_temp").replace("temp=", "")
    thr = sh("vcgencmd", "get_throttled").replace("throttled=", "")
    try:
        thr_i = int(thr, 16)
    except ValueError:
        thr_i = 0
    load = os.getloadavg()
    with open("/proc/uptime") as f:
        up = float(f.read().split()[0])

    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "service": sh("systemctl", "is-active", "vmoto-auto"),
        "state": state,
        "dash": {"name": dash.group(1), "mac": dash.group(2), "signal_dbm": int(dash.group(3)),
                 "freq_mhz": dash.group(4)} if dash else None,
        "stream": {"polls_per_s": float(cam.group(1)), "sent": int(cam.group(2)), "from_camera": int(cam.group(3)),
                   "dropped": int(cam.group(4)), "empty": int(cam.group(5)), "queue": int(cam.group(6)),
                   "age_avg_ms": int(cam.group(7)) if cam.group(7) else None,
                   "age_max_ms": int(cam.group(8)) if cam.group(8) else None} if cam else None,
        "camera": {"source": cam_src.group(1) if cam_src else None, "restarts_in_log": restarts},
        "tx_mbps": tx_rate(),
        "pi": {"temp": temp, "throttled": thr, "undervoltage_now": bool(thr_i & 0x1),
               "undervoltage_since_boot": bool(thr_i & 0x10000), "load": [round(x, 2) for x in load],
               "uptime_min": int(up // 60)},
        "log": [l[:200] for l in big if not NOISE.search(l)][-25:],
    }


PAGE = """<!doctype html>
<html lang="cs"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vmoto Pi</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#6b7385;--ok:#1a8f4c;--bad:#c0392b;--line:#e3e6ec}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2128;--fg:#e6e9ef;--mut:#98a0b0;--line:#2c313a}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:4px 0 12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);margin:0 0 8px}
.big{font-size:20px;font-weight:600}.mut{color:var(--mut)}.ok{color:var(--ok)}.bad{color:var(--bad)}
dl{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;margin:0}dt{color:var(--mut)}dd{margin:0}
pre{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;overflow-x:auto;font-size:12px;margin-top:12px}
#cam{margin-top:12px}
.ctl{display:grid;grid-template-columns:150px 1fr 56px;gap:10px;align-items:center;padding:4px 0}
.ctl label{color:var(--mut)}.ctl input[type=range]{width:100%}.ctl .v{text-align:right;font-variant-numeric:tabular-nums}
.ctl.off{opacity:.45}.saved{font-size:11px;color:var(--mut)}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center}
button{font:inherit;padding:6px 14px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer}
button.primary{background:var(--ok);border-color:var(--ok);color:#fff}
@media (max-width:520px){.ctl{grid-template-columns:110px 1fr 44px}}
</style></head><body><main>
<h1>Vmoto Pi – stav</h1>
<div class="grid">
 <div class="card"><h2>Stav</h2><div id="state" class="big">…</div><div id="svc" class="mut"></div></div>
 <div class="card"><h2>Displej</h2><dl id="dash"></dl></div>
 <div class="card"><h2>Přenos</h2><dl id="stream"></dl></div>
 <div class="card"><h2>Pi</h2><dl id="pi"></dl></div>
</div>
<div class="card" id="cam"><h2>Kamera – změny se projeví hned na displeji</h2>
 <div id="view"></div>
 <div id="ctls" class="mut">načítám…</div>
 <div class="btns"><button class="primary" id="save">Uložit</button><button id="defs">Výchozí hodnoty</button>
  <button id="revert">Vrátit uložené</button><span id="cammsg" class="mut"></span></div>
</div>
<pre id="log"></pre>
<p class="mut" id="ts"></p>
</main><script>
const dl=(el,rows)=>{document.getElementById(el).innerHTML=rows.map(([k,v])=>`<dt>${k}</dt><dd>${v??'–'}</dd>`).join('')};
async function tick(){
 try{
  const s=await (await fetch('/api/status',{cache:'no-store'})).json();
  const st=document.getElementById('state');st.textContent=s.state;
  st.className='big '+(s.state.startsWith('připojeno')?'ok':s.state.startsWith('hledá')?'':'bad');
  document.getElementById('svc').textContent='služba vmoto-auto: '+s.service;
  dl('dash',s.dash?[['název',s.dash.name],['MAC',s.dash.mac],['signál',s.dash.signal_dbm+' dBm'],['kanál',s.dash.freq_mhz+' MHz']]:[['','zatím nenalezen']]);
  const t=s.stream;dl('stream',t?[['displej bere',t.polls_per_s+' snímků/s'],
   ['datový tok',s.tx_mbps!=null?s.tx_mbps+' Mbit/s'+(t.polls_per_s?' (~'+Math.round(s.tx_mbps*1000/8/t.polls_per_s)+' kB/snímek)':''):null],
   ['odesláno',t.sent],['zahozeno',t.dropped],
   ['stáří snímku',t.age_avg_ms!=null?t.age_avg_ms+' ms (max '+t.age_max_ms+')':null],['kamera',s.camera.source],
   ['restarty kamery',s.camera.restarts_in_log]]:[['','zatím bez dat']]);
  const p=s.pi;dl('pi',[['teplota',p.temp],['zátěž',p.load.join(' / ')],['podpětí',p.undervoltage_now?'<span class=bad>ANO</span>':
   (p.undervoltage_since_boot?'<span class=bad>od startu ano</span>':'<span class=ok>ne</span>')],['běží',p.uptime_min+' min']]);
  document.getElementById('log').textContent=s.log.join('\\n');
  document.getElementById('ts').textContent='aktualizováno '+s.time;
 }catch(e){document.getElementById('ts').textContent='Pi neodpovídá: '+e}
}
tick();setInterval(tick,2000);

const NAMES={backlight_compensation:'WDR (protisvětlo)',brightness:'Jas',contrast:'Kontrast',saturation:'Sytost',
 gamma:'Gamma',sharpness:'Ostrost',hue:'Odstín',white_balance_automatic:'Auto vyvážení bílé',
 white_balance_temperature:'Teplota bílé (K)',auto_exposure:'Expozice',exposure_time_absolute:'Doba expozice (0,1 ms)',
 power_line_frequency:'Proti blikání světel'};
let cam=null,timers={};
const msg=t=>document.getElementById('cammsg').textContent=t;
function dirty(){if(!cam)return false;if(!Object.keys(cam.saved).length)return true;
 const v=cam.view,s=v.saved;
 if(!s||v.live.mirror!==s.mirror||['zoom','pan_x','pan_y','bitrate_mbps'].some(k=>Math.abs(v.live[k]-s[k])>1e-3))return true;
 return cam.ctrls.some(c=>c.flags.indexOf('inactive')<0&&c.name in cam.saved&&cam.saved[c.name]!==c.value)}
function renderView(d){
 const v=d.view.live,s=d.view.saved,el=document.getElementById('view');
 const row=(k,lbl,min,max,step,val,fmt)=>`<div class=ctl><label>${lbl}${s?`<div class=saved>uloženo ${fmt(s[k])}</div>`:''}</label>
  <input type=range data-v="${k}" min=${min} max=${max} step=${step} value=${val}><span class=v id="vv_${k}">${fmt(val)}</span></div>`;
 const pct=x=>Math.round(x*100)+' %';
 el.innerHTML=`<div class=ctl><label>Zrcadlit (zpětné zrcátko)${s?`<div class=saved>uloženo ${s.mirror?'ano':'ne'}</div>`:''}</label>
  <input type=checkbox id=mirror ${v.mirror?'checked':''}><span class=v>${v.mirror?'ano':'ne'}</span></div>`
  +row('bitrate_mbps','Datový tok (kvalita)',2,25,0.5,v.bitrate_mbps,x=>(+x).toFixed(1)+' Mb')
  +row('zoom','Digitální zoom',1,2,0.05,v.zoom,x=>(+x).toFixed(2)+'×')
  +row('pan_x','Výřez vodorovně',-1,1,0.02,v.pan_x,pct)+row('pan_y','Výřez svisle',-1,1,0.02,v.pan_y,pct);
 document.getElementById('mirror').addEventListener('change',e=>post('/api/view',{mirror:e.target.checked}));
 el.querySelectorAll('[data-v]').forEach(i=>i.addEventListener('input',()=>{
  const k=i.dataset.v,val=+i.value;
  document.getElementById('vv_'+k).textContent=k==='zoom'?val.toFixed(2)+'×':k==='bitrate_mbps'?val.toFixed(1)+' Mb':Math.round(val*100)+' %';
  clearTimeout(timers['v_'+k]);timers['v_'+k]=setTimeout(()=>post('/api/view',{[k]:val},false),80);
 }));
}
function renderCam(d){
 cam=d;renderView(d);const el=document.getElementById('ctls');
 if(!d.ok){el.textContent=d.msg;return}
 el.className='';
 el.innerHTML=d.ctrls.map(c=>{
  const off=c.flags.indexOf('inactive')>=0,lbl=NAMES[c.name]||c.name;
  const sv=(c.name in d.saved)?`<div class=saved>uloženo ${d.saved[c.name]}</div>`:'';
  let inp;
  if(c.type==='bool'||(c.type==='int'&&c.min===0&&c.max===1))
   inp=`<input type=checkbox data-n="${c.name}" ${c.value?'checked':''} ${off?'disabled':''}>`;
  else if(c.type==='menu')
   inp=`<select data-n="${c.name}" ${off?'disabled':''}>`+c.options.map(([v,t])=>`<option value=${v} ${v===c.value?'selected':''}>${t}</option>`).join('')+'</select>';
  else inp=`<input type=range data-n="${c.name}" min=${c.min} max=${c.max} step=${c.step||1} value=${c.value} ${off?'disabled':''}>`;
  return `<div class="ctl ${off?'off':''}"><label>${lbl}${sv}</label>${inp}<span class=v id="v_${c.name}">${c.value}</span></div>`;
 }).join('');
 el.querySelectorAll('[data-n]').forEach(i=>{
  const ev=i.type==='range'?'input':'change';
  i.addEventListener(ev,()=>{
   const n=i.dataset.n,v=i.type==='checkbox'?(i.checked?1:0):+i.value;
   document.getElementById('v_'+n).textContent=v;
   clearTimeout(timers[n]);
   timers[n]=setTimeout(()=>post('/api/camera',{[n]:v},n==='auto_exposure'||n==='white_balance_automatic'),120);
  });
 });
 msg((d.msg&&d.msg!=='OK'?d.msg+' · ':'')+(dirty()?'neuložené změny':'uloženo'));
}
async function post(url,body,rerender=true){
 try{const d=await (await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})})).json();
  if(rerender)renderCam(d);else{cam=d;msg((d.msg&&d.msg!=='OK'?d.msg+' · ':'')+(dirty()?'neuložené změny':'uloženo'))}}
 catch(e){msg('chyba: '+e)}
}
document.getElementById('save').onclick=()=>post('/api/camera/save');
document.getElementById('defs').onclick=()=>post('/api/camera/defaults');
document.getElementById('revert').onclick=()=>post('/api/camera/revert');
fetch('/api/camera',{cache:'no-store'}).then(r=>r.json()).then(renderCam).catch(e=>msg('chyba: '+e));
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _camera(self, msg: str = ""):
        dev = camera_dev()
        if not dev:
            return self._json(200, {"ok": False, "msg": "USB kamera není připojená", "ctrls": [], "saved": {},
                                    "view": views()})
        self._json(200, {"ok": True, "msg": msg, "dev": dev, "ctrls": camera_ctrls(dev), "saved": saved_ctrls(),
                         "view": views()})

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._json(200, status())
        elif self.path == "/api/camera":
            self._camera()
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            return self._json(400, {"ok": False, "msg": "neplatný JSON"})
        if self.path == "/api/view":
            write_view(VIEW_LIVE, {**views()["live"], **body})
            return self._camera("OK")
        dev = camera_dev()
        if not dev:
            return self._json(200, {"ok": False, "msg": "USB kamera není připojená", "ctrls": [], "saved": {},
                                    "view": views()})
        if self.path == "/api/camera":
            self._camera(set_ctrls(dev, body))
        elif self.path == "/api/camera/revert":
            write_view(VIEW_LIVE, views()["saved"] or VIEW_DEFAULT)
            self._camera(set_ctrls(dev, saved_ctrls()) if saved_ctrls() else "OK")
        elif self.path == "/api/camera/defaults":
            write_view(VIEW_LIVE, VIEW_DEFAULT)
            # ruční expozice/teplota bílé jsou při zapnuté automatice neaktivní -> nenastavovat
            manual = ("exposure_time_absolute", "white_balance_temperature")
            self._camera(set_ctrls(dev, {c["name"]: c["default"] for c in camera_ctrls(dev)
                                         if "default" in c and c["name"] not in manual}))
        elif self.path == "/api/camera/save":
            vals = {c["name"]: c["value"] for c in camera_ctrls(dev) if "value" in c and "inactive" not in c["flags"]}
            tmp = CAMERA_CONF + ".tmp"
            with open(tmp, "w") as f:
                f.write(",".join(f"{k}={v}" for k, v in vals.items()) + "\n")
            os.replace(tmp, CAMERA_CONF)
            write_view(VIEW_SAVED, views()["live"])
            self._camera("uloženo – použije se při každém spuštění kamery")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"web: http://{BIND}:{PORT}/", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
