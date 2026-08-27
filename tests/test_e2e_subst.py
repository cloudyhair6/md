"""
tests.test_e2e_subst
~~~~~~~~~~~~~~~~~~~~

Tier 4 & Tier 5 End-to-End Test Suite using real Windows `subst` virtual drives.
Validates full lifecycle from disk insertion, GOG config parsing, custom icon discovery,
installation prompting, setup execution, state persistence, disk unmount/remount,
and automated game launching without prompting.
"""

from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Set
import unittest

from PIL import Image

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

logger = logging.getLogger("tests.test_e2e_subst")


def create_mock_icon_file(filepath: Path, fmt: str = "ICO", size: tuple = (32, 32)) -> Path:
    """Creates a valid dummy .ico or .png image file."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", size, color=(0, 120, 215, 255))
    if fmt.upper() == "ICO":
        img.save(filepath, format="ICO", sizes=[(32, 32)])
    else:
        img.save(filepath, format="PNG")
    return filepath


def create_mock_game_disk(
    disk_dir: Path,
    game_id: str = "mock_game",
    title: str = "Mock Adventure Quest",
    version: str = "1.0.0",
    setup_script_name: str = "setup.bat",
    launcher_name: str = "game.bat",
    setup_fails: bool = False,
    create_config: bool = True,
    corrupt_config: bool = False,
    icon_filename: Optional[str] = "game.ico",
    custom_setup_args: Optional[List[str]] = None,
    custom_launcher_args: Optional[List[str]] = None,
    default_install_subdir: Optional[str] = None,
) -> Path:
    """
    Constructs a mock GOG game disk folder structure with configuration,
    custom icon, and batch scripts for setup and game launch.
    """
    disk_dir.mkdir(parents=True, exist_ok=True)

    # 1. Setup script
    setup_path = disk_dir / setup_script_name
    if setup_fails:
        setup_content = (
            "@echo off\n"
            "echo [MOCK SETUP] Simulating setup failure with exit code 1...\n"
            "exit /b 1\n"
        )
    else:
        setup_content = (
            "@echo off\n"
            "set OUTDIR=%~dp0\n"
            "if not \"%~1\"==\"\" set OUTDIR=%~1\n"
            "if not \"%~2\"==\"\" (\n"
            "    if \"%~1\"==\"--install-dir\" set OUTDIR=%~2\n"
            ")\n"
            "mkdir \"%OUTDIR%\" 2>nul\n"
            f"echo @echo off > \"%OUTDIR%\\{launcher_name}\"\n"
            f"echo echo [MOCK GAME] Running {title}... >> \"%OUTDIR%\\{launcher_name}\"\n"
            f"echo game_launched > \"%OUTDIR%\\launch_marker.txt\"\n"
            "exit /b 0\n"
        )
    setup_path.write_text(setup_content, encoding="utf-8")

    # 2. Launcher script on disk (source or standalone fallback)
    launcher_path = disk_dir / launcher_name
    launcher_content = (
        "@echo off\n"
        f"echo [MOCK GAME] Running {title} from disk...\n"
        "exit /b 0\n"
    )
    launcher_path.write_text(launcher_content, encoding="utf-8")

    # 3. Custom Icon
    if icon_filename:
        icon_path = disk_dir / icon_filename
        fmt = "ICO" if icon_filename.lower().endswith(".ico") else "PNG"
        create_mock_icon_file(icon_path, fmt=fmt)

    # 4. GOG Game Configuration (gog_game.json)
    if create_config:
        config_path = disk_dir / "gog_game.json"
        if corrupt_config:
            config_path.write_text("{ broken json content: invalid ]]]", encoding="utf-8")
        else:
            config_data = {
                "game_id": game_id,
                "title": title,
                "version": version,
                "setup": {
                    "executable": setup_script_name,
                    "arguments": custom_setup_args or [],
                    "default_install_subdir": default_install_subdir or game_id,
                },
                "launcher": {
                    "executable": launcher_name,
                    "arguments": custom_launcher_args or [],
                },
                "icon_path": icon_filename,
                "publisher": "Simulated GOG Publisher",
            }
            config_path.write_text(json.dumps(config_data, indent=2), encoding="utf-8")

    return disk_dir


@unittest.skipUnless(sys.platform == "win32", "Windows subst command is required for E2E subst tests.")
class TestE2ESubstDrives(unittest.TestCase):
    """
    Tier 4 & Tier 5 E2E test suite running against real Windows `subst` virtual drives.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name).resolve()

        self.install_root = self.temp_path / "installed_games"
        self.install_root.mkdir(parents=True, exist_ok=True)

        self.state_file = self.temp_path / "state" / "installed_games.json"
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        self.mounted_letters: Set[str] = set()

    def tearDown(self):
        # Reliably unmount all subst drives created during tests
        for letter in list(self.mounted_letters):
            try:
                DriveSimulator.unmount_subst(letter)
            except Exception as ex:
                logger.warning("Error unmounting %s during tearDown: %s", letter, ex)

        self.mounted_letters.clear()

        # Clean up temporary test files
        try:
            self.temp_dir.cleanup()
        except Exception as ex:
            logger.debug("Temp dir cleanup warning: %s", ex)

    def mount_virtual_disk(self, disk_folder: Path, preferred_letter: Optional[str] = None) -> str:
        """Helper to find an available drive letter and mount folder via subst."""
        letter = preferred_letter or DriveSimulator.find_available_drive_letter()
        if not letter:
            self.skipTest("No available Windows drive letters found for subst mounting.")

        success = DriveSimulator.mount_subst(letter, str(disk_folder))
        self.assertTrue(success, f"Failed to mount subst drive {letter} -> {disk_folder}")
        self.mounted_letters.add(letter)
        # Brief pause to allow Windows filesystem registration
        time.sleep(0.1)
        return letter

    def unmount_virtual_disk(self, letter: str) -> None:
        """Helper to unmount a subst drive."""
        if letter in self.mounted_letters:
            DriveSimulator.unmount_subst(letter)
            self.mounted_letters.remove(letter)
            time.sleep(0.1)

    # -------------------------------------------------------------------------
    # SCENARIO 1: First Insertion (Fresh Installation Flow)
    # -------------------------------------------------------------------------
    def test_scenario_1_first_insertion_fresh_install(self):
        """
        Scenario 1: First Insertion
        Mount mock GOG disk -> App detects drive -> Evaluates uninstalled state ->
        Runs setup executable -> Commits installed state to StateStore.
        """
        disk_dir = self.temp_path / "disk_cyberpunk"
        create_mock_game_disk(
            disk_dir,
            game_id="cyberpunk_2077",
            title="Cyberpunk 2077",
            version="2.12",
            setup_script_name="setup.bat",
            launcher_name="game.bat",
            custom_setup_args=["--install-dir", str(self.install_root / "Cyberpunk2077")],
            default_install_subdir="Cyberpunk2077",
        )

        drive_letter = self.mount_virtual_disk(disk_dir)

        app = GOGDiskMonitorApp(
            state_file_path=str(self.state_file),
            install_root=str(self.install_root),
            auto_confirm=True,
            headless=True,
        )

        results = app.scan_now()
        matching = [r for r in results if r.get("letter") == drive_letter]
        self.assertTrue(len(matching) >= 1, f"Expected scan result for drive {drive_letter}")
        res = matching[0]

        self.assertEqual(res.get("action"), "installed")
        self.assertEqual(res.get("game_id"), "cyberpunk_2077")
        self.assertEqual(res.get("title"), "Cyberpunk 2077")
        self.assertEqual(res.get("letter"), drive_letter)

        # Verify state store was updated
        self.assertTrue(app.state_store.is_installed("cyberpunk_2077", verify_executable=True))
        record = app.state_store.get_game("cyberpunk_2077")
        self.assertIsNotNone(record)
        self.assertEqual(record.title, "Cyberpunk 2077")
        self.assertEqual(record.version, "2.12")
        self.assertEqual(record.last_disk_drive, drive_letter)
        self.assertTrue(os.path.isfile(record.executable_path))

    # -------------------------------------------------------------------------
    # SCENARIO 2: Second Insertion (Auto-Launch Flow)
    # -------------------------------------------------------------------------
    def test_scenario_2_second_insertion_auto_launch(self):
        """
        Scenario 2: Second Insertion
        Disk is unmounted and remounted -> App detects drive -> Checks state ->
        Sees game IS installed -> Automatically launches game without prompting.
        """
        disk_dir = self.temp_path / "disk_witcher3"
        create_mock_game_disk(
            disk_dir,
            game_id="witcher_3",
            title="The Witcher 3: Wild Hunt",
            version="4.04",
            setup_script_name="setup.bat",
            launcher_name="witcher3.bat",
            custom_setup_args=["--install-dir", str(self.install_root / "Witcher3")],
            default_install_subdir="Witcher3",
        )

        # 1. First Insertion -> Install
        drive_letter_1 = self.mount_virtual_disk(disk_dir)
        app = GOGDiskMonitorApp(
            state_file_path=str(self.state_file),
            install_root=str(self.install_root),
            auto_confirm=True,
            headless=True,
        )

        res1 = app.scan_now()
        matching1 = [r for r in res1 if r.get("letter") == drive_letter_1]
        self.assertTrue(len(matching1) >= 1)
        self.assertEqual(matching1[0].get("action"), "installed")
        self.assertTrue(app.state_store.is_installed("witcher_3", verify_executable=True))

        # 2. Unmount disk
        self.unmount_virtual_disk(drive_letter_1)

        # 3. Remount disk (can be same or different drive letter)
        drive_letter_2 = self.mount_virtual_disk(disk_dir)

        # 4. Second Insertion -> Auto-Launch
        res2 = app.scan_now()
        matching2 = [r for r in res2 if r.get("letter") == drive_letter_2]
        self.assertTrue(len(matching2) >= 1)
        self.assertEqual(matching2[0].get("action"), "launched")
        self.assertEqual(matching2[0].get("game_id"), "witcher_3")
        self.assertEqual(matching2[0].get("letter"), drive_letter_2)

        # Verify last_launched_at timestamp updated
        updated_record = app.state_store.get_game("witcher_3")
        self.assertIsNotNone(updated_record.last_launched_at)
        self.assertEqual(updated_record.last_disk_drive, drive_letter_2)

    # -------------------------------------------------------------------------
    # SCENARIO 3: User Rejection / Decline Flow
    # -------------------------------------------------------------------------
    def test_scenario_3_prompt_decline_reprompt(self):
        """
        Scenario 3: Prompt Decline
        User declines prompt -> Setup is NOT run -> State remains uninstalled ->
        Subsequent insertion prompts again.
        """
        disk_dir = self.temp_path / "disk_decline"
        create_mock_game_disk(
            disk_dir,
            game_id="heroes_3",
            title="Heroes of Might and Magic III",
            version="3.2",
            custom_setup_args=["--install-dir", str(self.install_root / "Heroes3")],
            default_install_subdir="Heroes3",
        )

        drive_letter = self.mount_virtual_disk(disk_dir)

        # 1. Decline insertion prompt
        app = GOGDiskMonitorApp(
            state_file_path=str(self.state_file),
            install_root=str(self.install_root),
            auto_confirm=False,  # Rejects prompt
            headless=True,
        )

        res1 = app.scan_now()
        matching1 = [r for r in res1 if r.get("letter") == drive_letter]
        self.assertTrue(len(matching1) >= 1)
        self.assertEqual(matching1[0].get("action"), "prompt_cancelled")
        self.assertEqual(matching1[0].get("game_id"), "heroes_3")

        # Verify NOT installed
        self.assertFalse(app.state_store.is_installed("heroes_3"))
        self.assertIsNone(app.state_store.get_game("heroes_3"))

        # 2. Subsequent insertion with Acceptance
        app.auto_confirm = True
        res2 = app.scan_now()
        matching2 = [r for r in res2 if r.get("letter") == drive_letter]
        self.assertTrue(len(matching2) >= 1)
        self.assertEqual(matching2[0].get("action"), "installed")
        self.assertTrue(app.state_store.is_installed("heroes_3", verify_executable=True))

    # -------------------------------------------------------------------------
    # SCENARIO 4: Setup Failure Handling
    # -------------------------------------------------------------------------
    def test_scenario_4_setup_failure_handling(self):
        """
        Scenario 4: Setup Failure
        Setup executable fails with non-zero exit code -> State is NOT marked installed ->
        Re-insertion prompts again.
        """
        disk_dir = self.temp_path / "disk_fail"
        create_mock_game_disk(
            disk_dir,
            game_id="failing_game",
            title="Failing Setup Game",
            setup_fails=True,  # Script exits with code 1
        )

        drive_letter = self.mount_virtual_disk(disk_dir)

        app = GOGDiskMonitorApp(
            state_file_path=str(self.state_file),
            install_root=str(self.install_root),
            auto_confirm=True,
            headless=True,
        )

        res = app.scan_now()
        matching = [r for r in res if r.get("letter") == drive_letter]
        self.assertTrue(len(matching) >= 1)
        self.assertEqual(matching[0].get("action"), "setup_failed")
        self.assertEqual(matching[0].get("exit_code"), 1)

        # State must remain uninstalled
        self.assertFalse(app.state_store.is_installed("failing_game"))
        self.assertIsNone(app.state_store.get_game("failing_game"))

    # -------------------------------------------------------------------------
    # SCENARIO 5: Non-GOG Drive / Empty Drive Mount
    # -------------------------------------------------------------------------
    def test_scenario_5_non_gog_drive_safely_ignored(self):
        """
        Scenario 5: Non-GOG Drive Mount
        Mounting a regular folder / USB drive without gog_game.json ->
        App safely ignores without error or prompts.
        """
        normal_folder = self.temp_path / "normal_docs_drive"
        normal_folder.mkdir(parents=True, exist_ok=True)
        (normal_folder / "document.txt").write_text("Hello World", encoding="utf-8")
        (normal_folder / "photo.jpg").write_bytes(b"dummy image bytes")

        drive_letter = self.mount_virtual_disk(normal_folder)

        app = GOGDiskMonitorApp(
            state_file_path=str(self.state_file),
            install_root=str(self.install_root),
            auto_confirm=True,
            headless=True,
        )

        res = app.scan_now()
        matching = [r for r in res if r.get("letter") == drive_letter]
        self.assertTrue(len(matching) >= 1)
        self.assertEqual(matching[0].get("action"), "ignored")
        self.assertEqual(matching[0].get("reason"), "no_gog_config")
        self.assertEqual(matching[0].get("letter"), drive_letter)
        self.assertEqual(len(app.state_store.get_all_installed()), 0)

    # -------------------------------------------------------------------------
    # TIER 5: Background Monitoring Loop Real-Time Detection
    # -------------------------------------------------------------------------
    def test_scenario_6_background_thread_subst_detection(self):
        """
        Scenario 6: Live Background Polling Loop
        App runs in background thread -> Virtual disk is mounted dynamically ->
        DriveMonitor detects insertion -> Pipeline installs game automatically ->
        Disk is unmounted dynamically -> Removed event dispatched.
        """
        disk_dir = self.temp_path / "disk_bg_live"
        create_mock_game_disk(
            disk_dir,
            game_id="bg_live_game",
            title="Live Background Game",
            version="1.5.0",
            custom_setup_args=["--install-dir", str(self.install_root / "LiveBGGame")],
            default_install_subdir="LiveBGGame",
        )

        app = GOGDiskMonitorApp(
            state_file_path=str(self.state_file),
            install_root=str(self.install_root),
            poll_interval=0.2,  # fast poll for testing
            auto_confirm=True,
            headless=True,
            scan_on_startup=False,
        )

        removed_events: List[str] = []
        app.drive_monitor.add_on_removed_callback(lambda letter: removed_events.append(letter))

        # Start app background thread
        app.start(block=False)
        self.assertTrue(app.is_running)

        try:
            # Mount disk while app is running
            drive_letter = self.mount_virtual_disk(disk_dir)

            # Wait for background thread to detect and process
            max_wait = 5.0
            start_time = time.time()
            installed = False
            while time.time() - start_time < max_wait:
                if app.state_store.is_installed("bg_live_game", verify_executable=True):
                    installed = True
                    break
                time.sleep(0.1)

            self.assertTrue(installed, "Background monitor did not detect subst drive within timeout.")

            # Unmount disk while app is running
            self.unmount_virtual_disk(drive_letter)

            # Wait for removal event
            start_time = time.time()
            removed = False
            while time.time() - start_time < max_wait:
                if drive_letter in removed_events:
                    removed = True
                    break
                time.sleep(0.1)

            self.assertTrue(removed, f"Background monitor did not dispatch removal event for {drive_letter}.")

        finally:
            app.stop()
            self.assertFalse(app.is_running)

    # -------------------------------------------------------------------------
    # TIER 5: Multi-Game Disks Mounted Concurrently
    # -------------------------------------------------------------------------
    def test_scenario_7_multi_game_disks_concurrent(self):
        """
        Scenario 7: Multiple virtual disks mounted across different drive letters.
        Both games are detected and tracked independently.
        """
        disk1_dir = self.temp_path / "disk_multi_1"
        create_mock_game_disk(
            disk1_dir,
            game_id="game_alpha",
            title="Alpha Warriors",
            custom_setup_args=["--install-dir", str(self.install_root / "AlphaWarriors")],
            default_install_subdir="AlphaWarriors",
        )

        disk2_dir = self.temp_path / "disk_multi_2"
        create_mock_game_disk(
            disk2_dir,
            game_id="game_beta",
            title="Beta Legends",
            custom_setup_args=["--install-dir", str(self.install_root / "BetaLegends")],
            default_install_subdir="BetaLegends",
        )

        letter1 = self.mount_virtual_disk(disk1_dir)
        letter2 = self.mount_virtual_disk(disk2_dir)

        app = GOGDiskMonitorApp(
            state_file_path=str(self.state_file),
            install_root=str(self.install_root),
            auto_confirm=True,
            headless=True,
        )

        results = app.scan_now()
        matching = [r for r in results if r.get("letter") in {letter1, letter2}]
        self.assertEqual(len(matching), 2)
        installed_ids = {r.get("game_id") for r in matching if r.get("action") == "installed"}
        self.assertEqual(installed_ids, {"game_alpha", "game_beta"})

        self.assertTrue(app.state_store.is_installed("game_alpha", verify_executable=True))
        self.assertTrue(app.state_store.is_installed("game_beta", verify_executable=True))

    # -------------------------------------------------------------------------
    # TIER 5: Persistence Across App Restarts
    # -------------------------------------------------------------------------
    def test_scenario_8_app_restart_persistence(self):
        """
        Scenario 8: State persistence across application lifecycles.
        App 1 installs -> stops -> App 2 starts -> Remounts drive -> Auto-launches.
        """
        disk_dir = self.temp_path / "disk_persist"
        create_mock_game_disk(
            disk_dir,
            game_id="fallout_new_vegas",
            title="Fallout: New Vegas",
            version="1.4.0.525",
            custom_setup_args=["--install-dir", str(self.install_root / "FalloutNV")],
            default_install_subdir="FalloutNV",
        )

        letter = self.mount_virtual_disk(disk_dir)

        # Lifecycle 1: Install
        app1 = GOGDiskMonitorApp(
            state_file_path=str(self.state_file),
            install_root=str(self.install_root),
            auto_confirm=True,
            headless=True,
        )
        res1 = app1.scan_now()
        matching1 = [r for r in res1 if r.get("letter") == letter]
        self.assertTrue(len(matching1) >= 1)
        self.assertEqual(matching1[0]["action"], "installed")
        app1.stop()

        # Unmount and remount
        self.unmount_virtual_disk(letter)
        letter2 = self.mount_virtual_disk(disk_dir)

        # Lifecycle 2: Fresh app instance reading same state file
        app2 = GOGDiskMonitorApp(
            state_file_path=str(self.state_file),
            install_root=str(self.install_root),
            auto_confirm=True,
            headless=True,
        )
        res2 = app2.scan_now()
        matching2 = [r for r in res2 if r.get("letter") == letter2]
        self.assertTrue(len(matching2) >= 1)
        self.assertEqual(matching2[0]["action"], "launched")
        self.assertEqual(matching2[0]["game_id"], "fallout_new_vegas")
        app2.stop()

    # -------------------------------------------------------------------------
    # TIER 5: Custom Icon Resolution on subst Drive
    # -------------------------------------------------------------------------
    def test_scenario_9_custom_icon_resolution_on_subst(self):
        """
        Scenario 9: Custom .ico and .png icons on subst drives are resolved accurately.
        """
        disk_dir = self.temp_path / "disk_custom_icon"
        create_mock_game_disk(
            disk_dir,
            game_id="custom_icon_game",
            title="Custom Icon Game",
            icon_filename="art.ico",
        )

        letter = self.mount_virtual_disk(disk_dir)
        drive_root = f"{letter}\\"

        config = parse_disk_config(drive_root)
        self.assertIsNotNone(config)
        self.assertEqual(config.icon_path, "art.ico")

        resolved_icon = find_disk_icon(drive_root, config)
        self.assertIsNotNone(resolved_icon)
        self.assertTrue(os.path.isfile(resolved_icon))
        self.assertEqual(os.path.normpath(resolved_icon), os.path.normpath(os.path.join(drive_root, "art.ico")))

    # -------------------------------------------------------------------------
    # TIER 5: Missing Setup Executable Graceful Handling
    # -------------------------------------------------------------------------
    def test_scenario_10_missing_setup_executable(self):
        """
        Scenario 10: gog_game.json specifies a setup executable that does not exist.
        Handled safely with setup_failed and state left uninstalled.
        """
        disk_dir = self.temp_path / "disk_missing_exe"
        create_mock_game_disk(
            disk_dir,
            game_id="missing_setup_game",
            title="Missing Setup Game",
            setup_script_name="nonexistent_setup.bat",
        )
        # Delete the setup script so it is missing
        missing_file = disk_dir / "nonexistent_setup.bat"
        if missing_file.exists():
            missing_file.unlink()

        letter = self.mount_virtual_disk(disk_dir)

        app = GOGDiskMonitorApp(
            state_file_path=str(self.state_file),
            install_root=str(self.install_root),
            auto_confirm=True,
            headless=True,
        )

        res = app.scan_now()
        matching = [r for r in res if r.get("letter") == letter]
        self.assertTrue(len(matching) >= 1)
        self.assertEqual(matching[0].get("action"), "setup_failed")
        self.assertFalse(app.state_store.is_installed("missing_setup_game"))

    # -------------------------------------------------------------------------
    # TIER 5: Corrupted Config File on Virtual Drive
    # -------------------------------------------------------------------------
    def test_scenario_11_corrupted_config_on_subst(self):
        """
        Scenario 11: Invalid JSON syntax in gog_game.json on virtual drive.
        Safely ignored without crash.
        """
        disk_dir = self.temp_path / "disk_corrupt"
        create_mock_game_disk(
            disk_dir,
            corrupt_config=True,
        )

        letter = self.mount_virtual_disk(disk_dir)

        app = GOGDiskMonitorApp(
            state_file_path=str(self.state_file),
            install_root=str(self.install_root),
            auto_confirm=True,
            headless=True,
        )

        res = app.scan_now()
        matching = [r for r in res if r.get("letter") == letter]
        self.assertTrue(len(matching) >= 1)
        self.assertEqual(matching[0].get("action"), "ignored")
        self.assertEqual(matching[0].get("reason"), "no_gog_config")


if __name__ == "__main__":
    unittest.main(verbosity=2)
