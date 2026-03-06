#!/usr/bin/env python3
"""
Mac Battery Monitor — track battery drain and identify power-hungry apps.

Monitors battery usage from high charge (≥80%) down to low (<20%),
logging per-process energy impact, CPU, RAM, and system metrics every
sampling interval. Stores data in SQLite and can export to CSV.

Usage:
    sudo python3 battery_monitor.py start          # begin monitoring
    python3 battery_monitor.py status               # current battery info
    python3 battery_monitor.py sessions             # list recorded sessions
    python3 battery_monitor.py report [SESSION_ID]  # detailed drain report
    python3 battery_monitor.py export [SESSION_ID]  # export to CSV
    sudo python3 battery_monitor.py install-service # install launchd daemon
    sudo python3 battery_monitor.py remove-service  # remove launchd daemon
"""

import argparse
import csv
import datetime
import logging
import os
import plistlib
import re
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_DIR = Path.home() / ".battery-monitor"
DB_PATH = DATA_DIR / "battery.db"
LOG_PATH = DATA_DIR / "monitor.log"
PID_FILE = DATA_DIR / "monitor.pid"

DEFAULT_INTERVAL_SEC = 600  # 10 minutes
POWERMETRICS_SAMPLE_MS = 5000  # 5-second sample for powermetrics
TOP_N_PROCESSES = 30

SESSION_START_THRESHOLD = 80  # % — start tracking when at or above this
SESSION_END_THRESHOLD = 20  # % — end tracking when below this

LAUNCHD_LABEL = "com.user.battery-monitor"
LAUNCHD_PLIST = Path("/Library/LaunchDaemons") / f"{LAUNCHD_LABEL}.plist"

logger = logging.getLogger("battery-monitor")

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def setup_logging(verbose: bool = False):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    if not logger.handlers:
        fh = logging.FileHandler(LOG_PATH)
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)

    logger.setLevel(level)
    logger.propagate = False


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time      TEXT NOT NULL,
            start_level     INTEGER NOT NULL,
            end_time        TEXT,
            end_level       INTEGER,
            duration_min    REAL,
            status          TEXT DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id          INTEGER,
            timestamp           TEXT NOT NULL,
            battery_pct         INTEGER,
            temperature_c       REAL,
            voltage_mv          INTEGER,
            amperage_ma         INTEGER,
            power_draw_w        REAL,
            cycle_count         INTEGER,
            raw_capacity_mah    INTEGER,
            raw_max_capacity_mah INTEGER,
            design_capacity_mah INTEGER,
            is_charging         INTEGER,
            time_remaining_min  INTEGER,
            ext_connected       INTEGER,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE TABLE IF NOT EXISTS processes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id     INTEGER NOT NULL,
            pid             INTEGER,
            name            TEXT,
            cpu_pct         REAL,
            mem_mb          REAL,
            energy_impact   REAL,
            gpu_ms_per_s    REAL,
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
        );
        CREATE INDEX IF NOT EXISTS idx_snap_session ON snapshots(session_id);
        CREATE INDEX IF NOT EXISTS idx_proc_snap    ON processes(snapshot_id);
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Data collection helpers
# ---------------------------------------------------------------------------


SQLITE_INT_MAX = 2**63 - 1

def _parse_ioreg_value(raw: str):
    """Extract the value portion after '=' from an ioreg line."""
    val = raw.split("=", 1)[-1].strip()
    if val.lower() in ("yes", "true"):
        return True
    if val.lower() in ("no", "false"):
        return False
    try:
        n = int(val)
        # macOS ioreg reports some signed fields (e.g. Amperage) as unsigned
        # 64-bit ints when negative. Convert back to signed.
        if n > SQLITE_INT_MAX:
            n -= 2**64
        return n
    except ValueError:
        return val


