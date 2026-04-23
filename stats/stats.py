#!/usr/bin/env python3

# ============================================================
# ROV Pi Stats Monitor  —  port 9000
# Self-contained, no extra deps (stdlib only)
#
# GET /               HTML dashboard
# GET /stats          latest JSON snapshot
# GET /stats/history  rolling 10-min history
# GET /stats/quick    minimal dict for UI polling
# ============================================================

import collections, json, os, re, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT       = 9000
HISTORY_S  = 600
SAMPLE_S   = 5
MAX_POINTS = HISTORY_S // SAMPLE_S          # 120 points
NUM_CORES  = os.cpu_count() or 4

CAMS           = ["cam0", "cam1", "cam2"]
THUMB_DIR      = "/tmp"
THUMB_INTERVAL = 5    # seconds between file reads (ffmpeg writes every 5s)
ROS2_SETUP       = "/opt/ros/jazzy/setup.bash"
SLVROV_SETUP     = "/home/pi/slvrov_ros/install/setup.bash"
ROS_DOMAIN_ID    = "42"
FASTDDS_PROFILE  = "/home/pi/fastdds_config.xml"

# Shell preamble mirroring start_rov.sh — sources both workspaces
# and sets domain ID so we see the same nodes
def _ros2_preamble():
    parts = []
    if os.path.exists(ROS2_SETUP):
        parts.append(f"source {ROS2_SETUP}")
    if os.path.exists(SLVROV_SETUP):
        parts.append(f"source {SLVROV_SETUP}")
    parts.append(f"export ROS_DOMAIN_ID={ROS_DOMAIN_ID}")
    return " && ".join(parts)

history      = collections.deque(maxlen=MAX_POINTS)
history_lock = threading.Lock()

# ── camera thumbnails ─────────────────────────────────────────
cam_thumbs   = {}   # cam -> jpeg bytes
cam_thumb_ts = {}   # cam -> time.time() of last successful read
cam_lock     = threading.Lock()

def grab_thumbnails():
    """Read JPEG thumbnails written by each camera's ffmpeg process."""
    for cam in CAMS:
        path = os.path.join(THUMB_DIR, f"thumb_{cam}.jpg")
        try:
            mtime = os.path.getmtime(path)
            with open(path, "rb") as f:
                data = f.read()
            if data:
                with cam_lock:
                    cam_thumbs[cam]   = data
                    cam_thumb_ts[cam] = time.time()
        except Exception:
            pass

def thumbnail_sampler():
    while True:
        try:
            grab_thumbnails()
        except Exception as e:
            print(f"Thumbnail error: {e}")
        time.sleep(THUMB_INTERVAL)

# ── per-process CPU tracking ──────────────────────────────────
# Maps pid -> (utime+stime at last sample, wall_time at last sample)
_pid_prev   = {}
_pid_prev_lock = threading.Lock()

