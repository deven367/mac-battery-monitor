# Mac Battery Monitor

A single-file Python script that tracks macOS battery drain and identifies power-hungry apps. No external dependencies — uses only the Python standard library and built-in macOS tools.

## What It Tracks

Every sampling interval (default: 10 minutes), the monitor records:

| Category | Metrics |
|---|---|
| **Battery** | Level %, temperature, voltage, amperage, power draw (W), cycle count, capacity (mAh) |
| **Per-process** | CPU %, memory (MB), energy impact score, GPU time (ms/s) |
| **Session** | Drain from ≥80% → <20%, duration, drain rate (%/hr) |

Energy impact and GPU metrics require `sudo` (uses `powermetrics`). Without sudo, CPU and memory are still tracked via `ps`.

## Quick Start

```bash
# Check current battery status (no sudo needed)
python3 battery_monitor.py status

# Start monitoring (sudo recommended for energy-impact data)
sudo python3 battery_monitor.py start

# In another terminal, check progress
python3 battery_monitor.py sessions
python3 battery_monitor.py report
```

## Commands

| Command | Description | Needs sudo? |
|---|---|---|
| `start` | Begin monitoring (foreground) | Recommended |
| `status` | Show current battery info | No |
| `sessions` | List all recorded sessions | No |
| `report [ID]` | Detailed drain report for a session | No |
| `export [ID]` | Export session data to CSV | No |
| `stop` | Stop a running monitor | Same as start |
| `install-service` | Auto-start on boot via launchd | Yes |
| `remove-service` | Remove launchd auto-start | Yes |

## Options

```bash
# Custom sampling interval (seconds)
sudo python3 battery_monitor.py start -i 300    # every 5 minutes
sudo python3 battery_monitor.py start -i 60     # every 1 minute

# Verbose/debug logging
sudo python3 battery_monitor.py start -v

# Export a specific session to a custom directory
python3 battery_monitor.py export 3 -o ./my-data

# Report for a specific session
python3 battery_monitor.py report 3
```

## Session Tracking

A **session** represents one discharge cycle:

- **Starts** when battery is ≥80% and the charger is disconnected
- **Ends** when battery drops below 20% or the charger is plugged back in
- Snapshots are always recorded regardless of session state

## Auto-Start (launchd)

To have the monitor start automatically on boot:

```bash
sudo python3 battery_monitor.py install-service
```

This installs a LaunchDaemon at `/Library/LaunchDaemons/com.user.battery-monitor.plist`.

To remove:

```bash
sudo python3 battery_monitor.py remove-service
```

## Data Storage

All data is stored in `~/.battery-monitor/`:

```
~/.battery-monitor/
├── battery.db          # SQLite database
├── monitor.log         # Log file
├── monitor.pid         # PID file (when running)
└── exports/            # CSV exports
    ├── session_1_snapshots.csv
    └── session_1_processes.csv
```

You can query the SQLite database directly:

```bash
sqlite3 ~/.battery-monitor/battery.db "SELECT * FROM snapshots ORDER BY timestamp DESC LIMIT 5;"
```

## Sample Report Output

```
══════════════════════════════════════════════════════════════════════
  BATTERY DRAIN REPORT — Session #1
══════════════════════════════════════════════════════════════════════

  Period     : 2026-03-06T10:00:00  →  2026-03-06T16:30:00
  Duration   : 390 min
  Battery    : 95%  →  18%  (drained 77%)
  Drain Rate : 11.8% per hour

  ────────────────────────────────────────────────
  TOP ENERGY CONSUMERS (aggregated across 39 snapshots)
  ────────────────────────────────────────────────
    #  Process                    ΣEnergy  AvgCPU  MaxCPU    AvgMem   MaxGPU
    1  Google Chrome Helper         892.3   12.4%   45.2%    512MB   8.2ms
    2  WindowServer                 445.1    5.2%   18.0%    210MB  12.5ms
    3  kernel_task                  312.8    3.1%   22.0%      0MB      —
  ...
```

## Requirements

- macOS (tested on 15.x Sequoia)
- Python 3.9+ (ships with macOS)
- `sudo` access (recommended, for per-process energy impact via `powermetrics`)
