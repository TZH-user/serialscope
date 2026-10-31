# -*- coding: utf-8 -*-
"""sys_stats.py — 板端系统状态采样（Linux 页 1Hz 轮询，零外部依赖）。

全部读 /proc 与 /sys；CPU 占用与 eMMC I/O 速率按上次采样的差值计算（模块级
快照 + 锁，首次采样对应字段缺省，PC 端渲染成 "—" 并必须容忍任意缺键）。

命令面：control.py 的 "sys_stats" 命令 → ok_resp(stats=sample())。
纯解析函数（_cpu_times / cpu_usage_percent / parse_meminfo / parse_diskstat /
_root_disk_from_mounts）与 IO 无关，Windows 单测直接覆盖；sample() 本体是
薄 IO 胶水，真机验证走 _verify（1Hz 两次采样差值字段必须出现）。
"""
from __future__ import annotations

import os
import re
import threading
import time

_SECTOR = 512

_LOCK = threading.Lock()
# 差值基准快照：cpu=(total, idle_all)；disk=(read_sectors, write_sectors) + 采样时刻
_PREV: dict = {"cpu": None, "disk": None, "disk_ts": None}


def _read_text(path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


# ---------- 纯解析（与 IO 无关，单测覆盖） ----------

def _cpu_times(line: str):
    """/proc/stat 首行 → (total, idle_all)；idle_all = idle + iowait。"""
    parts = line.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None
    try:
        vals = [int(v) for v in parts[1:]]
    except ValueError:
        return None
    total = sum(vals)
    idle_all = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return total, idle_all


def cpu_usage_percent(cur, prev) -> float | None:
    """两次 (total, idle_all) 快照 → 占用百分比；无前值/无增量/异常快照 → None。"""
    if cur is None or prev is None:
        return None
    d_total = cur[0] - prev[0]
    d_idle = cur[1] - prev[1]
    if d_total <= 0 or d_idle < 0:
        return None
    d_busy = max(0, d_total - d_idle)
    return round(d_busy * 100.0 / d_total, 1)


def parse_meminfo(text: str):
    """→ (total_mb, used_mb)；used = MemTotal − MemAvailable。缺 MemTotal → (None, None)。"""
    total = avail = None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            total = float(line.split()[1])
        elif line.startswith("MemAvailable:"):
            avail = float(line.split()[1])
    if total is None:
        return None, None
    used = total - avail if avail is not None else 0.0
    return total / 1024.0, used / 1024.0  # kB → MB


def parse_diskstat(line: str):
    """/sys/class/block/mmcblkX/stat → (read_sectors, write_sectors)（第 3/7 列）。"""
    parts = line.split()
    if len(parts) < 7:
        return None
    try:
        return int(parts[2]), int(parts[6])
    except ValueError:
        return None


def _root_disk_from_mounts(mounts_text: str) -> str | None:
    """/proc/self/mounts 根分区所在块设备基名（/dev/mmcblk1p2 → mmcblk1）。"""
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "/" and parts[0].startswith("/dev/"):
            m = re.match(r"^(mmcblk\d+)", os.path.basename(parts[0]))
            return m.group(1) if m else None
    return None


def _pick_mmc_device() -> str | None:
    """eMMC 块设备：优先根分区所在盘，否则取最大的 mmcblk 整盘（排除 boot0/1 分区名）。"""
    root = _root_disk_from_mounts(_read_text("/proc/self/mounts") or "")
    if root and os.path.exists(f"/sys/class/block/{root}"):
        return root
    best, best_size = None, -1
    try:
        for name in os.listdir("/sys/class/block"):
            if not re.fullmatch(r"mmcblk\d+", name):
                continue
            size_text = _read_text(f"/sys/class/block/{name}/size")
            try:
                size = int(size_text.strip()) if size_text else 0
            except ValueError:
                size = 0
            if size > best_size:
                best, best_size = name, size
    except OSError:
        return None
    return best


# ---------- 采样（薄 IO 胶水） ----------

def sample() -> dict:
    """采一份系统状态；读不到的字段直接缺省（PC 端容忍缺键）。"""
    out: dict = {}
    with _LOCK:
        prev_cpu = _PREV["cpu"]
        prev_disk = _PREV["disk"]
        prev_ts = _PREV["disk_ts"]

    # CPU：占用 = 两次 /proc/stat 差值；顺带核数
    cur_cpu = None
    disk_snap = None
    stat_text = _read_text("/proc/stat")
    if stat_text:
        lines = stat_text.splitlines()
        if lines:
            cur_cpu = _cpu_times(lines[0])
            cores = sum(1 for ln in lines
                        if ln.startswith("cpu") and ln.split() and ln.split()[0] != "cpu")
            if cores:
                out["cpu_cores"] = cores
    usage = cpu_usage_percent(cur_cpu, prev_cpu)
    if usage is not None:
        out["cpu_percent"] = usage

    # 内存
    mem_text = _read_text("/proc/meminfo")
    if mem_text:
        total_mb, used_mb = parse_meminfo(mem_text)
        if total_mb is not None:
            out["mem_total_mb"] = round(total_mb, 1)
            out["mem_used_mb"] = round(used_mb, 1)

    # 根文件系统容量（eMMC 根分区）
    if hasattr(os, "statvfs"):
        try:
            st = os.statvfs("/")
            out["disk_total_gb"] = round(st.f_blocks * st.f_frsize / 1024 ** 3, 2)
            out["disk_free_gb"] = round(st.f_bavail * st.f_frsize / 1024 ** 3, 2)
        except OSError:
            pass

    # eMMC 块设备：容量 + 累计 I/O + 瞬时速率（差值）
    dev = _pick_mmc_device()
    if dev is not None:
        out["disk_dev"] = dev
        size_text = _read_text(f"/sys/class/block/{dev}/size")
        if size_text:
            try:
                out["disk_size_gb"] = round(int(size_text.strip()) * _SECTOR / 1024 ** 3, 2)
            except ValueError:
                pass
        stat_line = _read_text(f"/sys/class/block/{dev}/stat")
        disk_snap = parse_diskstat(stat_line) if stat_line else None
        if disk_snap is not None:
            out["disk_read_mb"] = round(disk_snap[0] * _SECTOR / 1024 ** 2, 1)
            out["disk_write_mb"] = round(disk_snap[1] * _SECTOR / 1024 ** 2, 1)
            now = time.monotonic()
            if prev_disk is not None and prev_ts is not None and now > prev_ts:
                dt = now - prev_ts
                dr = (disk_snap[0] - prev_disk[0]) * _SECTOR / 1024.0 / dt
                dw = (disk_snap[1] - prev_disk[1]) * _SECTOR / 1024.0 / dt
                if dr >= 0:
                    out["disk_read_kbps"] = round(dr, 1)
                if dw >= 0:
                    out["disk_write_kbps"] = round(dw, 1)

    # 温度：全部 thermal_zone 取最高（SoC 通常最热）
    try:
        temps = []
        for name in os.listdir("/sys/class/thermal"):
            t = _read_text(f"/sys/class/thermal/{name}/temp")
            if t:
                try:
                    temps.append(int(t.strip()) / 1000.0)
                except ValueError:
                    pass
        if temps:
            out["temp_c"] = round(max(temps), 1)
    except OSError:
        pass

    # 运行时长 / 负载
    up = _read_text("/proc/uptime")
    if up:
        try:
            out["uptime_s"] = int(float(up.split()[0]))
        except (ValueError, IndexError):
            pass
    load = _read_text("/proc/loadavg")
    if load:
        try:
            out["load1"] = float(load.split()[0])
        except (ValueError, IndexError):
            pass

    # 快照落位（下一次采样的差值基准）
    with _LOCK:
        _PREV["cpu"] = cur_cpu
        _PREV["disk"] = disk_snap
        _PREV["disk_ts"] = time.monotonic()
    return out
