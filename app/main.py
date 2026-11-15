import glob, os, re, shlex, shutil, subprocess, threading, time
import psutil
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

HOST = os.environ.get("HOST_ROOT", "/hostfs")
INTERVAL = float(os.environ.get("INTERVAL", "2"))
REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "vfat", "exfat", "ntfs", "ntfs3",
            "f2fs", "fuseblk", "nfs", "nfs4", "cifs"}
SKIP_NIC = ("veth", "br-", "docker", "lo")
DRM = os.environ.get("DRM_PATH", "/sys/class/drm")

app = FastAPI()

# CORS middleware - allow requests from dashboard origin
dashboard_origin = os.environ.get("DASHBOARD_ORIGIN", "*")  # Default to wildcard for flexibility
app.add_middleware(
    CORSMiddleware,
    allow_origins=[dashboard_origin] if dashboard_origin != "*" else ["*"],
    allow_credentials=dashboard_origin != "*",
    allow_methods=["*"],
    allow_headers=["*"],
)
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


def nvidia_gpus():
    if not shutil.which("nvidia-smi"):
        return []
    q = "index,name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,power.limit,fan.speed,clocks.sm,clocks.mem"
    try:
        out = subprocess.check_output(
            ["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"], text=True, timeout=5)
        procs = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    keys = ["index", "name", "temp", "util", "vram_used", "vram_total", "power", "power_limit", "fan_pct", "clk_sm", "clk_mem"]
    res = []
    for line in out.strip().splitlines():
        g = dict(zip(keys, [x.strip() for x in line.split(",")]))
        for k in keys[2:]:
            g[k] = num(g[k])
        g["vendor"] = "NVIDIA"
        res.append(g)
    apps = []
    for line in procs.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) == 4:
            apps.append({"pid": p[1], "name": p[2].split()[0], "vram": num(p[3])})
    if res:
        res[0]["procs"] = apps  # not mapped per GPU; shown once
    return res


def pci_name(slot):
    try:
        out = subprocess.check_output(["lspci", "-mm", "-s", slot], text=True, timeout=3)
        return shlex.split(out.splitlines()[0])[3]
    except Exception:
        return None


def scaled(path, div, nd=1):
    v = num(read(path).strip())
    return None if v is None else round(v / div, nd)


def amd_gpus():
    """AMD (amdgpu driver) via sysfs only: no ROCm or extra devices needed."""
    res = []
    for dev in sorted(glob.glob(f"{DRM}/card*/device")):
        if not re.fullmatch(r"card\d+", os.path.basename(os.path.dirname(dev))):
            continue
        if read(f"{dev}/vendor").strip() != "0x1002" or not os.path.exists(f"{dev}/mem_info_vram_total"):
            continue
        hw = (glob.glob(f"{dev}/hwmon/hwmon*") or [None])[0]
        slot = next((l.split("=", 1)[1] for l in read(f"{dev}/uevent").splitlines()
                     if l.startswith("PCI_SLOT_NAME=")), "")
        h = (lambda f, d, nd=1: scaled(f"{hw}/{f}", d, nd)) if hw else (lambda *a, **k: None)
        power = h("power1_average", 1e6) if hw and os.path.exists(f"{hw}/power1_average") else h("power1_input", 1e6)
        res.append({
            "vendor": "AMD", "index": re.search(r"card(\d+)", dev).group(1),
            "name": read(f"{dev}/product_name").strip() or (pci_name(slot) if slot else None) or "AMD GPU",
            "temp": h("temp1_input", 1000, 0), "util": scaled(f"{dev}/gpu_busy_percent", 1, 0),
            "vram_used": scaled(f"{dev}/mem_info_vram_used", 1048576, 0),
            "vram_total": scaled(f"{dev}/mem_info_vram_total", 1048576, 0),
            "power": power, "power_limit": h("power1_cap", 1e6),
            "fan_pct": None, "fan_rpm": h("fan1_input", 1, 0),
            "clk_sm": h("freq1_input", 1e6, 0), "clk_mem": h("freq2_input", 1e6, 0),
        })
    return res


def gpus():
    return nvidia_gpus() + amd_gpus()


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
    try:
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
    except Exception as e:
        print(f"Sampler error: {e}")


threading.Thread(target=sampler, daemon=True).start()


@app.get("/api/stats")
def stats():
    # Return current state directly - sampler already sets ready=True and all fields
    return state


@app.get("/dashboard")
def dashboard():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "dashboard.html"))

app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))
