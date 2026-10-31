#!/usr/bin/env python3
"""Status & settings page for the Pi on Ethernet (port 80). Standard library only.

  /                    HTML page, refreshes itself
  /api/status          status as JSON (used by the page, also usable from scripts)
  /api/camera          GET: USB camera controls (ranges, current and saved values)
                       POST {"name": value, ...}: applies immediately (live on the dash)
  /api/camera/save     POST: saves current values to camera.conf and the view to view.json
                       (logger.py applies them every time the camera starts)
  /api/camera/defaults POST: camera and view defaults (live, not saved)
  /api/camera/revert   POST: restores the saved values (live)
  /api/view            POST {"zoom": 1-2, "pan_x": -1..1, "pan_y": -1..1, "mirror": true/false,
                       "bitrate_mbps": 2-25}: zoom, pan, mirror and bitrate, live

Note: logger.py and vmoto-auto.sh write their log (/var/log/vmoto-auto.log) in Czech;
the regular expressions below match those Czech log lines.
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
VIEW_SAVED = f"{DIR}/view.json"            # digital zoom/pan/mirror/bitrate, saved (Save button)
# live changes, logger.py applies them immediately; /run/vmoto is created by the service (RuntimeDirectory).
# Not /dev/shm: systemd (RemoveIPC) deletes a user's files there when their last session logs out.
VIEW_LIVE = "/run/vmoto/view-live.json"
VIEW_DEFAULT = {"zoom": 1.0, "pan_x": 0.0, "pan_y": 0.0, "mirror": True,   # camera used as a rear-view mirror
                "bitrate_mbps": 12.0}
# camera controls offered on the page (order = order on the page)
CAMERA_CTRLS = ["backlight_compensation", "brightness", "contrast", "saturation", "gamma", "sharpness", "hue",
                "white_balance_automatic", "white_balance_temperature", "auto_exposure",
                "exposure_time_absolute", "power_line_frequency"]
# repetitive log lines that don't belong in the log overview
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
    """Parses `v4l2-ctl --list-ctrls-menus` into a list of controls."""
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
        return "nothing_to_set"
    # auto exposure / auto white balance must go first, otherwise a manual value is rejected
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
    """Actual bitrate to the dash (bytes sent on the Wi-Fi Direct interface between requests)."""
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
    # connection lines can be far back (streaming logs every 10 s), hence a bigger chunk of the log
    big = tail(LOG, 100_000, 3_000_000)
    auto = [l for l in big if "[auto]" in l]
    # state is a language-neutral key, the page translates it (EN/CS)
    state = "unknown"
    if auto:
        a = auto[-1]
        if "připojeno na" in a or "displej na" in a:
            state = "connected"
        elif "hledám" in a or "relace ukončena" in a:
            state = "searching"
        elif "nepovedlo" in a:
            state = "failed"
        elif "konec služby" in a:
            state = "stopped"
    if os.path.exists(f"{DIR}/DISABLE"):
        state = "paused"

    dash = last(auto, r"displej (\S+) \(([0-9a-f:]{17})\), signál (-?\d+) dBm, kanál (\S+) MHz")
    cam = last(lines, r"kamera 10s: žádostí ([\d.]+)/s, odesláno celkem (\d+), z kamery (\d+), zahozeno (\d+), "
                      r"prázdných (\d+), fronta (\d+)(?:, stáří snímku průměr (\d+) ms, max (\d+) ms)?")
    cam_src = last(big, r"\[camera\] (USB .*|SBS .*|picamera2 .*)")
    # camera restarts in the current session only (since the last connection to the dash)
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
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vmoto Pi</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#6b7385;--ok:#1a8f4c;--bad:#c0392b;--line:#e3e6ec}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2128;--fg:#e6e9ef;--mut:#98a0b0;--line:#2c313a}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:16px}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:4px 0 12px}
h1{font-size:20px;margin:0}
.lang{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.lang button{border:0;border-radius:0;padding:4px 12px;background:var(--card)}
.lang button.on{background:var(--ok);color:#fff}
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
<header><h1 data-t="title"></h1>
 <div class="lang"><button id="l_en" type="button">EN</button><button id="l_cs" type="button">CS</button></div></header>
<div class="grid">
 <div class="card"><h2 data-t="h_status"></h2><div id="state" class="big">…</div><div id="svc" class="mut"></div></div>
 <div class="card"><h2 data-t="h_dash"></h2><dl id="dash"></dl></div>
 <div class="card"><h2 data-t="h_stream"></h2><dl id="stream"></dl></div>
 <div class="card"><h2>Pi</h2><dl id="pi"></dl></div>
</div>
<div class="card" id="cam"><h2 data-t="h_camera"></h2>
 <div id="view"></div>
 <div id="ctls" class="mut" data-t="loading"></div>
 <div class="btns"><button class="primary" id="save" data-t="b_save"></button><button id="defs" data-t="b_defs"></button>
  <button id="revert" data-t="b_revert"></button><span id="cammsg" class="mut"></span></div>
</div>
<pre id="log"></pre>
<p class="mut" id="ts"></p>
</main><script>
const I18N={
 en:{title:'Vmoto Pi – status',h_status:'Status',h_dash:'Dash',h_stream:'Stream',h_camera:'Camera – changes apply live on the dash',
  loading:'loading…',b_save:'Save',b_defs:'Defaults',b_revert:'Restore saved',
  st_connected:'connected to dash',st_searching:'searching for dash',st_failed:'connection failed, retrying',
  st_stopped:'service stopped',st_paused:'paused (DISABLE)',st_unknown:'unknown',svc:'vmoto-auto service',
  name:'name',signal:'signal',channel:'channel',not_found:'not found yet',
  polls:'dash requests',fps:'frames/s',bitrate:'bitrate',kbframe:'kB/frame',sent:'sent',dropped:'dropped',
  age:'frame age',camera:'camera',restarts:'camera restarts',no_data:'no data yet',
  temp:'temperature',load:'load',uv:'under-voltage',yes_caps:'YES',since_boot:'since boot',no:'no',yes:'yes',uptime:'uptime',
  updated:'updated',no_resp:'Pi not responding',saved:'saved',unsaved:'unsaved changes',error:'error',
  mirror:'Mirror (rear-view)',br:'Bitrate (quality)',zoom:'Digital zoom',pan_x:'Pan horizontal',pan_y:'Pan vertical',
  no_camera:'USB camera not connected',invalid_json:'invalid JSON',nothing_to_set:'nothing to set',
  saved_note:'saved – applied every time the camera starts',
  c_backlight_compensation:'WDR (backlight)',c_brightness:'Brightness',c_contrast:'Contrast',c_saturation:'Saturation',
  c_gamma:'Gamma',c_sharpness:'Sharpness',c_hue:'Hue',c_white_balance_automatic:'Auto white balance',
  c_white_balance_temperature:'White balance (K)',c_auto_exposure:'Exposure',c_exposure_time_absolute:'Exposure time (0.1 ms)',
  c_power_line_frequency:'Anti-flicker'},
 cs:{title:'Vmoto Pi – stav',h_status:'Stav',h_dash:'Displej',h_stream:'Přenos',h_camera:'Kamera – změny se projeví hned na displeji',
  loading:'načítám…',b_save:'Uložit',b_defs:'Výchozí hodnoty',b_revert:'Vrátit uložené',
  st_connected:'připojeno k displeji',st_searching:'hledá displej',st_failed:'připojení selhalo, zkouší znovu',
  st_stopped:'služba zastavena',st_paused:'pozastaveno (DISABLE)',st_unknown:'neznámý',svc:'služba vmoto-auto',
  name:'název',signal:'signál',channel:'kanál',not_found:'zatím nenalezen',
  polls:'displej bere',fps:'snímků/s',bitrate:'datový tok',kbframe:'kB/snímek',sent:'odesláno',dropped:'zahozeno',
  age:'stáří snímku',camera:'kamera',restarts:'restarty kamery',no_data:'zatím bez dat',
  temp:'teplota',load:'zátěž',uv:'podpětí',yes_caps:'ANO',since_boot:'od startu ano',no:'ne',yes:'ano',uptime:'běží',
  updated:'aktualizováno',no_resp:'Pi neodpovídá',saved:'uloženo',unsaved:'neuložené změny',error:'chyba',
  mirror:'Zrcadlit (zpětné zrcátko)',br:'Datový tok (kvalita)',zoom:'Digitální zoom',pan_x:'Výřez vodorovně',pan_y:'Výřez svisle',
  no_camera:'USB kamera není připojená',invalid_json:'neplatný JSON',nothing_to_set:'nic k nastavení',
  saved_note:'uloženo – použije se při každém spuštění kamery',
  c_backlight_compensation:'WDR (protisvětlo)',c_brightness:'Jas',c_contrast:'Kontrast',c_saturation:'Sytost',
  c_gamma:'Gamma',c_sharpness:'Ostrost',c_hue:'Odstín',c_white_balance_automatic:'Auto vyvážení bílé',
  c_white_balance_temperature:'Teplota bílé (K)',c_auto_exposure:'Expozice',c_exposure_time_absolute:'Doba expozice (0,1 ms)',
  c_power_line_frequency:'Proti blikání světel'}};
let lang='en';
try{const s=localStorage.getItem('vmoto_lang');if(s==='cs'||s==='en')lang=s}catch(e){}
const T=k=>(I18N[lang][k]??I18N.en[k]??k);
const tm=m=>m&&m!=='OK'?T(m):'';   // server messages are keys; unknown text (v4l2-ctl errors) passes through
function setLang(l){
 lang=l;try{localStorage.setItem('vmoto_lang',l)}catch(e){}
 document.documentElement.lang=l;
 document.getElementById('l_en').className=l==='en'?'on':'';document.getElementById('l_cs').className=l==='cs'?'on':'';
 document.querySelectorAll('[data-t]').forEach(e=>e.textContent=T(e.dataset.t));
 if(cam)renderCam(cam);tick();
}
const dl=(el,rows)=>{document.getElementById(el).innerHTML=rows.map(([k,v])=>`<dt>${k}</dt><dd>${v??'–'}</dd>`).join('')};
async function tick(){
 try{
  const s=await (await fetch('/api/status',{cache:'no-store'})).json();
  const st=document.getElementById('state');st.textContent=T('st_'+s.state);
  st.className='big '+(s.state==='connected'?'ok':s.state==='searching'?'':'bad');
  document.getElementById('svc').textContent=T('svc')+': '+s.service;
  dl('dash',s.dash?[[T('name'),s.dash.name],['MAC',s.dash.mac],[T('signal'),s.dash.signal_dbm+' dBm'],[T('channel'),s.dash.freq_mhz+' MHz']]:[['',T('not_found')]]);
  const t=s.stream;dl('stream',t?[[T('polls'),t.polls_per_s+' '+T('fps')],
   [T('bitrate'),s.tx_mbps!=null?s.tx_mbps+' Mbit/s'+(t.polls_per_s?' (~'+Math.round(s.tx_mbps*1000/8/t.polls_per_s)+' '+T('kbframe')+')':''):null],
   [T('sent'),t.sent],[T('dropped'),t.dropped],
   [T('age'),t.age_avg_ms!=null?t.age_avg_ms+' ms (max '+t.age_max_ms+')':null],[T('camera'),s.camera.source],
   [T('restarts'),s.camera.restarts_in_log]]:[['',T('no_data')]]);
  const p=s.pi;dl('pi',[[T('temp'),p.temp],[T('load'),p.load.join(' / ')],[T('uv'),p.undervoltage_now?'<span class=bad>'+T('yes_caps')+'</span>':
   (p.undervoltage_since_boot?'<span class=bad>'+T('since_boot')+'</span>':'<span class=ok>'+T('no')+'</span>')],[T('uptime'),p.uptime_min+' min']]);
  document.getElementById('log').textContent=s.log.join('\\n');
  document.getElementById('ts').textContent=T('updated')+' '+s.time;
 }catch(e){document.getElementById('ts').textContent=T('no_resp')+': '+e}
}
let cam=null,timers={};
const msg=t=>document.getElementById('cammsg').textContent=t;
const camMsg=d=>msg((tm(d.msg)?tm(d.msg)+' · ':'')+(dirty()?T('unsaved'):T('saved')));
function dirty(){if(!cam)return false;if(!Object.keys(cam.saved).length)return true;
 const v=cam.view,s=v.saved;
 if(!s||v.live.mirror!==s.mirror||['zoom','pan_x','pan_y','bitrate_mbps'].some(k=>Math.abs(v.live[k]-s[k])>1e-3))return true;
 return cam.ctrls.some(c=>c.flags.indexOf('inactive')<0&&c.name in cam.saved&&cam.saved[c.name]!==c.value)}
function renderView(d){
 const v=d.view.live,s=d.view.saved,el=document.getElementById('view');
 const row=(k,lbl,min,max,step,val,fmt)=>`<div class=ctl><label>${lbl}${s?`<div class=saved>${T('saved')} ${fmt(s[k])}</div>`:''}</label>
  <input type=range data-v="${k}" min=${min} max=${max} step=${step} value=${val}><span class=v id="vv_${k}">${fmt(val)}</span></div>`;
 const pct=x=>Math.round(x*100)+' %';
 el.innerHTML=`<div class=ctl><label>${T('mirror')}${s?`<div class=saved>${T('saved')} ${s.mirror?T('yes'):T('no')}</div>`:''}</label>
  <input type=checkbox id=mirror ${v.mirror?'checked':''}><span class=v>${v.mirror?T('yes'):T('no')}</span></div>`
  +row('bitrate_mbps',T('br'),2,25,0.5,v.bitrate_mbps,x=>(+x).toFixed(1)+' Mb')
  +row('zoom',T('zoom'),1,2,0.05,v.zoom,x=>(+x).toFixed(2)+'×')
  +row('pan_x',T('pan_x'),-1,1,0.02,v.pan_x,pct)+row('pan_y',T('pan_y'),-1,1,0.02,v.pan_y,pct);
 document.getElementById('mirror').addEventListener('change',e=>post('/api/view',{mirror:e.target.checked}));
 el.querySelectorAll('[data-v]').forEach(i=>i.addEventListener('input',()=>{
  const k=i.dataset.v,val=+i.value;
  document.getElementById('vv_'+k).textContent=k==='zoom'?val.toFixed(2)+'×':k==='bitrate_mbps'?val.toFixed(1)+' Mb':Math.round(val*100)+' %';
  clearTimeout(timers['v_'+k]);timers['v_'+k]=setTimeout(()=>post('/api/view',{[k]:val},false),80);
 }));
}
function renderCam(d){
 cam=d;renderView(d);const el=document.getElementById('ctls');
 if(!d.ok){el.textContent=tm(d.msg);return}
 el.className='';
 el.innerHTML=d.ctrls.map(c=>{
  const off=c.flags.indexOf('inactive')>=0,lbl=T('c_'+c.name);
  const sv=(c.name in d.saved)?`<div class=saved>${T('saved')} ${d.saved[c.name]}</div>`:'';
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
 camMsg(d);
}
async function post(url,body,rerender=true){
 try{const d=await (await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})})).json();
  if(rerender)renderCam(d);else{cam=d;camMsg(d)}}
 catch(e){msg(T('error')+': '+e)}
}
document.getElementById('save').onclick=()=>post('/api/camera/save');
document.getElementById('defs').onclick=()=>post('/api/camera/defaults');
document.getElementById('revert').onclick=()=>post('/api/camera/revert');
document.getElementById('l_en').onclick=()=>setLang('en');
document.getElementById('l_cs').onclick=()=>setLang('cs');
setLang(lang);setInterval(tick,2000);
fetch('/api/camera',{cache:'no-store'}).then(r=>r.json()).then(renderCam).catch(e=>msg(T('error')+': '+e));
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
            return self._json(200, {"ok": False, "msg": "no_camera", "ctrls": [], "saved": {},
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
            return self._json(400, {"ok": False, "msg": "invalid_json"})
        if self.path == "/api/view":
            write_view(VIEW_LIVE, {**views()["live"], **body})
            return self._camera("OK")
        dev = camera_dev()
        if not dev:
            return self._json(200, {"ok": False, "msg": "no_camera", "ctrls": [], "saved": {},
                                    "view": views()})
        if self.path == "/api/camera":
            self._camera(set_ctrls(dev, body))
        elif self.path == "/api/camera/revert":
            write_view(VIEW_LIVE, views()["saved"] or VIEW_DEFAULT)
            self._camera(set_ctrls(dev, saved_ctrls()) if saved_ctrls() else "OK")
        elif self.path == "/api/camera/defaults":
            write_view(VIEW_LIVE, VIEW_DEFAULT)
            # manual exposure / white balance are inactive while auto is on -> don't set them
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
            self._camera("saved_note")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"web: http://{BIND}:{PORT}/", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
