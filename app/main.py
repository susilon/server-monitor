import os, shutil, subprocess, threading, time
import psutil
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

HOST = os.environ.get("HOST_ROOT", "/hostfs")
INTERVAL = float(os.environ.get("INTERVAL", "2"))
REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "vfat", "exfat", "ntfs", "ntfs3",
           "f2fs", "fuseblk", "nfs", "nfs4", "cifs"}
SKIP_NIC = ("veth", "br-", "docker", "lo")

app = FastAPI()
state = {"ready": False}


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def cpu_model():
    for line in (read(f"{HOST}/proc/cpuinfo") or read("/proc/cpuinfo")).splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return "CPU"


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None  # nvidia-smi prints "[N/A]"


def gpus():
    if not shutil.which("nvidia-smi"):
        return None
    q = "index,name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,power.limit,fan.speed,clocks.sm,clocks.mem"
    try:
        out = subprocess.check_output(
            ["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"], text=True, timeout=5)
        procs = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    keys = ["index", "name", "temp", "util", "vram_used", "vram_total", "power", "power_limit", "fan", "clk_sm", "clk_mem"]
    res = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        g = dict(zip(keys, p))
        for k in keys[2:]:
            g[k] = num(g[k])
        res.append(g)
    apps = []
    for line in procs.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) == 4:
            apps.append({"pid": p[1], "name": os.path.basename(p[2]), "vram": num(p[3])})
    if res:
        res[0]["procs"] = apps  # not mapped per GPU; shown once
    return res


def temps():
    out = []
    try:
        for chip, items in psutil.sensors_temperatures().items():
            for i, t in enumerate(items):
                out.append({"chip": chip, "label": t.label or f"sensor {i + 1}", "cur": t.current,
                            "high": t.high, "crit": t.critical})
    except Exception:
        pass
    return out


def disks():
    mounts = read(f"{HOST}/proc/1/mounts") or read("/proc/mounts")
    seen, res = set(), []
    for line in mounts.splitlines():
        p = line.split()
        if len(p) < 3:
            continue
        dev, mp, fs = p[0], p[1].replace("\\040", " "), p[2]
        if fs not in REAL_FS or dev in seen or mp.startswith("/var/lib/docker"):
            continue
        seen.add(dev)
        try:
            u = psutil.disk_usage(os.path.join(HOST, mp.lstrip("/")))
        except OSError:
            continue
        res.append({"dev": dev, "mount": mp, "fs": fs, "total": u.total, "used": u.used,
                    "free": u.free, "pct": round(u.used / (u.used + u.free) * 100, 1) if u.used + u.free else 0})
    return res


def sampler():
    prev_net = psutil.net_io_counters(pernic=True)
    prev_io = psutil.disk_io_counters()
    prev_t = time.time()
    psutil.cpu_percent(percpu=True)
    model = cpu_model()
    hostname = read(f"{HOST}/etc/hostname").strip() or os.uname().nodename
    while True:
        time.sleep(INTERVAL)
        now = time.time()
        dt = max(now - prev_t, 0.1)
        net, cur_net = [], psutil.net_io_counters(pernic=True)
        stats, addrs = psutil.net_if_stats(), psutil.net_if_addrs()
        for nic, c in cur_net.items():
            if nic.startswith(SKIP_NIC):
                continue
            p = prev_net.get(nic, c)
            ip = next((a.address for a in addrs.get(nic, []) if a.family.name == "AF_INET"), "")
            st = stats.get(nic)
            net.append({"name": nic, "ip": ip, "up": bool(st and st.isup), "speed": st.speed if st else 0,
                        "rx_bps": (c.bytes_recv - p.bytes_recv) / dt, "tx_bps": (c.bytes_sent - p.bytes_sent) / dt,
                        "rx_total": c.bytes_recv, "tx_total": c.bytes_sent,
                        "errs": c.errin + c.errout, "drops": c.dropin + c.dropout})
        io, io_rate = psutil.disk_io_counters(), {}
        if io and prev_io:
            io_rate = {"read_bps": (io.read_bytes - prev_io.read_bytes) / dt,
                       "write_bps": (io.write_bytes - prev_io.write_bytes) / dt}
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        freq = psutil.cpu_freq()
        state.update({
            "ready": True, "ts": now, "hostname": hostname, "uptime": now - psutil.boot_time(),
            "cpu": {"model": model, "pct": psutil.cpu_percent(), "cores": psutil.cpu_percent(percpu=True),
                    "load": os.getloadavg(), "freq": freq.current if freq else None,
                    "threads": psutil.cpu_count(), "physical": psutil.cpu_count(logical=False)},
            "mem": {"total": vm.total, "used": vm.total - vm.available, "available": vm.available,
                    "pct": vm.percent, "swap_total": sw.total, "swap_used": sw.used},
            "gpus": gpus(), "temps": temps(), "disks": disks(), "disk_io": io_rate, "net": net,
        })
        prev_net, prev_io, prev_t = cur_net, io, now


threading.Thread(target=sampler, daemon=True).start()


@app.get("/api/stats")
def stats():
    return state


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))
