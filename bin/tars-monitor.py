#!/usr/bin/env python3
"""TARS local monitor — a localhost-only dashboard for this machine (the GX10).

Tracks system memory, GPU/VRAM, load, and which Ollama models are spun up,
and charts them live. Stdlib only (no pip deps). Binds to 127.0.0.1 so it is
never exposed publicly.

Run:  python3 bin/tars-monitor.py [--port 8422]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OLLAMA_HOST = "http://localhost:11434"
CONTROLLER_HEALTH = "http://localhost:8420/api/health"
SAMPLE_INTERVAL = 3            # seconds between samples
HISTORY = deque(maxlen=600)    # ~30 min at 3s
_START = time.time()


# --------------------------------------------------------------------------- samplers
def _read_meminfo() -> dict:
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                out[k.strip()] = int(v.strip().split()[0])  # kB
    except OSError:
        pass
    return out


def _loadavg() -> float:
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])
    except OSError:
        return 0.0


def _nvidia_gpu():
    """Return (util_pct, temp_c, power_w). The GX10 GB10 has UNIFIED memory, so
    VRAM is reported as N/A by nvidia-smi — utilization/temp are the useful GPU
    signals; model memory comes from Ollama and total memory from RAM."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip().splitlines()
        if out:
            parts = [p.strip() for p in out[0].split(",")]
            util = int(float(parts[0])) if parts[0] not in ("[N/A]", "N/A") else None
            temp = int(float(parts[1])) if parts[1] not in ("[N/A]", "N/A") else None
            power = round(float(parts[2]), 1) if parts[2] not in ("[N/A]", "N/A") else None
            return util, temp, power
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    return None, None, None


