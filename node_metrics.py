"""
node_metrics.py

One node's health as JSON, for the app's Server screen. Standard library
only, because the same file is piped to `python3 -` over SSH to collect
the other cluster nodes too.

Fields that cannot be read on a node (no docker CLI, no systemd, no
thermal sensor, ...) are null rather than missing.

    python3 node_metrics.py            # prints JSON
    MONITOR_UNITS=nginx,ssh python3 node_metrics.py
"""

import json
import os
import shutil
import socket
import subprocess
import time

REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "vfat", "exfat", "f2fs", "ntfs", "fuseblk"}


def _read(path):
    with open(path) as f:
        return f.read()


def _cpu_times():
    fields = [int(x) for x in _read("/proc/stat").splitlines()[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields), idle


def cpu_percent(interval=0.3):
    t1, i1 = _cpu_times()
    time.sleep(interval)
    t2, i2 = _cpu_times()
    total = t2 - t1
    return round(100.0 * (1 - (i2 - i1) / total), 1) if total > 0 else None


def memory():
    info = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        info[key] = int(rest.split()[0])  # kB
    total = info["MemTotal"]
    used = total - info.get("MemAvailable", info.get("MemFree", 0))
    return {
        "total_mb": total // 1024,
        "used_mb": used // 1024,
        "percent": round(100.0 * used / total, 1) if total else 0.0,
        "swap_total_mb": info.get("SwapTotal", 0) // 1024,
        "swap_used_mb": (info.get("SwapTotal", 0) - info.get("SwapFree", 0)) // 1024,
    }


def disks(roots=None):
    """Real filesystems from /proc/mounts. `roots` maps mount -> path to
    stat instead (the executor sees the host's root under /host)."""
    out, seen = [], set()
    entries = roots.items() if roots else [
        (line.split()[1], line.split()[1])
        for line in _read("/proc/mounts").splitlines()
        if len(line.split()) > 2 and line.split()[2] in REAL_FS
    ]
    for mount, path in entries:
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        if (st.f_blocks, st.f_bfree) in seen or st.f_blocks == 0:
            continue  # same filesystem mounted twice (bind mounts)
        seen.add((st.f_blocks, st.f_bfree))
        total = st.f_blocks * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        usable = used + st.f_bavail * st.f_frsize  # without root-reserved blocks, like df
        out.append({
            "mount": mount,
            "total_gb": round(total / 1e9, 1),
            "used_gb": round(used / 1e9, 1),
            "percent": round(100.0 * used / usable, 1) if usable else 100.0,
        })
    return out


def temperature():
    best = None
    base = "/sys/class/thermal"
    if not os.path.isdir(base):
        return None
    for zone in os.listdir(base):
        try:
            value = int(_read(os.path.join(base, zone, "temp")).strip()) / 1000.0
        except (OSError, ValueError):
            continue
        if 0 < value < 150:
            best = value if best is None else max(best, value)
    return round(best, 1) if best is not None else None


def network():
    rx = tx = 0
    for line in _read("/proc/net/dev").splitlines()[2:]:
        name, _, data = line.partition(":")
        name = name.strip()
        if name == "lo" or name.startswith(("veth", "docker", "br-")):
            continue
        parts = data.split()
        rx += int(parts[0])
        tx += int(parts[8])
    return {"rx_mb": round(rx / 1e6, 1), "tx_mb": round(tx / 1e6, 1)}


def docker_containers():
    if not shutil.which("docker"):
        return None
    try:
        proc = subprocess.run(["docker", "ps", "-a", "--format", "{{json .}}"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = []
    for line in proc.stdout.splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        out.append({"name": c.get("Names", ""), "state": c.get("State", ""),
                    "status": c.get("Status", ""), "image": c.get("Image", "")})
    return out


def systemd_units(names):
    if not names or not shutil.which("systemctl"):
        return None
    out = []
    for unit in names:
        try:
            proc = subprocess.run(["systemctl", "show", unit, "-p", "ActiveState", "-p", "SubState"],
                                  capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        props = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
        active = props.get("ActiveState", "unknown")
        out.append({"unit": unit, "active": active, "sub": props.get("SubState", ""),
                    "ok": active == "active"})
    return out


def collect(node="local", hostname=None, disk_roots=None, net=True, units=None):
    return {
        "node": node,
        "ok": True,
        "error": None,
        "hostname": hostname or socket.gethostname(),
        "cpu_percent": cpu_percent(),
        "cpu_count": os.cpu_count() or 0,
        "load": list(os.getloadavg()),
        "memory": memory(),
        "disks": disks(disk_roots),
        "uptime_s": int(float(_read("/proc/uptime").split()[0])),
        "temp_c": temperature(),
        "net": network() if net else None,
        "docker": docker_containers(),
        "units": systemd_units(units),
    }


if __name__ == "__main__":
    units = [u for u in os.environ.get("MONITOR_UNITS", "").split(",") if u]
    print(json.dumps(collect(node=os.environ.get("NODE_NAME", "local"), units=units)))