def _proc_stat(pid):
    """Return (utime+stime ticks, name) for a pid, or None."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            raw = f.read()
        # name is between first ( and last ) to handle spaces
        name = raw[raw.index("(")+1 : raw.rindex(")")]
        rest = raw[raw.rindex(")")+2:].split()
        ticks = int(rest[11]) + int(rest[12])   # utime + stime
        return ticks, name
    except Exception:
        return None

def _pids_for(name):
    try:
        r = subprocess.run(["pgrep", "-x", name], capture_output=True, timeout=2)
        return [int(p) for p in r.stdout.decode().split() if p]
    except Exception:
        return []

def _all_pids():
    """All pids currently visible in /proc."""
    pids = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            pids.append(int(entry))
    return pids

def process_cpu_breakdown():
    """
    Returns:
      top5       — top 5 pids by CPU%, any process
      named      — {mediamtx, ffmpeg instances, ros2 nodes} with cpu%
    """
    now   = time.time()
    hz    = os.sysconf("SC_CLK_TCK")
    pids  = _all_pids()

    curr  = {}
    for pid in pids:
        result = _proc_stat(pid)
        if result:
            ticks, name = result
            curr[pid] = (ticks, name)

    with _pid_prev_lock:
        prev = dict(_pid_prev)
        _pid_prev.clear()
        _pid_prev.update(curr)

    # calculate cpu% per pid
    cpu_by_pid = {}
    for pid, (ticks, name) in curr.items():
        if pid in prev:
            dt_ticks = ticks - prev[pid][0]
            dt_wall  = now - (now - SAMPLE_S)   # approx; good enough
            pct = round(dt_ticks / hz / SAMPLE_S * 100, 1)
            cpu_by_pid[pid] = {"pid": pid, "name": name, "cpu_pct": max(0.0, pct)}

    # top 5 by cpu%
    top5 = sorted(cpu_by_pid.values(), key=lambda x: x["cpu_pct"], reverse=True)[:5]

    # named processes
    def named_entry(proc_name):
        pids_for = _pids_for(proc_name)
        entries  = []
        for pid in pids_for:
            if pid in cpu_by_pid:
                entries.append(cpu_by_pid[pid])
            else:
                entries.append({"pid": pid, "name": proc_name, "cpu_pct": None})
        return entries

    named = {
        "mediamtx": named_entry("mediamtx"),
        "ffmpeg":   named_entry("ffmpeg"),
    }

    named["ros2"] = {}   # populated separately via rclpy, not subprocess
    return {"top5": top5, "named": named}

# ── system collectors ─────────────────────────────────────────

def cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000, 1)
    except Exception:
        return None

def cpu_percent():
    def read():
        with open("/proc/stat") as f:
            parts = f.readline().split()
        idle = int(parts[4]); total = sum(int(x) for x in parts[1:])
        return idle, total
    i1,t1 = read(); time.sleep(0.2); i2,t2 = read()
    dt = t2-t1; di = i2-i1
    return round(100.0*(1-di/dt),1) if dt else 0.0

def load_average():
    try:
        with open("/proc/loadavg") as f:
            p = f.read().split()
        return {"1min": float(p[0]), "5min": float(p[1]), "15min": float(p[2])}
    except Exception:
        return {"1min": None, "5min": None, "15min": None}

def memory():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k,v = line.split(":")
            info[k.strip()] = int(v.split()[0])
    total = info.get("MemTotal",0); avail = info.get("MemAvailable",0)
    used  = total-avail
    st = info.get("SwapTotal",0); sf = info.get("SwapFree",0); su = st-sf
    return {
        "total_mb": round(total/1024,1), "used_mb": round(used/1024,1),
        "free_mb":  round(avail/1024,1), "percent": round(100*used/total,1) if total else 0,
        "swap": {"total_mb":round(st/1024,1),"used_mb":round(su/1024,1),
                 "percent":round(100*su/st,1) if st else 0},
    }

def disk(path="/"):
    st = os.statvfs(path)
    total=st.f_blocks*st.f_frsize; free=st.f_bavail*st.f_frsize; used=total-free
    return {"total_gb":round(total/1e9,2),"used_gb":round(used/1e9,2),
            "free_gb":round(free/1e9,2),"percent":round(100*used/total,1) if total else 0}

_last_ds = {}; _last_ds_time = 0.0

def _read_diskstats():
    stats = {}
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                p = line.split()
                if len(p)<14: continue
                dev = p[2]
                if re.match(r'^(mmcblk\d+|sd[a-z]|nvme\d+n\d+)$', dev):
                    stats[dev] = (int(p[3]),int(p[7]),int(p[5]),int(p[9]))
    except Exception: pass
    return stats

def disk_io():
    global _last_ds, _last_ds_time
    now=time.time(); curr=_read_diskstats(); prev=_last_ds; dt=now-_last_ds_time
    _last_ds=curr; _last_ds_time=now
    if not prev or dt<=0:
        return {"read_iops":0,"write_iops":0,"read_kb_s":0,"write_kb_s":0}
    ri=wi=rk=wk=0
    for dev,(rc,wc,rs,ws) in curr.items():
        if dev not in prev: continue
        pr,pw,prs,pws = prev[dev]
        ri+=rc-pr; wi+=wc-pw; rk+=(rs-prs)*512/1024; wk+=(ws-pws)*512/1024
    return {"read_iops":round(ri/dt,1),"write_iops":round(wi/dt,1),
            "read_kb_s":round(rk/dt,1),"write_kb_s":round(wk/dt,1)}

def uptime_info():
    with open("/proc/uptime") as f:
        s = float(f.read().split()[0])
    h,rem=divmod(int(s),3600); m,sec=divmod(rem,60)
    return {"seconds":int(s),"human":f"{h}h {m}m {sec}s"}

def network(iface="eth0"):
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if iface in line:
                    p=line.split()
                    return {"interface":iface,"rx_mb":round(int(p[1])/1e6,2),"tx_mb":round(int(p[9])/1e6,2)}
    except Exception: pass
    return {"interface":iface,"rx_mb":None,"tx_mb":None}

def throttle_flags():
    try:
        r=subprocess.run(["vcgencmd","get_throttled"],capture_output=True,timeout=2)
        val=int(r.stdout.decode().strip().split("=")[1],16)
        return {"raw":hex(val),
                "undervoltage_now":bool(val&(1<<0)),"freq_capped_now":bool(val&(1<<1)),
                "throttled_now":bool(val&(1<<2)),"temp_limit_now":bool(val&(1<<3)),
                "undervoltage_occurred":bool(val&(1<<16)),"freq_capped_occurred":bool(val&(1<<17)),
                "throttled_occurred":bool(val&(1<<18)),"temp_limit_occurred":bool(val&(1<<19)),
                "any_issue_now":bool(val&0x000F)}
    except Exception:
        return {"raw":None,"error":"vcgencmd unavailable"}

# ── ROS2 via rclpy (no subprocess, no bash sourcing) ──────────
_rclpy      = None   # module, set on first successful import
_ros2_node  = None   # long-lived rclpy node
_ros2_lock  = threading.Lock()

_ros2_init_error = None   # last error string from _ensure_ros2_node

def _ensure_ros2_node():
    global _rclpy, _ros2_node, _ros2_init_error
    os.environ["ROS_DOMAIN_ID"] = ROS_DOMAIN_ID
    if os.path.exists(FASTDDS_PROFILE):
        os.environ["FASTRTPS_DEFAULT_PROFILES_FILE"] = FASTDDS_PROFILE
    if _rclpy is None:
        try:
            import rclpy as _rclpy_mod
            _rclpy = _rclpy_mod
        except ImportError as e:
            _ros2_init_error = f"rclpy import failed: {e}"
            return False
    try:
        if _ros2_node is None:
            _rclpy.init()
            _ros2_node = _rclpy.create_node("rov_stats_monitor")
        _ros2_init_error = None
        return True
    except Exception as e:
        _ros2_init_error = str(e)
        return False

def ros2_nodes():
    with _ros2_lock:
        if not _ensure_ros2_node():
            return {"available": False, "nodes": [], "error": _ros2_init_error or "rclpy not available"}
        try:
            _rclpy.spin_once(_ros2_node, timeout_sec=0.5)
            pairs = _ros2_node.get_node_names_and_namespaces()
            nodes = sorted(
                f"{ns.rstrip('/')}/{name}"
                for name, ns in pairs
                if name != "rov_stats_monitor"
            )
            return {"available": True, "count": len(nodes), "nodes": nodes}
        except Exception as e:
            return {"available": True, "nodes": [], "error": str(e)}

def process_count(name):
    try:
        r=subprocess.run(["pgrep","-x",name],capture_output=True,timeout=2)
        return len([p for p in r.stdout.decode().strip().split() if p])
    except Exception: return 0

def process_running(name): return process_count(name)>0

# ── snapshot ──────────────────────────────────────────────────

def snapshot():
    temp=cpu_temp()
    return {
        "ts":time.time(),"iso":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        "temp_c":temp,"temp_f":round(temp*9/5+32,1) if temp else None,
        "cpu_pct":cpu_percent(),"load":load_average(),
        "memory":memory(),"disk":disk("/"),"disk_io":disk_io(),
        "uptime":uptime_info(),"network":network(),
        "processes":{
            "mediamtx":{"running":process_running("mediamtx")},
            "ffmpeg":{"running":process_running("ffmpeg"),"count":process_count("ffmpeg")},
        },
        "proc_cpu": process_cpu_breakdown(),
        "throttle":throttle_flags(),
        "ros2":ros2_nodes(),
        "cameras":{cam:{"ok": cam in cam_thumbs,
                         "fresh": cam in cam_thumbs and (time.time()-cam_thumb_ts.get(cam,0))<30
                        } for cam in CAMS},
    }

# ── sampler thread ────────────────────────────────────────────

def sampler():
    _read_diskstats()
    # prime pid table
    for pid in _all_pids():
        r = _proc_stat(pid)
        if r: _pid_prev[pid] = r
    time.sleep(SAMPLE_S)
    while True:
        try:
            s=snapshot()
            with history_lock: history.append(s)
        except Exception as e: print(f"Sampler error: {e}")
        time.sleep(SAMPLE_S)

# ── dashboard ─────────────────────────────────────────────────

DASHBOARD = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ROV Pi Stats</title>
<style>
:root{--bg:#0a0c0f;--surface:#111418;--border:#1e2530;--accent:#00e5ff;
      --warn:#ffb300;--danger:#ff3b5c;--ok:#00e676;--text:#c8d6e5;--dim:#4a5a6a;
      --mono:'Share Tech Mono',monospace}
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Barlow:wght@300;500&display=swap');
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Barlow',sans-serif;font-weight:300;min-height:100vh}
header{display:flex;align-items:center;gap:12px;padding:10px 20px;
       background:var(--surface);border-bottom:1px solid var(--border)}
.logo{font-family:var(--mono);color:var(--accent);letter-spacing:3px;font-size:1rem}
.sub{font-family:var(--mono);color:var(--dim);font-size:.6rem;letter-spacing:4px}
.uptime{margin-left:auto;font-family:var(--mono);font-size:.65rem;color:var(--dim)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:2px;padding:2px}
.card{background:var(--surface);padding:16px}
.card-title{font-family:var(--mono);font-size:.6rem;letter-spacing:3px;color:var(--accent);margin-bottom:10px}
.big-val{font-family:var(--mono);font-size:2rem;font-weight:500;line-height:1}
.big-unit{font-size:.8rem;color:var(--dim);margin-left:3px}
.sub-val{font-family:var(--mono);font-size:.7rem;color:var(--dim);margin-top:4px}
.bar-wrap{margin-top:8px;background:var(--border);height:4px;border-radius:2px}
.bar{height:4px;border-radius:2px;transition:width .4s}
canvas{width:100%!important;height:60px!important;margin-top:8px;display:block}
.pill{display:inline-block;font-family:var(--mono);font-size:.6rem;letter-spacing:1px;
      padding:2px 7px;border-radius:2px;border:1px solid}
.pill.ok    {color:var(--ok);    border-color:var(--ok);    background:rgba(0,230,118,.07)}
.pill.warn  {color:var(--warn);  border-color:var(--warn);  background:rgba(255,179,0,.07)}
.pill.danger{color:var(--danger);border-color:var(--danger);background:rgba(255,59,92,.07)}
.pill.dim   {color:var(--dim);   border-color:var(--border)}
.row{display:flex;justify-content:space-between;align-items:center;
     margin-bottom:5px;font-family:var(--mono);font-size:.7rem}
.flag-ok{color:var(--ok)}.flag-bad{color:var(--danger)}
/* process CPU table */
.proc-table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:.65rem;margin-top:6px}
.proc-table th{color:var(--dim);font-weight:normal;text-align:left;padding:2px 4px;
               border-bottom:1px solid var(--border);letter-spacing:1px}
.proc-table td{padding:3px 4px;border-bottom:1px solid #0d1014}
.cpu-bar-cell{width:80px}
.cpu-mini-bar{height:3px;border-radius:1px;background:var(--ok);transition:width .4s}
.highlight-row{background:rgba(0,229,255,.04)}
.node-list{font-family:var(--mono);font-size:.65rem;margin-top:6px;
           max-height:130px;overflow-y:auto;line-height:1.8}
.node-list span{display:block;padding:1px 0;border-bottom:1px solid var(--border)}
.node-none{color:var(--dim);font-style:italic}
footer{font-family:var(--mono);font-size:.6rem;color:var(--dim);
       text-align:center;padding:10px;letter-spacing:2px}
</style>
</head>
<body>
<header>
  <span class="logo">⬡ ROV</span>
  <span class="sub">PI STATS</span>
  <span class="uptime" id="uptime-hdr">—</span>
</header>
<div class="grid">

  <!-- Temperature -->
  <div class="card">
    <div class="card-title">CPU TEMPERATURE</div>
    <div><span class="big-val" id="temp-c">—</span><span class="big-unit">°C</span>
         &nbsp;<span class="big-val" style="font-size:1.1rem" id="temp-f">—</span><span class="big-unit">°F</span></div>
    <div class="bar-wrap"><div class="bar" id="temp-bar" style="width:0%"></div></div>
    <canvas id="chart-temp"></canvas>
  </div>

  <!-- Load average -->
  <div class="card">
    <div class="card-title">LOAD AVERAGE</div>
    <div><span class="big-val" id="load-1">—</span><span class="big-unit">1m</span></div>
    <div class="row" style="margin-top:6px"><span>5 min</span><span id="load-5" style="font-family:var(--mono)">—</span></div>
    <div class="row"><span>15 min</span><span id="load-15" style="font-family:var(--mono)">—</span></div>
    <canvas id="chart-load"></canvas>
  </div>

  <!-- CPU usage -->
  <div class="card">
    <div class="card-title">CPU USAGE</div>
    <div><span class="big-val" id="cpu-pct">—</span><span class="big-unit">%</span></div>
    <div class="bar-wrap"><div class="bar" id="cpu-bar" style="width:0%"></div></div>
    <canvas id="chart-cpu"></canvas>
  </div>

  <!-- Memory + swap -->
  <div class="card">
    <div class="card-title">MEMORY</div>
    <div><span class="big-val" id="mem-pct">—</span><span class="big-unit">%</span></div>
    <div class="sub-val" id="mem-detail">—</div>
    <div class="bar-wrap"><div class="bar" id="mem-bar" style="width:0%"></div></div>
    <canvas id="chart-mem"></canvas>
    <div style="margin-top:8px">
      <div class="row"><span>SWAP</span><span id="swap-pct" class="pill dim">—</span></div>
      <div class="sub-val" id="swap-detail">—</div>
    </div>
  </div>

  <!-- Disk + IO -->
  <div class="card">
    <div class="card-title">DISK /</div>
    <div><span class="big-val" id="disk-pct">—</span><span class="big-unit">%</span></div>
    <div class="sub-val" id="disk-detail">—</div>
    <div class="bar-wrap"><div class="bar" id="disk-bar" style="width:0%"></div></div>
    <div style="margin-top:10px">
      <div class="card-title">DISK I/O</div>
      <div class="row"><span>READ</span>  <span id="io-rkb"   class="pill dim">—</span></div>
      <div class="row"><span>WRITE</span> <span id="io-wkb"   class="pill dim">—</span></div>
      <div class="row"><span>R IOPS</span><span id="io-riops" class="pill dim">—</span></div>
      <div class="row"><span>W IOPS</span><span id="io-wiops" class="pill dim">—</span></div>
    </div>
    <canvas id="chart-io"></canvas>
  </div>

  <!-- Network -->
  <div class="card">
    <div class="card-title">NETWORK (eth0)</div>
    <div class="row"><span>RX</span><span><span class="big-val" style="font-size:1.1rem" id="net-rx">—</span><span class="big-unit">MB</span></span></div>
    <div class="row"><span>TX</span><span><span class="big-val" style="font-size:1.1rem" id="net-tx">—</span><span class="big-unit">MB</span></span></div>
    <canvas id="chart-net"></canvas>
    <br>
    <div class="card-title">THERMAL / POWER</div>
    <div class="row"><span>Undervoltage now</span>    <span id="fl-uv-now">—</span></div>
    <div class="row"><span>Throttled now</span>       <span id="fl-th-now">—</span></div>
    <div class="row"><span>Temp limit now</span>      <span id="fl-tl-now">—</span></div>
    <div class="row"><span>Undervoltage (ever)</span> <span id="fl-uv-occ">—</span></div>
    <div class="row"><span>Throttled (ever)</span>    <span id="fl-th-occ">—</span></div>
  </div>

  <!-- Per-process CPU — top 5 -->
  <div class="card" style="grid-column:span 2">
    <div class="card-title">TOP PROCESSES BY CPU%</div>
    <table class="proc-table">
      <thead><tr><th>PID</th><th>NAME</th><th>CPU%</th><th></th></tr></thead>
      <tbody id="top5-body"></tbody>
    </table>
    <br>
    <div class="card-title">NAMED PROCESS BREAKDOWN</div>
    <table class="proc-table">
      <thead><tr><th>PROCESS</th><th>PID</th><th>CPU%</th><th></th></tr></thead>
      <tbody id="named-body"></tbody>
    </table>
  </div>

  <!-- Camera thumbnails -->
  <div class="card" style="grid-column:span 2">
    <div class="card-title">CAMERAS <span id="cam-ts" style="color:var(--dim);font-size:.55rem;letter-spacing:1px"></span></div>
    <div style="display:flex;gap:12px;flex-wrap:wrap">
      <div id="cam-cam0" style="flex:1;min-width:140px"><div class="sub-val" style="margin-bottom:4px">CAM0 <span id="pill-cam0" class="pill dim">—</span></div><img id="img-cam0" src="" style="width:100%;border:1px solid var(--border);background:#000;display:block;min-height:80px"></div>
      <div id="cam-cam1" style="flex:1;min-width:140px"><div class="sub-val" style="margin-bottom:4px">CAM1 <span id="pill-cam1" class="pill dim">—</span></div><img id="img-cam1" src="" style="width:100%;border:1px solid var(--border);background:#000;display:block;min-height:80px"></div>
      <div id="cam-cam2" style="flex:1;min-width:140px"><div class="sub-val" style="margin-bottom:4px">CAM2 <span id="pill-cam2" class="pill dim">—</span></div><img id="img-cam2" src="" style="width:100%;border:1px solid var(--border);background:#000;display:block;min-height:80px"></div>
    </div>
  </div>

  <!-- Processes status + ROS2 -->
  <div class="card">
    <div class="card-title">SERVICES</div>
    <div class="row"><span>mediamtx</span>
      <span id="proc-mediamtx" class="pill dim">—</span></div>
    <div class="row"><span>ffmpeg</span>
      <span><span id="proc-ffmpeg" class="pill dim">—</span>
            &nbsp;<span id="proc-ffmpeg-count" style="font-family:var(--mono);font-size:.65rem;color:var(--dim)"></span>
      </span></div>
    <br>
    <div class="card-title">ROS2 NODES <span id="ros2-count" style="color:var(--dim)"></span></div>
    <span id="ros2-status" class="pill dim">—</span>
    <div class="node-list" id="ros2-nodes"></div>
  </div>

</div>
<footer id="footer-ts">LAST UPDATE: —</footer>

<script>
const POLL_MS=5000, NCORES=4;
function makeChart(id,color){
  const c=document.getElementById(id);
  c.width=c.offsetWidth||300; c.height=60;
  const ctx=c.getContext('2d');
  return {draw(pts,mn,mx){
    const W=c.width,H=c.height,range=(mx-mn)||1;
    ctx.clearRect(0,0,W,H);
    ctx.strokeStyle='#1e253033';ctx.lineWidth=0.5;
    [.25,.5,.75].forEach(f=>{ctx.beginPath();ctx.moveTo(0,H*f);ctx.lineTo(W,H*f);ctx.stroke()});
    if(pts.length<2)return;
    ctx.beginPath();ctx.strokeStyle=color;ctx.lineWidth=1.5;
    pts.forEach((v,i)=>{const x=(i/(pts.length-1))*W,y=H-((v-mn)/range)*(H-4)-2;
      i===0?ctx.moveTo(x,y):ctx.lineTo(x,y)});
    ctx.stroke();
    ctx.lineTo(W,H);ctx.lineTo(0,H);ctx.closePath();ctx.fillStyle=color+'22';ctx.fill();
  }};
}
const charts={
  temp:makeChart('chart-temp','#00e5ff'),
  load:makeChart('chart-load','#e040fb'),
  cpu: makeChart('chart-cpu', '#00e676'),
  mem: makeChart('chart-mem', '#ffb300'),
  io:  makeChart('chart-io',  '#b388ff'),
  net: makeChart('chart-net', '#ff6b35'),
};

function barColor(p){return p>85?'var(--danger)':p>65?'var(--warn)':'var(--ok)'}
function setBar(id,p){const e=document.getElementById(id);e.style.width=p+'%';e.style.background=barColor(p)}
function setPill(id,running,count){
  const e=document.getElementById(id);
  if(running==null){e.textContent='UNKNOWN';e.className='pill dim';return}
  e.textContent=running?'RUNNING':'STOPPED';e.className=running?'pill ok':'pill danger';
  if(count!=null){const ce=document.getElementById(id+'-count');if(ce)ce.textContent=running?'×'+count:''}
}
function setFlag(id,bad){
  const e=document.getElementById(id);
  if(bad==null){e.textContent='—';e.className='';return}
  e.textContent=bad?'⚠ YES':'NO';e.className=bad?'flag-bad':'flag-ok';
}
function setIoPill(id,val,unit){
  const e=document.getElementById(id);
  if(val==null){e.textContent='—';e.className='pill dim';return}
  e.textContent=`${val} ${unit}`;e.className=val>500?'pill warn':'pill ok';
}
function cpuColor(pct){
  if(pct==null)return'var(--dim)';
  return pct>80?'var(--danger)':pct>40?'var(--warn)':'var(--ok)';
}

function renderTop5(top5){
  const tbody=document.getElementById('top5-body');
  tbody.innerHTML='';
  (top5||[]).forEach(p=>{
    const pct=p.cpu_pct??0;
    const barW=Math.min(100,pct/NCORES*100);
    const color=cpuColor(pct);
    tbody.innerHTML+=`<tr>
      <td style="color:var(--dim)">${p.pid}</td>
      <td>${p.name}</td>
      <td style="color:${color}">${pct}%</td>
      <td class="cpu-bar-cell"><div class="cpu-mini-bar" style="width:${barW}%;background:${color}"></div></td>
    </tr>`;
  });
}

function renderNamed(named){
  const tbody=document.getElementById('named-body');
  tbody.innerHTML='';
  // mediamtx
  const mtx=(named?.mediamtx||[]);
  if(mtx.length){
    mtx.forEach(p=>{
      const pct=p.cpu_pct??'—'; const color=cpuColor(p.cpu_pct);
      const barW=p.cpu_pct!=null?Math.min(100,p.cpu_pct/NCORES*100):0;
      tbody.innerHTML+=`<tr class="highlight-row">
        <td style="color:var(--accent)">mediamtx</td>
        <td style="color:var(--dim)">${p.pid}</td>
        <td style="color:${color}">${pct}${p.cpu_pct!=null?'%':''}</td>
        <td class="cpu-bar-cell"><div class="cpu-mini-bar" style="width:${barW}%;background:${color}"></div></td>
      </tr>`;
    });
  }
  // ffmpeg instances
  (named?.ffmpeg||[]).forEach((p,i)=>{
    const pct=p.cpu_pct??'—'; const color=cpuColor(p.cpu_pct);
    const barW=p.cpu_pct!=null?Math.min(100,p.cpu_pct/NCORES*100):0;
    tbody.innerHTML+=`<tr class="highlight-row">
      <td style="color:#ff6b35">ffmpeg #${i+1}</td>
      <td style="color:var(--dim)">${p.pid}</td>
      <td style="color:${color}">${pct}${p.cpu_pct!=null?'%':''}</td>
      <td class="cpu-bar-cell"><div class="cpu-mini-bar" style="width:${barW}%;background:${color}"></div></td>
    </tr>`;
  });
  // ros2 nodes
  Object.entries(named?.ros2||{}).forEach(([node,p])=>{
    const pct=p.cpu_pct??'—'; const color=cpuColor(p.cpu_pct);
    const barW=p.cpu_pct!=null?Math.min(100,p.cpu_pct/NCORES*100):0;
    tbody.innerHTML+=`<tr class="highlight-row">
      <td style="color:#e040fb">${node}</td>
      <td style="color:var(--dim)">${p.pid??'—'}</td>
      <td style="color:${color}">${pct}${p.cpu_pct!=null?'%':''}</td>
      <td class="cpu-bar-cell"><div class="cpu-mini-bar" style="width:${barW}%;background:${color}"></div></td>
    </tr>`;
  });
}

async function update(){
  try{
    const pts=await fetch('/stats/history').then(r=>r.json());
    if(!pts.length)return;
    const l=pts[pts.length-1];

    // temp
    document.getElementById('temp-c').textContent=l.temp_c??'—';
    document.getElementById('temp-f').textContent=l.temp_f??'—';
    setBar('temp-bar',Math.min(100,((l.temp_c??0)/85)*100));
    charts.temp.draw(pts.map(p=>p.temp_c??0),0,85);

    // load
    const ld=l.load??{};
    document.getElementById('load-1').textContent=ld['1min']??'—';
    document.getElementById('load-5').textContent=ld['5min']??'—';
    document.getElementById('load-15').textContent=ld['15min']??'—';
    const loads=pts.map(p=>p.load?.['1min']??0);
    charts.load.draw(loads,0,Math.max(...loads,1));

    // cpu
    document.getElementById('cpu-pct').textContent=l.cpu_pct??'—';
    setBar('cpu-bar',l.cpu_pct??0);
    charts.cpu.draw(pts.map(p=>p.cpu_pct??0),0,100);

    // memory
    document.getElementById('mem-pct').textContent=l.memory?.percent??'—';
    document.getElementById('mem-detail').textContent=`${l.memory?.used_mb??'—'} / ${l.memory?.total_mb??'—'} MB`;
    setBar('mem-bar',l.memory?.percent??0);
    charts.mem.draw(pts.map(p=>p.memory?.percent??0),0,100);
    const sw=l.memory?.swap,swp=sw?.percent??0;
    const spe=document.getElementById('swap-pct');
    spe.textContent=`${swp}%`;spe.className=swp>50?'pill danger':swp>10?'pill warn':'pill ok';
    document.getElementById('swap-detail').textContent=`${sw?.used_mb??'—'} / ${sw?.total_mb??'—'} MB`;

    // disk
    document.getElementById('disk-pct').textContent=l.disk?.percent??'—';
    document.getElementById('disk-detail').textContent=`${l.disk?.used_gb??'—'} / ${l.disk?.total_gb??'—'} GB`;
    setBar('disk-bar',l.disk?.percent??0);
    const iovals=pts.map(p=>(p.disk_io?.read_kb_s??0)+(p.disk_io?.write_kb_s??0));
    setIoPill('io-rkb',  l.disk_io?.read_kb_s, 'KB/s');
    setIoPill('io-wkb',  l.disk_io?.write_kb_s,'KB/s');
    setIoPill('io-riops',l.disk_io?.read_iops, 'r/s');
    setIoPill('io-wiops',l.disk_io?.write_iops,'w/s');
    charts.io.draw(iovals,0,Math.max(...iovals,1));

    // network
    document.getElementById('net-rx').textContent=l.network?.rx_mb??'—';
    document.getElementById('net-tx').textContent=l.network?.tx_mb??'—';
    const rxs=pts.map(p=>p.network?.rx_mb??0);
    charts.net.draw(rxs,Math.min(...rxs),Math.max(...rxs,0.01));

    // throttle
    const th=l.throttle??{};
    setFlag('fl-uv-now',th.undervoltage_now);setFlag('fl-th-now',th.throttled_now);
    setFlag('fl-tl-now',th.temp_limit_now);  setFlag('fl-uv-occ',th.undervoltage_occurred);
    setFlag('fl-th-occ',th.throttled_occurred);

    // per-process CPU
    const pc=l.proc_cpu??{};
    renderTop5(pc.top5);
    renderNamed(pc.named);

    // services
    setPill('proc-mediamtx',l.processes?.mediamtx?.running);
    setPill('proc-ffmpeg',  l.processes?.ffmpeg?.running,l.processes?.ffmpeg?.count);

    // ros2
    const ros=l.ros2??{};
    document.getElementById('ros2-count').textContent=ros.count!=null?`(${ros.count})`:'';
    const rs=document.getElementById('ros2-status');
    const nl=document.getElementById('ros2-nodes');
    if(!ros.available){rs.textContent='NOT INSTALLED';rs.className='pill dim';nl.innerHTML='<span class="node-none">ROS2 not found</span>'}
    else if(ros.error){rs.textContent='ERROR';rs.className='pill warn';nl.innerHTML=`<span class="node-none">${ros.error}</span>`}
    else if(!ros.nodes?.length){rs.textContent='NO NODES';rs.className='pill warn';nl.innerHTML='<span class="node-none">No nodes running</span>'}
    else{rs.textContent='ACTIVE';rs.className='pill ok';nl.innerHTML=ros.nodes.map(n=>`<span>${n}</span>`).join('')}

    // cameras
    const cams=l.cameras??{};
    let anyOk=false;
    ['cam0','cam1','cam2'].forEach(cam=>{
      const info=cams[cam]??{};
      const pill=document.getElementById('pill-'+cam);
      if(info.ok&&info.fresh){pill.textContent='OK';pill.className='pill ok';anyOk=true;}
      else if(info.ok&&!info.fresh){pill.textContent='STALE';pill.className='pill warn';}
      else{pill.textContent='OFFLINE';pill.className='pill danger';}
    });
    document.getElementById('cam-ts').textContent=anyOk?'(updated every 5s)':'';

    document.getElementById('uptime-hdr').textContent='UP '+(l.uptime?.human??'—');
    document.getElementById('footer-ts').textContent='LAST UPDATE: '+(l.iso??'—');
  }catch(e){console.warn('fetch failed:',e)}
}

// Refresh thumbnail images every 60s
function refreshCamImages(){
  const t=Date.now();
  ['cam0','cam1','cam2'].forEach(cam=>{
    document.getElementById('img-'+cam).src='/cam/'+cam+'.jpg?t='+t;
  });
}
refreshCamImages();
setInterval(refreshCamImages,5000);
update();setInterval(update,POLL_MS);
</script>
</body>
</html>
"""

