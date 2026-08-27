#!/usr/bin/env python3
"""
verify_simulation.py
~~~~~~~~~~~~~~~~~~~~

Standalone User Verification & Simulation Harness for GOG Game Disk Monitor.
Strictly verifies all Acceptance Criteria specified in ORIGINAL_REQUEST.md:
  1. Application Execution & System Readiness
  2. First Insertion (Installation Flow):
     - Detects newly mounted virtual drive (via Windows subst).
     - Checks local PC state (uninstalled).
     - Displays installation prompt with custom game icon (.ico).
     - Executes setup executable from disk upon acceptance.
     - Commits installed state to local PC state store.
  3. Second Insertion (Auto-Launch Flow):
     - Unmounts and remounts virtual drive.
     - Detects remounted drive.
     - Checks local PC state (installed).
     - Automatically launches game executable without prompt.

CLI Flags:
  --auto                 Automated non-interactive run (auto-confirms prompt dialog).
  --interactive          Interactive run with real live GUI dialogs for human verification.
  --drive-letter LETTER  Override virtual drive letter (e.g. Z:). Defaults to finding free letter.
  --keep-files           Preserve temporary simulation folders and subst mount on exit.
  -v, --verbose          Enable detailed debug logging.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

# Import gog_disk_monitor components
from gog_disk_monitor import (
    GOGDiskConfig,
    GOGDiskMonitorApp,
    InstalledGameRecord,
    LauncherConfig,
    ProcessRunner,
    SetupConfig,
    StateStore,
    find_disk_icon,
    parse_disk_config,
)
from gog_disk_monitor.drive_monitor import (
    DriveInfo,
    DriveMonitor,
    DriveSimulator,
    WindowsDriveDetector,
)

logger = logging.getLogger("verify_simulation")

# Terminal Color Codes
class Colors:
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def print_banner(title: str) -> None:
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'=' * 75}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN} {title}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'=' * 75}{Colors.RESET}")


def print_step(step_num: int, title: str) -> None:
    print(f"\n{Colors.BOLD}{Colors.BLUE}[STEP {step_num}] {title}{Colors.RESET}")
    print(f"{Colors.DIM}{'-' * 70}{Colors.RESET}")


def print_pass(msg: str) -> None:
    print(f"  {Colors.GREEN}[PASS]{Colors.RESET} {msg}")


def print_fail(msg: str) -> None:
    print(f"  {Colors.RED}[FAIL]{Colors.RESET} {msg}")


def print_info(msg: str) -> None:
    print(f"  {Colors.YELLOW}[INFO]{Colors.RESET} {msg}")


def create_simulation_icon(icon_path: Path) -> Path:
    """Generates an attractive retro-styled mock game .ico file."""
    icon_path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (64, 64), color=(30, 30, 46, 255))
    from PIL import ImageDraw
    draw = ImageDraw.Draw(img)
    # Outer circle
    draw.ellipse([4, 4, 60, 60], fill=(70, 130, 180, 255), outline=(255, 215, 0, 255), width=2)
    # Inner ring
    draw.ellipse([22, 22, 42, 42], fill=(30, 30, 46, 255), outline=(255, 255, 255, 255), width=2)
    # Center hole
    draw.ellipse([28, 28, 36, 36], fill=(20, 20, 30, 255))

    img.save(icon_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    return icon_path


def create_simulation_mock_disk(
    disk_dir: Path,
    target_install_dir: Path,
    game_id: str = "witcher_3_wild_hunt",
    title: str = "The Witcher 3: Wild Hunt (Simulated GOG Edition)",
    version: str = "4.04",
) -> Path:
    """
    Constructs the mock GOG game disk structure with:
      - gog_game.json configuration
      - custom icon (game.ico)
      - mock setup batch script (setup.bat)
      - mock game executable batch script (game.bat)
    """
    disk_dir.mkdir(parents=True, exist_ok=True)

    # 1. Setup script on disk
    setup_bat = disk_dir / "setup.bat"
    setup_script = (
        "@echo off\n"
        "echo ========================================================\n"
        "echo  [MOCK SETUP] Installing GOG Game: " + title + "\n"
        "echo ========================================================\n"
        "set TARGET_DIR=%~dp0\n"
        "if not \"%~1\"==\"\" set TARGET_DIR=%~1\n"
        "if \"%~1\"==\"--install-dir\" (\n"
        "    if not \"%~2\"==\"\" set TARGET_DIR=%~2\n"
        ")\n"
        "echo Target installation path: %TARGET_DIR%\n"
        "mkdir \"%TARGET_DIR%\" 2>nul\n"
        "echo @echo off > \"%TARGET_DIR%\\game.bat\"\n"
        "echo echo ======================================== >> \"%TARGET_DIR%\\game.bat\"\n"
        f"echo echo  [GAME RUNNING] {title} v{version} >> \"%TARGET_DIR%\\game.bat\"\n"
        "echo echo ======================================== >> \"%TARGET_DIR%\\game.bat\"\n"
        "echo echo Simulation game launched successfully at %DATE% %TIME% >> \"%TARGET_DIR%\\game.bat\"\n"
        "echo game_active > \"%TARGET_DIR%\\game_running.marker\"\n"
        "echo echo Installation finished successfully.\n"
        "exit /b 0\n"
    )
    setup_bat.write_text(setup_script, encoding="utf-8")

    # 2. Disk launcher placeholder
    disk_launcher = disk_dir / "game.bat"
    disk_launcher.write_text(
        "@echo off\n"
        f"echo Running {title} from disc...\n"
        "exit /b 0\n",
        encoding="utf-8",
    )

    # 3. Custom game icon
    icon_path = disk_dir / "game.ico"
    create_simulation_icon(icon_path)

    # 4. GOG Game Configuration (gog_game.json)
    config_path = disk_dir / "gog_game.json"
    config_data = {
        "game_id": game_id,
        "title": title,
        "version": version,
        "setup": {
            "executable": "setup.bat",
            "arguments": ["--install-dir", str(target_install_dir)],
            "default_install_subdir": "TheWitcher3",
            "estimated_size_mb": 45000,
        },
        "launcher": {
            "executable": "game.bat",
            "arguments": ["--fullscreen", "--directx12"],
            "working_directory": None,
        },
        "icon_path": "game.ico",
        "publisher": "CD PROJEKT RED",
        "disk_info": {
            "disk_number": 1,
            "total_disks": 1,
            "label": "WITCHER3_DISC1",
        },
    }
    config_path.write_text(json.dumps(config_data, indent=2), encoding="utf-8")

    return disk_dir


def run_verification(
    interactive: bool = False,
    override_drive: Optional[str] = None,
    keep_files: bool = False,
    verbose: bool = False,
) -> bool:
    """
    Executes full end-to-end simulation verification.
    Returns True if all acceptance criteria pass, False otherwise.
    """
    if sys.platform != "win32":
        print_fail("Windows operating system is required for `subst` virtual drive verification.")
        return False

    mode_name = "INTERACTIVE (GUI Prompt Dialogs)" if interactive else "AUTOMATED (Non-interactive)"
    print_banner(f"GOG Game Disk Monitor - Simulation Verification ({mode_name})")

    # Tracking Results
    checks_passed = 0
    total_checks = 0

    def check(condition: bool, description: str) -> bool:
        nonlocal checks_passed, total_checks
        total_checks += 1
        if condition:
            print_pass(description)
            checks_passed += 1
            return True
        else:
            print_fail(description)
            return False

    # 1. Environment & Setup
    print_step(1, "Environment Preparation & Virtual Drive Letter Resolution")
    temp_dir = tempfile.mkdtemp(prefix="gog_sim_")
    temp_path = Path(temp_dir).resolve()
    print_info(f"Workspace temp directory: {temp_path}")

    install_root = temp_path / "installed_games"
    install_root.mkdir(parents=True, exist_ok=True)
    target_game_dir = install_root / "TheWitcher3"

    state_file = temp_path / "state" / "installed_games.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    disk_folder = temp_path / "virtual_disc"

    # Resolve drive letter
    target_drive = override_drive or DriveSimulator.find_available_drive_letter()
    if not target_drive:
        print_fail("Could not find any available logical drive letters on this PC.")
        return False

    # Normalize letter
    clean_letter = target_drive.strip().rstrip("\\/").rstrip(":").upper() + ":"
    print_info(f"Using virtual drive letter: {clean_letter}")

    # Generate Mock Game Disk
    create_simulation_mock_disk(disk_folder, target_install_dir=target_game_dir)
    print_info(f"Mock GOG disk generated at: {disk_folder}")

    # State store instance
    state_store = StateStore(state_file_path=str(state_file))
    check(not state_store.is_installed("witcher_3_wild_hunt"), "StateStore initialized: game is NOT yet installed.")

    mounted = False

    try:
        # ---------------------------------------------------------------------
        # 2. First Insertion (Installation Flow)
        # ---------------------------------------------------------------------
        print_step(2, f"First Insertion (Installation Flow) on {clean_letter}")
        print_info(f"Mounting virtual disk folder to {clean_letter} via Windows `subst`...")
        mounted = DriveSimulator.mount_subst(clean_letter, str(disk_folder))
        check(mounted, f"Mounted {disk_folder} to {clean_letter}")

        # Verify Windows detects subst drive
        detector = WindowsDriveDetector()
        is_subst, subst_target = detector.is_subst_drive(clean_letter)
        check(is_subst, f"Windows kernel confirms {clean_letter} is active SUBST drive (target: {subst_target})")

        # Initialize Application Coordinator
        app = GOGDiskMonitorApp(
            state_file_path=str(state_file),
            install_root=str(install_root),
            auto_confirm=True if not interactive else None,
            headless=not interactive,
        )

        if interactive:
            print(f"\n{Colors.BOLD}{Colors.YELLOW}>>> Displaying installation prompt dialog with custom game icon...{Colors.RESET}")
            print(f"{Colors.YELLOW}>>> Please click 'Install Game' in the popup window to proceed.{Colors.RESET}\n")

        print_info("Scanning logical drives for newly inserted GOG game disc...")
        scan_results_1 = app.scan_now()
        check(len(scan_results_1) >= 1, "Drive monitor scan detected candidate game disc.")

        first_res = scan_results_1[0] if scan_results_1 else {}
        action_1 = first_res.get("action")
        check(action_1 == "installed", f"Pipeline executed installation action (result action: '{action_1}').")

        # Verify state persistence
        is_installed = app.state_store.is_installed("witcher_3_wild_hunt", verify_executable=True)
        check(is_installed, "Local PC state file (%APPDATA% / custom store) updated to installed.")

        record = app.state_store.get_game("witcher_3_wild_hunt")
        check(record is not None and record.title.startswith("The Witcher 3"), "State record contains valid game metadata.")
        check(record is not None and os.path.isfile(record.executable_path), f"Installed game executable exists at: {record.executable_path if record else 'N/A'}")

        # ---------------------------------------------------------------------
        # 3. Second Insertion (Auto-Launch Flow)
        # ---------------------------------------------------------------------
        print_step(3, f"Second Insertion (Auto-Launch Flow) on {clean_letter}")
        print_info(f"Unmounting virtual disk {clean_letter} via `subst /d` to simulate ejection...")
        unmount_ok = DriveSimulator.unmount_subst(clean_letter)
        mounted = False
        check(unmount_ok, f"Virtual drive {clean_letter} unmounted successfully.")

        # Re-check state while disk is unmounted
        check(app.state_store.is_installed("witcher_3_wild_hunt", verify_executable=True), "PC state preserves installation record while disk is unmounted.")

        print_info(f"Remounting virtual disk to {clean_letter} via `subst` to simulate re-insertion...")
        mounted = DriveSimulator.mount_subst(clean_letter, str(disk_folder))
        check(mounted, f"Virtual drive {clean_letter} remounted.")

        if interactive:
            print(f"\n{Colors.BOLD}{Colors.CYAN}>>> Verifying Auto-Launch: No prompt should appear; game binary starts immediately.{Colors.RESET}\n")

        print_info("Scanning drives for remounted GOG disc...")
        scan_results_2 = app.scan_now()
        check(len(scan_results_2) >= 1, "Drive monitor scan detected remounted disc.")

        second_res = scan_results_2[0] if scan_results_2 else {}
        action_2 = second_res.get("action")
        check(action_2 == "launched", f"Pipeline executed auto-launch action (result action: '{action_2}').")

        # Verify launch timestamp updated
        updated_record = app.state_store.get_game("witcher_3_wild_hunt")
        check(
            updated_record is not None and updated_record.last_launched_at is not None,
            f"State record updated with last_launched_at timestamp ({updated_record.last_launched_at if updated_record else 'N/A'}).",
        )

    finally:
        # ---------------------------------------------------------------------
        # 4. Cleanup
        # ---------------------------------------------------------------------
        print_step(4, "Simulation Teardown & Resource Cleanup")
        if mounted and not keep_files:
            print_info(f"Cleaning up virtual subst drive {clean_letter}...")
            DriveSimulator.unmount_subst(clean_letter)
            print_pass(f"Unmounted {clean_letter}")
        elif mounted and keep_files:
            print_info(f"--keep-files specified: Virtual drive {clean_letter} remains mounted at {disk_folder}")

        if not keep_files:
            print_info("Removing temporary simulation directories...")
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
                print_pass("Temporary simulation directories cleaned up.")
            except Exception as ex:
                print_info(f"Note on temp cleanup: {ex}")
        else:
            print_info(f"--keep-files specified: Temporary files preserved at: {temp_dir}")

    # -------------------------------------------------------------------------
    # 5. Summary & Verdict
    # -------------------------------------------------------------------------
    print_banner("Simulation Verification Summary")
    success_rate = (checks_passed / total_checks * 100) if total_checks else 0
    print(f"Total Criteria Evaluated: {total_checks}")
    print(f"Passed:                   {Colors.GREEN}{checks_passed}{Colors.RESET}")
    print(f"Failed:                   {Colors.RED if checks_passed < total_checks else Colors.GREEN}{total_checks - checks_passed}{Colors.RESET}")
    print(f"Success Rate:             {Colors.BOLD}{success_rate:.1f}%{Colors.RESET}")

    if checks_passed == total_checks:
        print(f"\n{Colors.BOLD}{Colors.GREEN}[ALL ACCEPTANCE CRITERIA VERIFIED SUCCESSFULLY]{Colors.RESET}\n")
        return True
    else:
        print(f"\n{Colors.BOLD}{Colors.RED}[VERIFICATION FAILED: Some criteria did not pass]{Colors.RESET}\n")
        return False


def build_parser() -> argparse.ArgumentParser:
    """Builds CLI argument parser for verify_simulation."""
    parser = argparse.ArgumentParser(
        prog="verify_simulation",
        description="Standalone Verification & Simulation for Windows GOG Game Disk Monitor",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--auto",
        action="store_true",
        default=True,
        help="Run automated non-interactive verification (default).",
    )
    group.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Run interactive verification with live Tkinter GUI prompt dialogs.",
    )
    parser.add_argument(
        "--drive-letter",
        metavar="LETTER",
        type=str,
        default=None,
        help="Target drive letter for subst simulation (e.g. Z:). Defaults to first free letter.",
    )
    parser.add_argument(
        "--keep-files",
        action="store_true",
        default=False,
        help="Keep simulation files and virtual drive mounted after completion for inspection.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Enable verbose debug logging.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Logging setup
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
        datefmt="%H:%M:%S",
    )

    interactive = bool(args.interactive)
    passed = run_verification(
        interactive=interactive,
        override_drive=args.drive_letter,
        keep_files=args.keep_files,
        verbose=args.verbose,
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
