#!/usr/bin/env python3
"""
WattHog Backend
Handles power source detection (Battery or RAPL) and process power impact scoring.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psutil


WEIGHT_CPU = 1.0
WEIGHT_CTX_SWITCHES = 0.3
WEIGHT_IO = 0.2

CTX_SWITCH_NORM = 1000.0
IO_NORM = 50_000_000.0

POWER_SUPPLY_BASE = Path("/sys/class/power_supply")
POWERCAP_BASE = Path("/sys/class/powercap")


@dataclass
class BatteryInfo:
    """Battery status from sysfs."""
    name: str
    status: str
    power_now_watts: float
    energy_now_wh: float
    energy_full_wh: float
    percent: float
    time_remaining_h: Optional[float]

    def __str__(self) -> str:
        lines = [
            f"🔋 Battery: {self.name}",
            f"   Status:            {self.status}",
            f"   Current Power:     {self.power_now_watts:.2f} W",
            f"   Remaining Cap.:    {self.energy_now_wh:.2f} / {self.energy_full_wh:.2f} Wh ({self.percent:.1f} %)",
        ]
        if self.time_remaining_h is not None:
            hours = int(self.time_remaining_h)
            minutes = int((self.time_remaining_h - hours) * 60)
            lines.append(f"   Est. Time Left:    {hours}h {minutes:02d}min")
        else:
            lines.append(f"   Est. Time Left:    N/A")
        return "\n".join(lines)


@dataclass
class ProcessScore:
    """Process with calculated power impact score."""
    pid: int
    name: str
    username: str
    cpu_percent: float
    ctx_switches: int
    io_read_bytes: int
    io_write_bytes: int
    power_score: float

    def __str__(self) -> str:
        return (
            f"  PID {self.pid:<7} | {self.name:<25} | "
            f"User: {self.username:<12} | "
            f"CPU: {self.cpu_percent:5.1f}% | "
            f"CtxSw: {self.ctx_switches:>8} | "
            f"IO: {(self.io_read_bytes + self.io_write_bytes) / 1_000_000:>8.1f} MB | "
            f"⚡ Impact: {self.power_score:.2f}"
        )


@dataclass
class RaplInfo:
    """CPU package power from Intel RAPL."""
    domain: str
    power_watts: float
    has_permission: bool = True
    source: str = "RAPL"

    def __str__(self) -> str:
        return (
            f"⚡ Desktop CPU Power (RAPL): {self.power_watts:.2f} W\n"
            f"   Domain: {self.domain}"
        )


def _read_sysfs_int(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError, PermissionError):
        return None


def _read_sysfs_str(path: Path) -> Optional[str]:
    try:
        return path.read_text().strip()
    except (FileNotFoundError, PermissionError):
        return None


def find_battery_dirs() -> list[Path]:
    if not POWER_SUPPLY_BASE.exists():
        return []
    return sorted(
        p for p in POWER_SUPPLY_BASE.iterdir()
        if p.name.startswith("BAT") and p.is_dir()
    )


def read_battery(bat_path: Path) -> Optional[BatteryInfo]:
    bat_type = _read_sysfs_str(bat_path / "type")
    if bat_type != "Battery":
        return None

    name = bat_path.name
    status = _read_sysfs_str(bat_path / "status") or "Unknown"

    energy_now_uw = _read_sysfs_int(bat_path / "energy_now")
    energy_full_uw = _read_sysfs_int(bat_path / "energy_full")
    power_now_uw = _read_sysfs_int(bat_path / "power_now")

    if energy_now_uw is not None and energy_full_uw is not None:
        energy_now_wh = energy_now_uw / 1_000_000
        energy_full_wh = energy_full_uw / 1_000_000
        power_now_w = (power_now_uw / 1_000_000) if power_now_uw is not None else 0.0
    else:
        charge_now = _read_sysfs_int(bat_path / "charge_now")
        charge_full = _read_sysfs_int(bat_path / "charge_full")
        current_now = _read_sysfs_int(bat_path / "current_now")
        voltage_now = _read_sysfs_int(bat_path / "voltage_now")

        if charge_now is None or charge_full is None or voltage_now is None:
            return None

        voltage_v = voltage_now / 1_000_000
        energy_now_wh = (charge_now / 1_000_000) * voltage_v
        energy_full_wh = (charge_full / 1_000_000) * voltage_v
        power_now_w = ((current_now or 0) / 1_000_000) * voltage_v

    percent = (energy_now_wh / energy_full_wh * 100) if energy_full_wh > 0 else 0.0

    time_remaining_h: Optional[float] = None
    if status == "Discharging" and power_now_w > 0:
        time_remaining_h = energy_now_wh / power_now_w

    return BatteryInfo(
        name=name,
        status=status,
        power_now_watts=power_now_w,
        energy_now_wh=energy_now_wh,
        energy_full_wh=energy_full_wh,
        percent=percent,
        time_remaining_h=time_remaining_h,
    )


def get_battery_info() -> Optional[BatteryInfo]:
    for bat_dir in find_battery_dirs():
        info = read_battery(bat_dir)
        if info is not None:
            return info
    return None


def get_demo_battery_info() -> BatteryInfo:
    return BatteryInfo(
        name="BAT0 (Simulated)",
        status="Discharging",
        power_now_watts=12.45,
        energy_now_wh=38.2,
        energy_full_wh=57.0,
        percent=67.0,
        time_remaining_h=38.2 / 12.45,
    )


def find_rapl_domains() -> list[Path]:
    if not POWERCAP_BASE.exists():
        return []
    results = []
    for p in sorted(POWERCAP_BASE.iterdir()):
        if p.name.startswith("intel-rapl:") and p.name.count(":") == 1:
            results.append(p)
    return results


def read_rapl_power(rapl_path: Path, interval: float = 1.0) -> Optional[RaplInfo]:
    """Reads RAPL energy counters to calculate power over an interval."""
    energy_uj_path = rapl_path / "energy_uj"
    name_path = rapl_path / "name"

    domain_name = _read_sysfs_str(name_path) or rapl_path.name

    try:
        e0 = int(energy_uj_path.read_text().strip())
    except PermissionError:
        return RaplInfo(domain=domain_name, power_watts=0.0, has_permission=False)
    except (FileNotFoundError, ValueError):
        return None

    time.sleep(interval)

    try:
        e1 = int(energy_uj_path.read_text().strip())
    except (PermissionError, FileNotFoundError, ValueError):
        return None

    max_energy = _read_sysfs_int(rapl_path / "max_energy_range_uj")
    delta = e1 - e0
    if delta < 0 and max_energy is not None:
        delta += max_energy
    elif delta < 0:
        return None

    power_w = delta / 1_000_000 / interval

    return RaplInfo(
        domain=domain_name,
        power_watts=round(power_w, 2),
    )


def get_rapl_info() -> Optional[RaplInfo]:
    for rapl_dir in find_rapl_domains():
        info = read_rapl_power(rapl_dir)
        if info is not None:
            return info
    return None


@dataclass
class GpuInfo:
    """Dedicated or integrated GPU power."""
    name: str
    power_watts: float

def get_amd_gpu_power() -> Optional[GpuInfo]:
    hwmon_base = Path("/sys/class/drm/card0/device/hwmon")
    if not hwmon_base.exists():
        return None
        
    for hwmon in hwmon_base.iterdir():
        power_path = hwmon / "power1_average"
        name_path = hwmon / "name"
        if power_path.exists():
            try:
                power_uw = int(power_path.read_text().strip())
                name = name_path.read_text().strip() if name_path.exists() else "AMD GPU"
                return GpuInfo(name=name, power_watts=power_uw / 1_000_000)
            except (ValueError, PermissionError):
                pass
    return None

def get_nvidia_gpu_power() -> Optional[GpuInfo]:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True
        )
        lines = result.stdout.strip().split("\n")
        if lines and lines[0]:
            parts = lines[0].split(", ")
            if len(parts) >= 2:
                name = parts[0]
                power = float(parts[1])
                return GpuInfo(name=name, power_watts=power)
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
        pass
    return None

def get_gpu_info() -> Optional[GpuInfo]:
    amd = get_amd_gpu_power()
    if amd is not None:
        return amd
    return get_nvidia_gpu_power()


def get_power_source(demo: bool = False) -> tuple[Optional[BatteryInfo], Optional[RaplInfo], Optional[GpuInfo]]:
    gpu = get_gpu_info() if not demo else GpuInfo(name="Demo GPU (Nvidia RTX 9090)", power_watts=45.2)

    if demo:
        return get_demo_battery_info(), None, gpu

    battery = get_battery_info()
    if battery is not None:
        return battery, None, gpu

    rapl = get_rapl_info()
    if rapl is not None:
        return None, rapl, gpu

    return None, None, gpu


def compute_power_score(
    cpu_pct: float,
    ctx_switches: int,
    io_bytes: int,
) -> float:
    """Calculates power impact score as a weighted sum of normalized metrics."""
    score = (
        WEIGHT_CPU * cpu_pct
        + WEIGHT_CTX_SWITCHES * (ctx_switches / CTX_SWITCH_NORM) * 100
        + WEIGHT_IO * (io_bytes / IO_NORM) * 100
    )
    return round(score, 2)


def get_top_processes(n: int = 20) -> list[ProcessScore]:
    """Retrieves top process by power impact score using per-second deltas."""
    snapshot: dict[int, dict] = {}
    procs: list[psutil.Process] = []

    for proc in psutil.process_iter(["pid", "name", "username"]):
        try:
            proc.cpu_percent(interval=None)

            ctx = proc.num_ctx_switches()
            ctx_total_0 = (ctx.voluntary or 0) + (ctx.involuntary or 0)

            try:
                io = proc.io_counters()
                io_read_0 = io.read_bytes or 0
                io_write_0 = io.write_bytes or 0
            except (psutil.AccessDenied, AttributeError):
                io_read_0 = 0
                io_write_0 = 0

            snapshot[proc.pid] = {
                "ctx_total_0": ctx_total_0,
                "io_read_0": io_read_0,
                "io_write_0": io_write_0,
            }
            procs.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    time.sleep(1.0)

    results: list[ProcessScore] = []
    for proc in procs:
        try:
            if proc.pid not in snapshot:
                continue
            s0 = snapshot[proc.pid]

            cpu_pct = proc.cpu_percent(interval=None) / (psutil.cpu_count() or 1)

            ctx = proc.num_ctx_switches()
            ctx_total_1 = (ctx.voluntary or 0) + (ctx.involuntary or 0)
            ctx_delta = max(0, ctx_total_1 - s0["ctx_total_0"])

            try:
                io = proc.io_counters()
                io_read_1 = io.read_bytes or 0
                io_write_1 = io.write_bytes or 0
            except (psutil.AccessDenied, AttributeError):
                io_read_1 = s0["io_read_0"]
                io_write_1 = s0["io_write_0"]

            io_read_delta = max(0, io_read_1 - s0["io_read_0"])
            io_write_delta = max(0, io_write_1 - s0["io_write_0"])
            io_delta = io_read_delta + io_write_delta

            info = proc.as_dict(attrs=["pid", "name", "username"])
            score = compute_power_score(cpu_pct, ctx_delta, io_delta)

            results.append(ProcessScore(
                pid=info["pid"],
                name=info["name"] or "<unknown>",
                username=info["username"] or "<unknown>",
                cpu_percent=cpu_pct,
                ctx_switches=ctx_delta,
                io_read_bytes=io_read_delta,
                io_write_bytes=io_write_delta,
                power_score=score,
            ))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    results.sort(key=lambda p: p.power_score, reverse=True)
    return results[:n]


def main() -> None:
    parser = argparse.ArgumentParser(description="WattHog Backend test utility.")
    parser.add_argument("--demo", action="store_true", help="Use simulated data")
    parser.add_argument("-n", "--top", type=int, default=5, help="Number of processes")
    args = parser.parse_args()

    print("======================================================================")
    print("  🐗 WattHog - Backend PoC")
    print("======================================================================")
    print()
    print("── A) Power Source ───────────────────────────────────────────────────")
    print()

    battery, rapl, gpu = get_power_source(demo=args.demo)

    if args.demo and battery is not None:
        print("  ⚠️  Demo Mode Active")
        print()

    if gpu is not None:
        print(f"  🎮 GPU Power: {gpu.name} - {gpu.power_watts:.2f} W")
        print()

    if battery is not None:
        print(battery)
    elif rapl is not None:
        if not rapl.has_permission:
            print("  ❌ Missing RAPL permissions.")
            print("  💡 Tip: Run: sudo ./install.sh")
        else:
            print(rapl)
    else:
        print("  ❌ No power source found.")

    print()
    print(f"── B) Top {args.top} Impact Processes ────────────────────────────────────────")
    print()
    print("  Measuring processes (1s sample) ...")

    top_procs = get_top_processes(n=args.top)

    if not top_procs:
        print("  ❌ Failed to fetch processes.")
    else:
        print()
        print(f"  {'PID':<9} | {'Process':<25} | {'User':<14} | {'CPU %':>7} | {'CtxSw/s':>10} | "
              f"{'I/O (MB/s)':>10} | {'⚡ Impact':>9}")
        print("  " + "─" * 100)
        for p in top_procs:
            print(p)
    print()

    print("── Legend ────────────────────────────────────────────────────────────")
    print(f"  Impact = {WEIGHT_CPU}×CPU% + "
          f"{WEIGHT_CTX_SWITCHES}×(CtxSw_s/{CTX_SWITCH_NORM:.0f})×100 + "
          f"{WEIGHT_IO}×(IO_s/{IO_NORM/1e6:.0f}MB)×100")
    print()


if __name__ == "__main__":
    main()
