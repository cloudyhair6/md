# GOG Game Disk Monitor & Auto-Launcher

[![Python Version](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)](https://microsoft.com/windows)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A Windows background application that monitors removable drives, optical CD/DVD discs, and simulated virtual drives (`subst`). When a newly inserted drive contains a GOG game configuration descriptor (`gog_game.json`), the application reads the configuration, extracts the game's custom icon, and either prompts the user to install the game or automatically launches it if already installed on the PC.

---

## Architecture & Data Flow

```
[ Drive Inserted / subst X: ]
              │
              ▼
    [ DriveMonitor (ctypes) ] ── (detects drive bitmask + subst / DOS device target)
              │
              ▼
    [ GOGDiskParser ] ── (reads & validates X:\gog_game.json + icon)
              │
              ▼
    [ LocalStateManager ] ── (queries %APPDATA%\GOGDiskMonitor\installed_games.json)
              │
      ┌───────┴────────────────────────┐
      ▼ (Not Installed)                ▼ (Already Installed)
[ InstallPromptDialog ]         [ GameLauncher ]
  (displays custom icon)          (launches game exe detached)
      │
  (User Accepts)
      │
      ▼
[ SetupRunner ] ── (executes setup.exe on disk, waits for exit code 0)
      │
      ▼
[ LocalStateManager ] ── (commits game_id -> installed state atomically)
      │
      ▼
[ System Tray Notification ]
```

---

## Features

- **R1: Background Drive Monitoring & System Tray Daemon**
  - Runs in the Windows system tray (`pystray` + `Pillow`) with dynamic menus and tooltips.
  - Efficient Win32 `kernel32` logical drive bitmask polling (500ms default) with zero CPU overhead.
  - Windows `SetErrorMode` critical popup suppression (prevents "No disk in drive" modal dialogs).
  - Supports physical USB thumb drives, optical CD/DVD drives, and virtual `subst` mapped drives.

- **R2: GOG Game Disk Specification & Config Discovery**
  - Scans newly mounted drives for `gog_game.json` (or `gog_disk.json`).
  - High-DPI custom icon discovery (`.ico` / `.png`) from disk root.

- **R3: Installation Prompt & State Management**
  - High-DPI Tkinter modal dialog showing game title, publisher, version, drive letter, and custom game icon.
  - Executes installer executable from disk with exit code verification.
  - Thread-safe persistent JSON state store (`%APPDATA%\GOGDiskMonitor\installed_games.json`) with atomic write-temp-rename and automatic corruption recovery.

- **R4: Automated Game Launching**
  - When an installed game's disk is inserted, the monitor automatically launches the game binary detached (`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`) without prompting.

---

## Disk Specification Schema (`gog_game.json`)

To create an autorun-enabled GOG disk, place a `gog_game.json` file in the root directory of the disc or drive:

```json
{
  "game_id": "cyberpunk_2077",
  "title": "Cyberpunk 2077",
  "version": "2.12",
  "setup": {
    "executable": "setup.exe",
    "arguments": ["/SILENT"],
    "default_install_subdir": "Cyberpunk2077",
    "estimated_size_mb": 70000
  },
  "launcher": {
    "executable": "bin/x64/Cyberpunk2077.exe",
    "arguments": ["--fullscreen"],
    "working_directory": "bin/x64"
  },
  "icon_path": "game.ico",
  "publisher": "CD PROJEKT RED",
  "disk_info": {
    "disk_number": 1,
    "total_disks": 1,
    "label": "CYBERPUNK_DISC1"
  }
}
```

---

## Installation & Requirements

### Requirements
- Windows 10 / 11 (or Windows 7 / 8 with Python 3.8+)
- Python 3.8+

### Setup
```bash
# Clone or navigate to the repository
cd disks

# Install dependencies
pip install -r requirements.txt

# (Optional) Install package in editable development mode
pip install -e .
```

---

## Usage

### 1. Running the System Tray Monitor (Standard Mode)
```bash
python -m gog_disk_monitor.cli
# or if installed via pip:
gog-disk-monitor
```

### 2. Running in Headless Mode (Background Service)
```bash
python -m gog_disk_monitor.cli --headless
```

### 3. Single Synchronous Drive Scan
```bash
python -m gog_disk_monitor.cli --scan-once
```

### 4. GOG Game Disk Generator & Setup Utility (PyQt / PySide GUI)
Create and deploy autorun-ready GOG disk media (game executable, custom icon, and JSON descriptor):
```bash
python GUI_setup.py
# or if installed via pip:
gog-disk-setup
```

### 5. Querying & Managing Installed Games
```bash
# List all recorded game installations
python -m gog_disk_monitor.cli --list-installed

# Remove / unmark a game record
python -m gog_disk_monitor.cli --unmark cyberpunk_2077
```

### CLI Reference
| Option | Description |
|---|---|
| `--headless` | Run without system tray icon or GUI prompt dialogs. |
| `--scan-once` | Perform a single synchronous scan of all logical drives and exit. |
| `--state-file PATH` | Custom path to `installed_games.json` persistent state file. |
| `--poll-interval SEC` | Polling interval in seconds (default: 0.5s). |
| `--auto-confirm` | Automatically accept and install detected GOG game discs without prompting. |
| `--auto-reject` | Automatically reject detected GOG game installation prompts. |
| `--scan-startup` | Scan already mounted drives immediately upon monitor startup. |
| `--install-root PATH` | Base directory for installed game folders (default: `C:\GOG Games`). |
| `--list-installed` | List all installed games recorded in the state store. |
| `--unmark GAME_ID` | Unmark / remove an installed game from the state store. |
| `-v, --verbose` | Enable debug logging. |

---

## Verification & Simulation Harness

For testing without physical DVD drives, the application includes a complete simulation harness using Windows virtual `subst` drives.

### Automated End-to-End Verification
Runs full non-interactive verification across First Insertion (Install Flow), Drive Unmount/Remount, and Second Insertion (Auto-Launch Flow):
```bash
python verify_simulation.py --auto
```

### Interactive Verification (Live GUI Prompt Dialogs)
Prompts the human user with the live Tkinter modal dialog showing the custom `.ico` game icon:
```bash
python verify_simulation.py --interactive
```

### Manual Testing with Windows `subst`
You can manually mount and unmount any mock game folder as a virtual drive:
```cmd
REM 1. Mount a mock game folder as drive Z:
subst Z: "C:\path\to\my_mock_gog_disk"

REM 2. Unmount drive Z:
subst Z: /d
```

---

## Running the Automated Test Suite

The test suite covers 5 tiers of testing (152 comprehensive unit, integration, and E2E test cases):

```bash
python -m unittest discover -s tests -v
```

### Test Coverage Hierarchy
- **Tier 1 (Feature Unit Tests)**: Schema parsing, state persistence, error mode suppression, UI dialog bindings, process execution, tray menus.
- **Tier 2 (Boundary & Corner Cases)**: Malformed JSON, corrupted state recovery, concurrent multithreading, unicode paths, missing files.
- **Tier 3 (Cross-Feature Integration)**: App coordinator pipeline, CLI commands, tray notifications.
- **Tier 4 (E2E `subst` Scenarios)**: Real Windows `subst` disk mount, installation flow, unmount/remount auto-launch, prompt decline, setup failure handling.
- **Tier 5 (Adversarial & Live Loop Hardening)**: Multi-disk concurrency, background monitor real-time event detection, persistence across process restarts.

---

## License

This project is licensed under the MIT License - see the LICENSE file for details.