def _ollama_ps() -> list:
    """Currently loaded (spun-up) models with their memory footprint."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/ps", timeout=4) as r:
            data = json.loads(r.read().decode())
        models = []
        for m in data.get("models", []):
            models.append({
                "name": m.get("name", "?"),
                "vram_gb": round(m.get("size_vram", 0) / 1e9, 2),
                "size_gb": round(m.get("size", 0) / 1e9, 2),
            })
        return models
    except Exception:
        return []


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def sample() -> dict:
    mem = _read_meminfo()
    total_kb = mem.get("MemTotal", 0)
    avail_kb = mem.get("MemAvailable", 0)
    gpu_util, gpu_temp, gpu_power = _nvidia_gpu()
    models = _ollama_ps()
    return {
        "t": int(time.time()),
        "ram_used_gb": round((total_kb - avail_kb) / 1024 / 1024, 2),
        "ram_total_gb": round(total_kb / 1024 / 1024, 1),
        "gpu_util": gpu_util,
        "gpu_temp": gpu_temp,
        "gpu_power": gpu_power,
        "load1": _loadavg(),
        "models": models,
        "model_vram_gb": round(sum(m["vram_gb"] for m in models), 2),
        "controller_ok": _http_ok(CONTROLLER_HEALTH),
    }


def _sampler_loop():
    while True:
        try:
            HISTORY.append(sample())
        except Exception:
            pass
        time.sleep(SAMPLE_INTERVAL)


# --------------------------------------------------------------------------- HTTP
PAGE = r"""<!doctype html><html><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>TARS Monitor — GX10</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{--bg:#0d0e10;--panel:#16181c;--border:#2a2e35;--text:#e6e7e9;--muted:#9aa0a8;--accent:#c96442;--good:#3fb950;--bad:#f85149}
  *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
  header{display:flex;align-items:center;gap:12px;padding:16px 22px;border-bottom:1px solid var(--border);background:var(--panel)}
  header h1{font-size:18px;margin:0;letter-spacing:2px}
  header .sub{color:var(--muted);font-size:12px}
  .dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:5px}
  .dot.on{background:var(--good)}.dot.off{background:var(--bad)}
  main{padding:22px;max-width:1200px;margin:0 auto}
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px;margin-bottom:22px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 18px}
  .card .n{font-size:26px;font-weight:700}.card .l{color:var(--muted);font-size:12px;margin-top:3px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  @media(max-width:760px){.grid{grid-template-columns:1fr}}
  .panel{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px 18px;margin-bottom:18px}
  .panel h3{margin:0 0 12px;font-size:14px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.5px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--border)}
  th{color:var(--muted);font-size:11px;text-transform:uppercase}
  td code{background:#1c1f24;padding:2px 7px;border-radius:5px}
  .muted{color:var(--muted)}
</style></head><body>
<header>
  <h1>TARS MONITOR</h1>
  <span class="sub">GX10 · localhost only · <span id="ctl"></span></span>
  <span style="flex:1"></span>
  <span class="sub" id="clock"></span>
</header>
<main>
  <div class="cards" id="cards"></div>
  <div class="panel"><h3>Spun-up models (Ollama)</h3><div id="models">…</div></div>
  <div class="grid">
    <div class="panel"><h3>System memory (GB)</h3><canvas id="ramChart" height="150"></canvas></div>
    <div class="panel"><h3>GPU utilization (%)</h3><canvas id="vramChart" height="150"></canvas></div>
    <div class="panel"><h3>Model memory loaded (GB)</h3><canvas id="modelChart" height="150"></canvas></div>
    <div class="panel"><h3>Load average (1-min)</h3><canvas id="loadChart" height="150"></canvas></div>
  </div>
</main>
<script>
const C={};
function mk(id,label,color,opts){
  const ctx=document.getElementById(id);
  return new Chart(ctx,{type:'line',data:{labels:[],datasets:[{label,data:[],borderColor:color,backgroundColor:color+'22',fill:true,tension:.25,pointRadius:0,borderWidth:2,...(opts&&opts.ds||{})}].concat((opts&&opts.extra)||[])},
    options:{animation:false,responsive:true,maintainAspectRatio:false,
      scales:{x:{ticks:{color:'#9aa0a8',maxTicksLimit:6},grid:{color:'#22262d'}},y:{ticks:{color:'#9aa0a8'},grid:{color:'#22262d'},beginAtZero:true,...(opts&&opts.y||{})}},
      plugins:{legend:{labels:{color:'#9aa0a8',boxWidth:12}}}}});
}
C.ram=mk('ramChart','Used',' #c96442'.trim());
C.vram=mk('vramChart','GPU %','#3fb950',{y:{max:100}});
C.model=mk('modelChart','Model VRAM','#6ea8fe');
C.load=mk('loadChart','load1','#e0795a');
function fmtTime(t){const d=new Date(t*1000);return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})}
function card(n,l){return '<div class="card"><div class="n">'+n+'</div><div class="l">'+l+'</div></div>'}
async function tick(){
  let j;try{j=await (await fetch('/metrics')).json()}catch(e){return}
  const h=j.history||[];if(!h.length)return;
  const last=h[h.length-1];
  const labels=h.map(s=>fmtTime(s.t));
  C.ram.data.labels=labels;C.ram.data.datasets[0].data=h.map(s=>s.ram_used_gb);
  C.vram.data.labels=labels;C.vram.data.datasets[0].data=h.map(s=>s.gpu_util);
  C.model.data.labels=labels;C.model.data.datasets[0].data=h.map(s=>s.model_vram_gb);
  C.load.data.labels=labels;C.load.data.datasets[0].data=h.map(s=>s.load1);
  C.ram.update();C.vram.update();C.model.update();C.load.update();
  document.getElementById('cards').innerHTML=
    card(last.ram_used_gb+' / '+last.ram_total_gb,'RAM used (GB)')+
    card((last.gpu_util!=null?last.gpu_util+'%':'n/a'),'GPU utilization')+
    card((last.gpu_temp!=null?last.gpu_temp+'°C':'n/a'),'GPU temp')+
    card(last.models.length,'Models spun up')+
    card(last.model_vram_gb,'Model memory (GB)')+
    card(last.load1.toFixed(2),'Load (1-min)');
  document.getElementById('ctl').innerHTML='<span class="dot '+(last.controller_ok?'on':'off')+'"></span>controller '+(last.controller_ok?'up':'down');
  document.getElementById('clock').textContent='updated '+fmtTime(last.t);
  const ms=last.models;
  document.getElementById('models').innerHTML=ms.length?
    '<table><tr><th>Model</th><th>VRAM (GB)</th><th>Total size (GB)</th></tr>'+
      ms.map(m=>'<tr><td><code>'+m.name+'</code></td><td>'+m.vram_gb+'</td><td>'+m.size_gb+'</td></tr>').join('')+'</table>'
    :'<span class="muted">No models loaded right now.</span>';
}
tick();setInterval(tick,3000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/metrics"):
            body = json.dumps({"history": list(HISTORY), "uptime_s": int(time.time() - _START)}).encode()
            self._send(body, "application/json")
        elif self.path == "/" or self.path.startswith("/?"):
            self._send(PAGE.encode(), "text/html; charset=utf-8")
        else:
            self.send_response(404)
            self.end_headers()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8422)
    ap.add_argument("--host", default="127.0.0.1")  # local only
    args = ap.parse_args()
    HISTORY.append(sample())  # seed one point immediately
    threading.Thread(target=_sampler_loop, daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"TARS monitor on http://{args.host}:{args.port} (local only)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
