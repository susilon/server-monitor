# Server Monitor

A small self-hosted dashboard that shows the live status of the **host** machine it runs on, even though the app itself runs inside a Docker container.

- CPU: total and per-core usage, frequency, load average
- Memory: used, available, swap
- GPU: NVIDIA (via `nvidia-smi`) and AMD (via `/sys`), including VRAM, temperature, power, fan and clocks
- Temperatures: every sensor the kernel exposes (CPU, NVMe, motherboard, GPU, and so on)
- Disks: usage, available space and capacity per mounted filesystem, plus total read/write speed
- Network: per-interface address, link state, speed, throughput, totals, errors and drops

The server works on machines with an NVIDIA GPU, an AMD GPU, or no GPU at all. The GPU section is hidden when no supported GPU is found.

## How it works

```
Browser  --polls every 2 s-->  GET /api/stats  -->  FastAPI + psutil (container)
                                                      |-- /proc, /sys            (CPU, memory, sensors, AMD GPU)
                                                      |-- /hostfs (host / :ro)   (real disk list and usage)
                                                      |-- host network namespace (NIC counters)
                                                      '-- nvidia-smi             (NVIDIA GPU, optional)
```

A background thread samples the host every `INTERVAL` seconds (default 2) and keeps the latest result in memory. Network and disk speeds are calculated from the difference between two samples. Nothing is written to disk and there is no database.

To see the host instead of the container, `docker-compose.yml` uses:

| Setting | Why |
|---|---|
| `network_mode: host` | Reads the host's network interfaces and counters. The UI is served directly on the host's port. |
| `pid: host` | Gives a host-wide view of CPU and processes. |
| `cap_add: SYS_PTRACE` | Lets the app read the host's `/proc/1/mounts` to list the real disks. |
| `/:/hostfs:ro,rslave` | Mounts the host filesystem read-only so disk usage reflects the host, not Docker's overlay. |

## Requirements

- Linux host with Docker and Docker Compose v2
- NVIDIA GPU only: [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/) installed on the host
- AMD GPU: the `amdgpu` kernel driver (the default on modern Linux). No ROCm needed.

## Quick start

### Server with no GPU, or with an AMD GPU

```bash
docker compose up -d --build
```

### Server with an NVIDIA GPU

```bash
docker compose -f docker-compose.nvidia.yml up -d --build
```

Use one file or the other, not both. Then open `http://<server-ip>:8787`.

To stop it: `docker compose down` (add `-f docker-compose.nvidia.yml` if you used that file).

## Configuration

Set these as environment variables (a `.env` file next to the compose file, or Coolify's Environment Variables tab).

| Variable | Default | Description |
|---|---|---|
| `MONITOR_PORT` | `8787` | Port the dashboard listens on. Because the container uses host networking, this must be free on the host. |
| `INTERVAL` | `2` | Seconds between samples. The browser polls every 2 seconds regardless. |
| `HOST_ROOT` | `/hostfs` | Where the host filesystem is mounted inside the container. Change only if you change the volume. |
| `DRM_PATH` | `/sys/class/drm` | Where AMD GPUs are read from. Mainly useful for testing. |
| `DASHBOARD_ORIGIN` | `*` | CORS origin for dashboard-to-server requests. Set to your dashboard URL (e.g., `http://100.97.13.76:8787`) for production, or keep `*` for development. |

## Deploying on Coolify

1. Create a new resource of type **Docker Compose** from your Git repository.
2. Set **Docker Compose Location** to `/docker-compose.yml`, or `/docker-compose.nvidia.yml` on NVIDIA servers.
3. Add `MONITOR_PORT` under Environment Variables if 8787 is taken.
4. Deploy, then open `http://<server-ip>:<MONITOR_PORT>`.

Notes:
- Host networking bypasses Coolify's proxy, so a domain set in the Coolify UI will not route to this app. Use the IP and port, or add your own Traefik/Caddy route to `host-ip:port`.
- Coolify may rewrite or ignore `pid: host` or `cap_add`. If the disk list is empty or the container fails to start, check the deployment log.
- Docker Swarm does not support `pid: host` or `network_mode: host`, so run this with plain Docker Compose, not as a Swarm service.

## API

`GET /api/stats` returns the latest sample as JSON. It returns `{"ready": false}` for the first couple of seconds after start. Useful keys: `hostname`, `uptime`, `cpu`, `mem`, `gpus`, `temps`, `disks`, `disk_io`, `net`. You can use it for your own scripts or alerting.

## Project structure

```
server-monitor/
├── docker-compose.yml          # no GPU or AMD GPU
├── docker-compose.nvidia.yml   # NVIDIA GPU
├── Dockerfile
└── app/
    ├── main.py                 # FastAPI backend and host sampling
    └── static/index.html       # single-file dashboard (no build step)
```

## Security

- **There is no authentication.** The dashboard exposes hardware details, IP addresses, mount points and (on NVIDIA) GPU process names. Keep it on a private network such as Tailscale or your LAN, firewall the port, or put it behind a reverse proxy with authentication.
- The container runs as root with the host filesystem mounted read-only, the host PID namespace, the host network, and `SYS_PTRACE`. This access is what makes host monitoring possible, but it is broad. Do not expose the container to the internet, and do not add other services to it.
- The app only reads data. It does not write to the host filesystem or run commands from user input.
- **CORS Configuration:** The backend allows cross-origin requests from the dashboard. In production, set `DASHBOARD_ORIGIN` to your dashboard's exact origin (e.g., `http://100.97.13.76:8787`) instead of the wildcard `*`. This prevents unauthorized servers from accessing your monitoring data.

## Troubleshooting

| Problem | Likely cause and fix |
|---|---|
| Port already in use | Another service has the port. Check with `ss -tlnp \| grep <port>` and change `MONITOR_PORT`. |
| `could not select device driver "nvidia"` | You used the NVIDIA file on a server without nvidia-container-toolkit, or without an NVIDIA GPU. Use `docker-compose.yml` or install the toolkit. |
| GPU section missing on an NVIDIA server | You are using `docker-compose.yml` instead of `docker-compose.nvidia.yml`, or `nvidia-smi` fails on the host. |
| GPU section missing on an AMD server | The card is not using `amdgpu`, or `/sys/class/drm/card*/device/mem_info_vram_total` does not exist on the host. |
| Few or no temperatures | The kernel is not exposing sensors. On the host, run `sudo sensors-detect` (lm-sensors) and load the suggested modules. |
| Disk list empty or shows container disks | `pid: host`, `cap_add: SYS_PTRACE` or the `/hostfs` mount was removed or ignored by your platform. |
| Wrong network numbers | `network_mode: host` is not applied, so you are seeing the container's own network. |

## Limitations

- Linux only.
- AMD support covers the `amdgpu` driver. Intel GPUs are not supported.
- On AMD integrated graphics (APUs), VRAM is a small reserved slice of system RAM and does not reflect total graphics memory.
- NVIDIA process names are listed once, not per GPU.
- Available sensors, labels and limits depend on the hardware and kernel drivers.
- There is no history, alerting or persistence. Graphs show roughly the last two minutes in the open browser tab.

## Disclaimer

This software is provided **"as is"**, without warranty of any kind, express or implied. Use it at your own risk. The authors are not liable for any damage, data loss, downtime or security incident arising from its use.

- Readings come from the operating system and hardware drivers and may be inaccurate, delayed or missing. Do not rely on this dashboard for safety-critical decisions, thermal protection, or as your only monitoring.
- The default configuration grants the container broad read access to the host. Review it against your own security requirements before deploying, especially on shared or production servers.
- It has not been tested on every hardware or distribution combination.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