def get_battery_info() -> dict:
    """Return a dict of battery metrics from ioreg."""
    try:
        raw = subprocess.check_output(
            ["ioreg", "-r", "-c", "AppleSmartBattery", "-d", "1"],
            text=True, timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        logger.warning("Failed to read ioreg battery data")
        return {}

    keys_of_interest = {
        "CurrentCapacity", "MaxCapacity",
        "AppleRawCurrentCapacity", "AppleRawMaxCapacity",
        "DesignCapacity", "CycleCount", "Temperature",
        "Voltage", "Amperage", "InstantAmperage",
        "IsCharging", "ExternalConnected",
        "TimeRemaining", "AvgTimeToEmpty", "AvgTimeToFull",
    }
    info = {}
    for line in raw.splitlines():
        for key in keys_of_interest:
            if f'"{key}"' in line:
                info[key] = _parse_ioreg_value(line)
                break

    # Derived values
    temp_raw = info.get("Temperature")
    if isinstance(temp_raw, int):
        info["temperature_c"] = temp_raw / 100.0

    voltage = info.get("Voltage", 0)
    amperage = info.get("Amperage", 0)
    if voltage and amperage:
        info["power_draw_w"] = abs(voltage * amperage) / 1_000_000.0

    return info


def get_battery_pct() -> int:
    """Quick battery percentage via pmset."""
    try:
        out = subprocess.check_output(["pmset", "-g", "batt"], text=True, timeout=5)
        m = re.search(r"(\d+)%", out)
        if m:
            return int(m.group(1))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    return -1


def get_top_processes() -> list:
    """Top processes by CPU from ps (always available, no sudo)."""
    try:
        out = subprocess.check_output(
            ["ps", "-arcwwxo", "pid,pcpu,rss,comm"],
            text=True, timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []

    procs = []
    for line in out.strip().splitlines()[1:TOP_N_PROCESSES + 1]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        procs.append({
            "pid": int(parts[0]),
            "cpu_pct": float(parts[1]),
            "mem_mb": round(float(parts[2]) / 1024.0, 1),
            "name": parts[3].rsplit("/", 1)[-1],
            "energy_impact": 0.0,
            "gpu_ms_per_s": 0.0,
        })
    return procs


def get_powermetrics_energy() -> dict:
    """Per-process energy impact via powermetrics (requires sudo).

    Returns {process_name: {"energy": float, "gpu_ms": float}}.
    """
    try:
        raw = subprocess.check_output(
            ["sudo", "-n", "powermetrics",
             "--samplers", "tasks",
             "-n", "1",
             "-i", str(POWERMETRICS_SAMPLE_MS),
             "-f", "plist"],
            timeout=POWERMETRICS_SAMPLE_MS / 1000 + 15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, PermissionError):
        logger.debug("powermetrics unavailable — using ps data only")
        return {}

    try:
        data = plistlib.loads(raw)
    except Exception:
        logger.debug("Failed to parse powermetrics plist output")
        return {}

    result = {}
    for task in data.get("tasks", []):
        name = task.get("name", "unknown")
        energy = task.get("energy_impact_per_s", task.get("energy_impact", 0)) or 0
        gpu = task.get("gputime_ms_per_s", 0) or 0
        existing = result.get(name)
        if existing:
            existing["energy"] += energy
            existing["gpu_ms"] = max(existing["gpu_ms"], gpu)
        else:
            result[name] = {"energy": energy, "gpu_ms": gpu}
    return result


def collect_snapshot() -> dict:
    """Collect one complete snapshot of battery + process metrics."""
    battery = get_battery_info()
    procs = get_top_processes()

    energy_map = get_powermetrics_energy()
    if energy_map:
        for p in procs:
            em = energy_map.get(p["name"])
            if em:
                p["energy_impact"] = em["energy"]
                p["gpu_ms_per_s"] = em["gpu_ms"]

    battery_pct = battery.get("CurrentCapacity", get_battery_pct())
    is_charging = 1 if battery.get("IsCharging") else 0
    ext_connected = 1 if battery.get("ExternalConnected") else 0

    time_remaining = battery.get("AvgTimeToEmpty", -1)
    if time_remaining == 65535:
        time_remaining = -1

    return {
        "battery_pct": battery_pct,
        "temperature_c": battery.get("temperature_c"),
        "voltage_mv": battery.get("Voltage"),
        "amperage_ma": battery.get("Amperage"),
        "power_draw_w": battery.get("power_draw_w"),
        "cycle_count": battery.get("CycleCount"),
        "raw_capacity_mah": battery.get("AppleRawCurrentCapacity"),
        "raw_max_capacity_mah": battery.get("AppleRawMaxCapacity"),
        "design_capacity_mah": battery.get("DesignCapacity"),
        "is_charging": is_charging,
        "time_remaining_min": time_remaining,
        "ext_connected": ext_connected,
        "processes": procs,
    }


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


def get_active_session(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM sessions WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def start_session(conn: sqlite3.Connection, level: int) -> int:
    now = datetime.datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO sessions (start_time, start_level, status) VALUES (?, ?, 'active')",
        (now, level),
    )
    conn.commit()
    logger.info("Session %d started at %d%%", cur.lastrowid, level)
    return cur.lastrowid


def end_session(conn: sqlite3.Connection, session_id: int, level: int):
    now = datetime.datetime.now().isoformat()
    row = conn.execute("SELECT start_time FROM sessions WHERE id = ?", (session_id,)).fetchone()
    duration = None
    if row:
        start = datetime.datetime.fromisoformat(row["start_time"])
        duration = (datetime.datetime.now() - start).total_seconds() / 60.0
    conn.execute(
        "UPDATE sessions SET end_time=?, end_level=?, duration_min=?, status='completed' WHERE id=?",
        (now, level, duration, session_id),
    )
    conn.commit()
    logger.info("Session %d ended at %d%% (%.0f min)", session_id, level, duration or 0)


def save_snapshot(conn: sqlite3.Connection, session_id: int, snap: dict) -> int:
    now = datetime.datetime.now().isoformat()
    cur = conn.execute("""
        INSERT INTO snapshots
            (session_id, timestamp, battery_pct, temperature_c, voltage_mv,
             amperage_ma, power_draw_w, cycle_count, raw_capacity_mah,
             raw_max_capacity_mah, design_capacity_mah, is_charging,
             time_remaining_min, ext_connected)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        session_id, now, snap["battery_pct"], snap["temperature_c"],
        snap["voltage_mv"], snap["amperage_ma"], snap["power_draw_w"],
        snap["cycle_count"], snap["raw_capacity_mah"],
        snap["raw_max_capacity_mah"], snap["design_capacity_mah"],
        snap["is_charging"], snap["time_remaining_min"], snap["ext_connected"],
    ))
    snap_id = cur.lastrowid

    for p in snap["processes"]:
        conn.execute("""
            INSERT INTO processes (snapshot_id, pid, name, cpu_pct, mem_mb, energy_impact, gpu_ms_per_s)
            VALUES (?,?,?,?,?,?,?)
        """, (snap_id, p["pid"], p["name"], p["cpu_pct"], p["mem_mb"],
              p["energy_impact"], p["gpu_ms_per_s"]))

    conn.commit()
    return snap_id


# ---------------------------------------------------------------------------
# Monitoring loop
# ---------------------------------------------------------------------------


_running = True


def _handle_signal(signum, frame):
    global _running
    logger.info("Received signal %d — stopping", signum)
    _running = False


def cmd_start(args):
    """Main monitoring loop."""
    setup_logging(args.verbose)
    interval = args.interval

    # Write PID file
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    conn = get_db()

    # Check for sudo availability for powermetrics
    has_sudo = False
    try:
        subprocess.check_call(
            ["sudo", "-n", "true"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5,
        )
        has_sudo = True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass

    if has_sudo:
        logger.info("Running with sudo — powermetrics energy data enabled")
    else:
        logger.warning(
            "sudo not available (run with sudo for energy-impact data). "
            "Falling back to ps-based CPU/memory metrics only."
        )

    logger.info("Battery monitor started (interval=%ds, pid=%d)", interval, os.getpid())
    logger.info("Data stored in %s", DB_PATH)

    session = get_active_session(conn)
    session_id = session["id"] if session else None

    while _running:
        try:
            snap = collect_snapshot()
            pct = snap["battery_pct"]
            charging = snap["is_charging"]

            # Session lifecycle
            if session_id is None:
                if pct >= SESSION_START_THRESHOLD and not charging:
                    session_id = start_session(conn, pct)
                elif pct >= SESSION_START_THRESHOLD and charging:
                    logger.info("Battery at %d%% but charging — waiting for unplug to start session", pct)
                else:
                    logger.info("Battery at %d%% — monitoring (no active session, waiting for ≥%d%%)", pct, SESSION_START_THRESHOLD)
            else:
                if pct < SESSION_END_THRESHOLD:
                    end_session(conn, session_id, pct)
                    session_id = None
                    logger.info("Session ended — battery below %d%%", SESSION_END_THRESHOLD)
                elif charging:
                    end_session(conn, session_id, pct)
                    session_id = None
                    logger.info("Session ended — charger connected at %d%%", pct)

            snap_id = save_snapshot(conn, session_id, snap)

            top3 = sorted(snap["processes"], key=lambda p: p["energy_impact"] or p["cpu_pct"], reverse=True)[:3]
            top3_str = ", ".join(f'{p["name"]}({p["cpu_pct"]:.0f}%cpu)' for p in top3)
            logger.info(
                "Snapshot #%d — %d%% %s | %.1f W | %.1f°C | top: %s",
                snap_id, pct,
                "⚡charging" if charging else "🔋discharging",
                snap.get("power_draw_w") or 0,
                snap.get("temperature_c") or 0,
                top3_str,
            )

        except Exception:
            logger.exception("Error during snapshot collection")

        # Sleep in small increments so we can respond to signals
        deadline = time.time() + interval
        while _running and time.time() < deadline:
            time.sleep(min(2, deadline - time.time()))

    # Clean up
    if session_id is not None:
        pct = get_battery_pct()
        end_session(conn, session_id, pct)

    conn.close()
    PID_FILE.unlink(missing_ok=True)
    logger.info("Monitor stopped")


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def cmd_status(args):
    """Show current battery status."""
    snap = collect_snapshot()
    pct = snap["battery_pct"]
    charging = snap["is_charging"]
    ext = snap["ext_connected"]

    print(f"\n{'─' * 52}")
    print(f"  Battery Level     : {pct}% {'⚡ charging' if charging else '🔋 discharging'}")
    print(f"  Power Adapter     : {'Connected' if ext else 'Not connected'}")
    if snap["power_draw_w"]:
        print(f"  Power Draw        : {snap['power_draw_w']:.1f} W")
    if snap["temperature_c"]:
        print(f"  Temperature       : {snap['temperature_c']:.1f}°C")
    if snap["voltage_mv"]:
        print(f"  Voltage           : {snap['voltage_mv']} mV")
    if snap["amperage_ma"] is not None:
        print(f"  Amperage          : {snap['amperage_ma']} mA")
    if snap["cycle_count"]:
        print(f"  Cycle Count       : {snap['cycle_count']}")
    if snap["raw_capacity_mah"] and snap["raw_max_capacity_mah"]:
        health = 100.0 * snap["raw_max_capacity_mah"] / snap["design_capacity_mah"] if snap["design_capacity_mah"] else 0
        print(f"  Capacity          : {snap['raw_capacity_mah']} / {snap['raw_max_capacity_mah']} mAh")
        print(f"  Design Capacity   : {snap['design_capacity_mah']} mAh")
        print(f"  Battery Health    : {health:.1f}%")
    if snap["time_remaining_min"] and snap["time_remaining_min"] > 0:
        h, m = divmod(snap["time_remaining_min"], 60)
        print(f"  Time Remaining    : {h}h {m}m")
    print(f"{'─' * 52}")

    # Active session?
    conn = get_db()
    session = get_active_session(conn)
    if session:
        print(f"  Active Session    : #{session['id']} (started at {session['start_level']}%)")
    else:
        print("  Active Session    : None")

    # Running daemon?
    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        print(f"  Monitor PID       : {pid}")
    else:
        print("  Monitor           : Not running")
    print(f"{'─' * 52}\n")
    conn.close()


def cmd_sessions(args):
    """List all recorded sessions."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM sessions ORDER BY id DESC LIMIT 20"
    ).fetchall()
    conn.close()

    if not rows:
        print("\nNo sessions recorded yet. Run 'start' to begin monitoring.\n")
        return

    print(f"\n{'ID':>4}  {'Status':<10}  {'Start':>19}  {'Start%':>6}  {'End%':>5}  {'Duration':>10}")
    print("─" * 68)
    for r in rows:
        start = r["start_time"][:19] if r["start_time"] else "—"
        dur = f"{r['duration_min']:.0f} min" if r["duration_min"] else "—"
        end_lvl = f"{r['end_level']}%" if r["end_level"] is not None else "—"
        print(f"{r['id']:>4}  {r['status']:<10}  {start:>19}  {r['start_level']:>5}%  {end_lvl:>5}  {dur:>10}")
    print()


def cmd_report(args):
    """Detailed report for a session."""
    conn = get_db()

    if args.session_id:
        session_id = args.session_id
    else:
        row = conn.execute(
            "SELECT id FROM sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            print("\nNo sessions found.\n")
            conn.close()
            return
        session_id = row["id"]

    session = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not session:
        print(f"\nSession #{session_id} not found.\n")
        conn.close()
        return

    snapshots = conn.execute(
        "SELECT * FROM snapshots WHERE session_id = ? ORDER BY timestamp", (session_id,)
    ).fetchall()

    # Aggregate process data across all snapshots in this session
    snap_ids = [s["id"] for s in snapshots]
    if not snap_ids:
        print(f"\nSession #{session_id} has no snapshots.\n")
        conn.close()
        return

    placeholders = ",".join("?" * len(snap_ids))
    proc_rows = conn.execute(f"""
        SELECT name,
               COUNT(*)         AS appearances,
               AVG(cpu_pct)     AS avg_cpu,
               MAX(cpu_pct)     AS max_cpu,
               AVG(mem_mb)      AS avg_mem,
               MAX(mem_mb)      AS max_mem,
               SUM(energy_impact) AS total_energy,
               AVG(energy_impact) AS avg_energy,
               MAX(gpu_ms_per_s)  AS max_gpu_ms
        FROM processes
        WHERE snapshot_id IN ({placeholders})
        GROUP BY name
        ORDER BY total_energy DESC, avg_cpu DESC
        LIMIT 25
    """, snap_ids).fetchall()
    conn.close()

    # --- Print report ---
    print(f"\n{'═' * 70}")
    print(f"  BATTERY DRAIN REPORT — Session #{session_id}")
    print(f"{'═' * 70}")

    start_t = session["start_time"][:19] if session["start_time"] else "?"
    end_t = session["end_time"][:19] if session["end_time"] else "ongoing"
    dur = f"{session['duration_min']:.0f} min" if session["duration_min"] else "ongoing"
    start_lvl = session["start_level"]
    end_lvl = session["end_level"] if session["end_level"] is not None else "?"
    drain = (start_lvl - session["end_level"]) if session["end_level"] is not None else "?"

    print(f"\n  Period     : {start_t}  →  {end_t}")
    print(f"  Duration   : {dur}")
    print(f"  Battery    : {start_lvl}%  →  {end_lvl}%  (drained {drain}%)")
    if session["duration_min"] and session["end_level"] is not None:
        rate = (start_lvl - session["end_level"]) / (session["duration_min"] / 60)
        print(f"  Drain Rate : {rate:.1f}% per hour")

    # Battery timeline
    print(f"\n  {'─' * 48}")
    print(f"  BATTERY TIMELINE")
    print(f"  {'─' * 48}")
    print(f"  {'Time':>19}  {'Batt%':>5}  {'Power':>7}  {'Temp':>6}  {'State':<12}")
    for s in snapshots:
        ts = s["timestamp"][:19] if s["timestamp"] else "?"
        pwr = f"{s['power_draw_w']:.1f}W" if s["power_draw_w"] else "—"
        temp = f"{s['temperature_c']:.1f}°" if s["temperature_c"] else "—"
        state = "charging" if s["is_charging"] else "discharging"
        print(f"  {ts:>19}  {s['battery_pct']:>4}%  {pwr:>7}  {temp:>6}  {state:<12}")

    # Top energy consumers
    print(f"\n  {'─' * 66}")
    print(f"  TOP ENERGY CONSUMERS (aggregated across {len(snapshots)} snapshots)")
    print(f"  {'─' * 66}")
    print(f"  {'#':>3}  {'Process':<25} {'ΣEnergy':>8} {'AvgCPU':>7} {'MaxCPU':>7} {'AvgMem':>8} {'MaxGPU':>8}")
    for i, p in enumerate(proc_rows[:15], 1):
        energy_str = f"{p['total_energy']:.1f}" if p["total_energy"] else "—"
        gpu_str = f"{p['max_gpu_ms']:.1f}ms" if p["max_gpu_ms"] else "—"
        print(
            f"  {i:>3}  {p['name']:<25} {energy_str:>8} "
            f"{p['avg_cpu']:>6.1f}% {p['max_cpu']:>6.1f}% "
            f"{p['avg_mem']:>6.0f}MB {gpu_str:>8}"
        )

    # Top RAM consumers
    ram_sorted = sorted(proc_rows, key=lambda p: p["avg_mem"] or 0, reverse=True)
    print(f"\n  {'─' * 50}")
    print(f"  TOP RAM CONSUMERS")
    print(f"  {'─' * 50}")
    print(f"  {'#':>3}  {'Process':<25} {'AvgMem':>8} {'MaxMem':>8}")
    for i, p in enumerate(ram_sorted[:10], 1):
        print(f"  {i:>3}  {p['name']:<25} {p['avg_mem']:>6.0f}MB {p['max_mem']:>6.0f}MB")

    # Battery health (from first snapshot)
    if snapshots:
        s0 = snapshots[0]
        if s0["raw_max_capacity_mah"] and s0["design_capacity_mah"]:
            health = 100.0 * s0["raw_max_capacity_mah"] / s0["design_capacity_mah"]
            print(f"\n  {'─' * 40}")
            print(f"  BATTERY HEALTH")
            print(f"  {'─' * 40}")
            print(f"  Cycle Count     : {s0['cycle_count']}")
            print(f"  Max Capacity    : {s0['raw_max_capacity_mah']} mAh")
            print(f"  Design Capacity : {s0['design_capacity_mah']} mAh")
            print(f"  Health          : {health:.1f}%")

    print(f"\n{'═' * 70}\n")


def cmd_export(args):
    """Export session data to CSV files."""
    conn = get_db()

    if args.session_id:
        session_id = args.session_id
    else:
        row = conn.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            print("\nNo sessions found.\n")
            conn.close()
            return
        session_id = row["id"]

    out_dir = Path(args.output) if args.output else DATA_DIR / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Export snapshots
    snapshots = conn.execute(
        "SELECT * FROM snapshots WHERE session_id = ? ORDER BY timestamp", (session_id,)
    ).fetchall()

    snap_file = out_dir / f"session_{session_id}_snapshots.csv"
    with open(snap_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([desc[0] for desc in conn.execute(
            "SELECT * FROM snapshots LIMIT 0").description])
        for s in snapshots:
            writer.writerow(tuple(s))
    print(f"Snapshots  → {snap_file}")

    # Export process data
    snap_ids = [s["id"] for s in snapshots]
    if snap_ids:
        placeholders = ",".join("?" * len(snap_ids))
        procs = conn.execute(f"""
            SELECT p.*, s.timestamp, s.battery_pct
            FROM processes p
            JOIN snapshots s ON s.id = p.snapshot_id
            WHERE p.snapshot_id IN ({placeholders})
            ORDER BY s.timestamp, p.energy_impact DESC
        """, snap_ids).fetchall()

        proc_file = out_dir / f"session_{session_id}_processes.csv"
        with open(proc_file, "w", newline="") as f:
            writer = csv.writer(f)
            if procs:
                writer.writerow(procs[0].keys())
                for p in procs:
                    writer.writerow(tuple(p))
        print(f"Processes  → {proc_file}")

    conn.close()
    print(f"\nExported session #{session_id} to {out_dir}/\n")


def cmd_stop(args):
    """Stop a running monitor daemon."""
    if not PID_FILE.exists():
        print("No running monitor found.")
        return

    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to monitor (PID {pid})")
    except ProcessLookupError:
        print(f"Monitor process {pid} not found — cleaning up stale PID file")
        PID_FILE.unlink(missing_ok=True)
    except PermissionError:
        print(f"Permission denied sending signal to PID {pid}. Try: sudo python3 {__file__} stop")


def cmd_install_service(args):
    """Install launchd daemon for auto-start monitoring."""
    script_path = str(Path(__file__).resolve())
    python_path = sys.executable

    plist_content = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [python_path, script_path, "start", "--interval", str(args.interval)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
    }

    if os.geteuid() != 0:
        print("Installing the launchd daemon requires root. Run with sudo.")
        sys.exit(1)

    with open(LAUNCHD_PLIST, "wb") as f:
        plistlib.dump(plist_content, f)

    os.chmod(LAUNCHD_PLIST, 0o644)
    subprocess.run(["launchctl", "load", str(LAUNCHD_PLIST)], check=True)
    print(f"Installed and loaded {LAUNCHD_LABEL}")
    print(f"Plist: {LAUNCHD_PLIST}")
    print(f"Logs:  {LOG_PATH}")
    print(f"\nTo uninstall: sudo python3 {script_path} remove-service")


def cmd_remove_service(args):
    """Remove launchd daemon."""
    if os.geteuid() != 0:
        print("Removing the launchd daemon requires root. Run with sudo.")
        sys.exit(1)

    if LAUNCHD_PLIST.exists():
        subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST)], check=False)
        LAUNCHD_PLIST.unlink()
        print(f"Removed {LAUNCHD_LABEL}")
    else:
        print("Launchd daemon not installed.")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="battery_monitor",
        description="Mac Battery Monitor — track battery drain and find power-hungry apps",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              sudo python3 battery_monitor.py start           # monitor with defaults
              sudo python3 battery_monitor.py start -i 300    # sample every 5 min
              python3 battery_monitor.py status                # current battery info
              python3 battery_monitor.py sessions              # list sessions
              python3 battery_monitor.py report                # latest session report
              python3 battery_monitor.py report 3              # report for session #3
              python3 battery_monitor.py export                # export latest to CSV
              python3 battery_monitor.py export 3 -o ./data    # export session #3
              sudo python3 battery_monitor.py install-service  # auto-start on boot
              sudo python3 battery_monitor.py remove-service   # remove auto-start
        """),
    )

    sub = parser.add_subparsers(dest="command", help="command to run")

    p_start = sub.add_parser("start", help="start monitoring battery")
    p_start.add_argument("-i", "--interval", type=int, default=DEFAULT_INTERVAL_SEC,
                         help=f"sampling interval in seconds (default: {DEFAULT_INTERVAL_SEC})")
    p_start.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    sub.add_parser("status", help="show current battery status")
    sub.add_parser("sessions", help="list recorded sessions")
    sub.add_parser("stop", help="stop running monitor")

    p_report = sub.add_parser("report", help="detailed drain report for a session")
    p_report.add_argument("session_id", nargs="?", type=int, help="session ID (default: latest)")

    p_export = sub.add_parser("export", help="export session data to CSV")
    p_export.add_argument("session_id", nargs="?", type=int, help="session ID (default: latest)")
    p_export.add_argument("-o", "--output", help="output directory (default: ~/.battery-monitor/exports/)")

    p_install = sub.add_parser("install-service", help="install launchd daemon")
    p_install.add_argument("-i", "--interval", type=int, default=DEFAULT_INTERVAL_SEC,
                           help=f"sampling interval in seconds (default: {DEFAULT_INTERVAL_SEC})")

    sub.add_parser("remove-service", help="remove launchd daemon")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "start": cmd_start,
        "status": cmd_status,
        "sessions": cmd_sessions,
        "report": cmd_report,
        "export": cmd_export,
        "stop": cmd_stop,
        "install-service": cmd_install_service,
        "remove-service": cmd_remove_service,
    }

    fn = dispatch.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