# ── HTTP handler ──────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path=self.path.split("?")[0]
        if path=="/":self._html(DASHBOARD)
        elif path.startswith("/cam/") and path.endswith(".jpg"):
            cam=path[5:-4]
            with cam_lock: data=cam_thumbs.get(cam)
            if data:
                self.send_response(200)
                self.send_header("Content-Type","image/jpeg")
                self.send_header("Content-Length",str(len(data)))
                self.send_header("Cache-Control","no-cache")
                self.end_headers();self.wfile.write(data)
            else:self.send_response(404);self.end_headers()
        elif path=="/stats":
            with history_lock: data=history[-1] if history else {}
            self._json(data)
        elif path=="/stats/history":
            with history_lock: data=list(history)
            self._json(data)
        elif path=="/stats/quick":
            with history_lock: l=history[-1] if history else {}
            self._json({
                "temp_c":l.get("temp_c"),"cpu_pct":l.get("cpu_pct"),
                "load_1min":l.get("load",{}).get("1min"),
                "mem_pct":l.get("memory",{}).get("percent"),
                "swap_pct":l.get("memory",{}).get("swap",{}).get("percent"),
                "mediamtx":l.get("processes",{}).get("mediamtx",{}).get("running"),
                "ffmpeg_count":l.get("processes",{}).get("ffmpeg",{}).get("count",0),
                "throttle_issue":l.get("throttle",{}).get("any_issue_now"),
                "ros2_nodes":l.get("ros2",{}).get("count",0),
                "uptime":l.get("uptime",{}).get("human"),
            })
        else:self.send_response(404);self.end_headers()

    def _json(self,data):
        body=json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(body)))
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers();self.wfile.write(body)

    def _html(self,html):
        body=html.encode()
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers();self.wfile.write(body)

    def log_message(self,fmt,*args):
        if int(args[1])>=400:super().log_message(fmt,*args)

if __name__=="__main__":
    _read_diskstats()
    for pid in _all_pids():
        r=_proc_stat(pid)
        if r:_pid_prev[pid]=r
    t=threading.Thread(target=sampler,daemon=True);t.start()
    th=threading.Thread(target=thumbnail_sampler,daemon=True);th.start()
    print(f"ROV stats → http://0.0.0.0:{PORT}/")
    HTTPServer(("0.0.0.0",PORT),Handler).serve_forever()
