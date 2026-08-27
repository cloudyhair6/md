"""
tests.test_integration
~~~~~~~~~~~~~~~~~~~~~~~

Comprehensive integration test suite for Milestone 4:
  - System Tray Management (gog_disk_monitor.tray.TrayManager)
  - Application Coordinator (gog_disk_monitor.app.GOGDiskMonitorApp)
  - Drive insertion / unmount lifecycle (Install vs Launch branch routing)
  - State persistence & updates
  - Command-Line Interface (gog_disk_monitor.cli)
"""

from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from gog_disk_monitor import (
    GOGDiskConfig,
    GOGDiskMonitorApp,
    InstalledGameRecord,
    LauncherConfig,
    ProcessRunner,
    SetupConfig,
    StateStore,
    TrayManager,
    build_parser,
    create_default_icon_image,
    load_tray_icon,
    main,
    parse_disk_config,
)
from gog_disk_monitor.drive_monitor import DriveInfo, DriveMonitor, WindowsDriveDetector


class TestTrayManager(unittest.TestCase):
    """Unit and functional tests for TrayManager and icon generation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_default_icon_image(self):
        """Generates a valid RGBA PIL image with the default procedural disc style."""
        img = create_default_icon_image(size=(64, 64))
        self.assertIsNotNone(img)
        self.assertEqual(img.size, (64, 64))
        self.assertEqual(img.mode, "RGBA")

    def test_load_tray_icon_fallback(self):
        """Falls back to procedural icon if file does not exist or is invalid."""
        img_none = load_tray_icon(None)
        self.assertIsNotNone(img_none)
        self.assertEqual(img_none.size, (64, 64))

        img_missing = load_tray_icon("nonexistent_path_xyz.ico")
        self.assertIsNotNone(img_missing)
        self.assertEqual(img_missing.size, (64, 64))

    def test_load_tray_icon_from_valid_image(self):
        """Loads and resizes an existing image file correctly."""
        from PIL import Image
        sample_img_path = self.temp_path / "custom_icon.png"
        raw_img = Image.new("RGBA", (128, 128), (255, 0, 0, 255))
        raw_img.save(sample_img_path)

        loaded = load_tray_icon(sample_img_path, size=(32, 32))
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.size, (32, 32))

    def test_tray_manager_status_updates(self):
        """set_status and get_status update internal state and tooltip."""
        tray = TrayManager(app_name="TestApp", headless=True)
        self.assertEqual(tray.get_status(), "Monitoring drives...")

        tray.set_status("Installing Baldur's Gate")
        self.assertEqual(tray.get_status(), "Installing Baldur's Gate")

    def test_tray_manager_dynamic_menu_building(self):
        """build_menu includes status header, scan, installed games submenu, and exit."""
        games_store = {
            "witcher_3": InstalledGameRecord(
                game_id="witcher_3",
                title="The Witcher 3",
                version="4.04",
                executable_path="C:\\games\\w3.exe",
            ),
            "cyberpunk": InstalledGameRecord(
                game_id="cyberpunk",
                title="Cyberpunk 2077",
                version="2.1",
                executable_path="C:\\games\\cp.exe",
            ),
        }

        tray = TrayManager(
            app_name="GOG Monitor",
            get_installed_games=lambda: games_store,
            headless=True,
        )

        menu = tray.build_menu()
        self.assertIsNotNone(menu)

    def test_tray_manager_menu_with_empty_games(self):
        """build_menu handles empty installed games store gracefully."""
        tray = TrayManager(
            app_name="GOG Monitor",
            get_installed_games=lambda: {},
            headless=True,
        )
        menu = tray.build_menu()
        self.assertIsNotNone(menu)

    def test_tray_manager_callbacks_dispatch(self):
        """Menu click callbacks for scan, open state, and launch execute properly."""
        scan_called = []
        state_called = []
        launch_called = []

        tray = TrayManager(
            app_name="GOG Monitor",
            on_scan_now=lambda: scan_called.append(True),
            on_open_state=lambda: state_called.append(True),
            on_launch_game=lambda gid: launch_called.append(gid),
            headless=True,
        )

        tray._on_menu_scan_now()
        self.assertTrue(scan_called)

        tray._on_menu_open_state()
        self.assertTrue(state_called)

        tray._handle_launch_game("homm3")
        self.assertEqual(launch_called, ["homm3"])

    def test_tray_manager_headless_lifecycle(self):
        """Headless TrayManager starts and stops cleanly without error."""
        setup_called = []
        exit_called = []

        tray = TrayManager(
            app_name="GOG Monitor",
            on_exit=lambda: exit_called.append(True),
            headless=True,
        )

        self.assertFalse(tray.is_running())
        tray.start(setup=lambda icon: setup_called.append(True), detached=True)
        self.assertTrue(tray.is_running())
        self.assertTrue(setup_called)

        tray.stop()
        self.assertFalse(tray.is_running())
        self.assertTrue(exit_called)

    def test_open_state_folder_creates_directory(self):
        """open_state_folder creates target directory if missing."""
        target = self.temp_path / "test_state_dir"
        self.assertFalse(target.exists())

        with patch("os.startfile", create=True) as mock_startfile:
            success = TrayManager.open_state_folder(target)
            self.assertTrue(success)
            self.assertTrue(target.is_dir())
            if sys.platform == "win32":
                mock_startfile.assert_called_once_with(str(target.resolve()))


class TestAppInsertionInstallFlow(unittest.TestCase):
    """Integration tests for the First Insertion (Install) branch in GOGDiskMonitorApp."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)

        self.state_file = self.base_path / "installed_games.json"
        self.state_store = StateStore(state_file_path=self.state_file)

        # Mock virtual disk folder
        self.disk_folder = self.base_path / "mock_drive_E"
        self.disk_folder.mkdir()

        # Mock game installation destination folder
        self.install_root = self.base_path / "installed_games_root"
        self.install_root.mkdir()

        # Create mock installer executable inside disk
        self.setup_script = self.disk_folder / "setup_test.bat"
        # The installer creates the target folder and executable
        installed_game_dir = self.install_root / "heroes3"
        installed_game_exe = installed_game_dir / "heroes3.exe"

        batch_content = (
            f"@echo off\n"
            f"mkdir \"{installed_game_dir}\" 2>nul\n"
            f"echo mock_game_binary > \"{installed_game_exe}\"\n"
            f"exit /b 0\n"
        )
        self.setup_script.write_text(batch_content, encoding="utf-8")

        # Create gog_game.json descriptor on disk
        self.config_data = {
            "schema_version": "1.0",
            "game_id": "heroes_of_might_and_magic_3",
            "title": "Heroes of Might and Magic III: Complete",
            "version": "4.0",
            "publisher": "Ubisoft",
            "setup": {
                "executable": "setup_test.bat",
                "arguments": ["/SILENT"],
                "default_install_subdir": "heroes3",
                "estimated_size_mb": 500,
            },
            "launcher": {
                "executable": "heroes3.exe",
                "arguments": ["-fullscreen"],
            },
        }
        config_path = self.disk_folder / "gog_game.json"
        config_path.write_text(json.dumps(self.config_data, indent=2), encoding="utf-8")

        self.drive_info = DriveInfo(
            letter="E:",
            drive_type="REMOVABLE",
            volume_name="HOMM3_DISC",
            root_path=f"{self.disk_folder}\\",
            is_ready=True,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_first_insertion_accepted_installs_and_records_state(self):
        """
        Uninstalled game disc inserted -> prompt accepted -> setup executed
        -> state store updated with InstalledGameRecord -> game marked installed.
        """
        app = GOGDiskMonitorApp(
            state_store=self.state_store,
            auto_confirm=True,
            headless=True,
            install_root=str(self.install_root),
        )

        self.assertFalse(self.state_store.is_installed("heroes_of_might_and_magic_3"))

        result = app.handle_drive_inserted(self.drive_info)

        self.assertEqual(result.get("action"), "installed")
        self.assertEqual(result.get("game_id"), "heroes_of_might_and_magic_3")
        self.assertEqual(result.get("title"), "Heroes of Might and Magic III: Complete")

        # Verify state store now contains the installed game record
        self.assertTrue(self.state_store.is_installed("heroes_of_might_and_magic_3"))
        record = self.state_store.get_game("heroes_of_might_and_magic_3")
        self.assertIsNotNone(record)
        self.assertEqual(record.title, "Heroes of Might and Magic III: Complete")
        self.assertEqual(record.version, "4.0")
        self.assertEqual(record.last_disk_drive, "E:")
        self.assertEqual(record.last_disk_label, "HOMM3_DISC")

        # Verify physical executable was created by setup script
        expected_exe = self.install_root / "heroes3" / "heroes3.exe"
        self.assertTrue(expected_exe.is_file())

    def test_first_insertion_declined_cancels_and_does_not_modify_state(self):
        """
        Uninstalled game disc inserted -> prompt cancelled -> setup NOT run
        -> state store remains unmodified.
        """
        app = GOGDiskMonitorApp(
            state_store=self.state_store,
            auto_confirm=False,
            headless=True,
            install_root=str(self.install_root),
        )

        result = app.handle_drive_inserted(self.drive_info)

        self.assertEqual(result.get("action"), "prompt_cancelled")
        self.assertFalse(self.state_store.is_installed("heroes_of_might_and_magic_3"))

        expected_exe = self.install_root / "heroes3" / "heroes3.exe"
        self.assertFalse(expected_exe.exists())

    def test_first_insertion_setup_failure_does_not_record_state(self):
        """
        If setup executable fails (non-zero exit code), state store is NOT updated.
        """
        # Create failing setup script
        fail_setup = self.disk_folder / "setup_fail.bat"
        fail_setup.write_text("@echo off\nexit /b 42\n", encoding="utf-8")

        self.config_data["setup"]["executable"] = "setup_fail.bat"
        (self.disk_folder / "gog_game.json").write_text(json.dumps(self.config_data), encoding="utf-8")

        app = GOGDiskMonitorApp(
            state_store=self.state_store,
            auto_confirm=True,
            headless=True,
            install_root=str(self.install_root),
        )

        result = app.handle_drive_inserted(self.drive_info)

        self.assertEqual(result.get("action"), "setup_failed")
        self.assertEqual(result.get("exit_code"), 42)
        self.assertFalse(self.state_store.is_installed("heroes_of_might_and_magic_3"))

    def test_first_insertion_with_quoted_arguments_in_config(self):
        """
        Verify that when a config contains quoted arguments (like /dir=\"C:\\Path\"),
        the application parses the config and executes setup successfully without literal quotes in path.
        """
        # Create mock installer script that validates its arguments
        check_script = self.disk_folder / "setup_quoted.bat"
        installed_game_dir = self.install_root / "heroes3_quoted"
        installed_game_exe = installed_game_dir / "heroes3.exe"

        batch_content = (
            f"@echo off\n"
            f"mkdir \"{installed_game_dir}\" 2>nul\n"
            f"echo mock_game_binary > \"{installed_game_exe}\"\n"
            f"exit /b 0\n"
        )
        check_script.write_text(batch_content, encoding="utf-8")

        self.config_data["game_id"] = "homm3_quoted"
        self.config_data["setup"]["executable"] = "setup_quoted.bat"
        self.config_data["setup"]["arguments"] = [
            r'/dir="C:\GOG Games\Heroes 3"',
            "/SILENT",
        ]
        self.config_data["setup"]["default_install_subdir"] = "heroes3_quoted"
        (self.disk_folder / "gog_game.json").write_text(json.dumps(self.config_data), encoding="utf-8")

        app = GOGDiskMonitorApp(
            state_store=self.state_store,
            auto_confirm=True,
            headless=True,
            install_root=str(self.install_root),
        )

        with patch("gog_disk_monitor.launcher.ProcessRunner.run_setup", wraps=ProcessRunner.run_setup) as spy_setup:
            result = app.handle_drive_inserted(self.drive_info)

            self.assertEqual(result.get("action"), "installed")
            self.assertEqual(result.get("game_id"), "homm3_quoted")
            self.assertTrue(self.state_store.is_installed("homm3_quoted", verify_executable=True))

            # Verify that run_setup was called with sanitized arguments
            spy_setup.assert_called_once()
            called_args = spy_setup.call_args[1].get("args") or spy_setup.call_args[0][1]
            self.assertIn(r"/dir=C:\GOG Games\Heroes 3", called_args)
            self.assertNotIn(r'/dir="C:\GOG Games\Heroes 3"', called_args)


class TestAppInsertionLaunchFlow(unittest.TestCase):
    """Integration tests for the Second Insertion (Auto-Launch) branch in GOGDiskMonitorApp."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)

        self.state_file = self.base_path / "installed_games.json"
        self.state_store = StateStore(state_file_path=self.state_file)

        # Mock game installation on PC
        self.game_dir = self.base_path / "Games" / "Diablo"
        self.game_dir.mkdir(parents=True)
        self.game_exe = self.game_dir / "diablo.exe"
        self.game_exe.write_text("mock binary", encoding="utf-8")

        # Pre-record game as installed in state store
        record = InstalledGameRecord(
            game_id="diablo_1",
            title="Diablo + Hellfire",
            version="1.09",
            install_path=str(self.game_dir),
            executable_path=str(self.game_exe),
            last_disk_drive="D:",
        )
        self.state_store.mark_installed(record)

        # Mock virtual disk folder with matching descriptor
        self.disk_folder = self.base_path / "mock_drive_D"
        self.disk_folder.mkdir()

        self.config_data = {
            "schema_version": "1.0",
            "game_id": "diablo_1",
            "title": "Diablo + Hellfire",
            "version": "1.09",
            "setup": {
                "executable": "setup.exe",
            },
            "launcher": {
                "executable": "diablo.exe",
                "arguments": ["-direct"],
            },
        }
        (self.disk_folder / "gog_game.json").write_text(json.dumps(self.config_data), encoding="utf-8")

        self.drive_info = DriveInfo(
            letter="D:",
            drive_type="CDROM",
            volume_name="DIABLO_DISC",
            root_path=f"{self.disk_folder}\\",
            is_ready=True,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_second_insertion_auto_launches_detached_without_prompt(self):
        """
        When disk of an installed game is inserted, it automatically launches the
        installed game detached without prompting, and updates launch timestamp in state.
        """
        app = GOGDiskMonitorApp(
            state_store=self.state_store,
            headless=True,
        )

        mock_proc = MagicMock()
        mock_proc.pid = 98765

        with patch("gog_disk_monitor.launcher.ProcessRunner.launch_game", return_value=mock_proc) as mock_launch:
            result = app.handle_drive_inserted(self.drive_info)

            self.assertEqual(result.get("action"), "launched")
            self.assertEqual(result.get("game_id"), "diablo_1")
            self.assertEqual(result.get("pid"), 98765)

            # Verify launch_game was called with correct binary path and arguments
            mock_launch.assert_called_once_with(
                game_exe_path=str(self.game_exe),
                args=["-direct"],
                cwd=str(self.game_dir),
                detached=True,
            )

            # Verify state store was updated with last launch time and current drive info
            rec = self.state_store.get_game("diablo_1")
            self.assertIsNotNone(rec.last_launched_at)
            self.assertEqual(rec.last_disk_drive, "D:")
            self.assertEqual(rec.last_disk_label, "DIABLO_DISC")

    def test_broken_installation_missing_exe_prompts_reinstall(self):
        """
        If a game was marked installed but the physical .exe is missing,
        verify_executable detects it and drops back into the prompt/install branch.
        """
        # Delete the physical executable
        if self.game_exe.exists():
            self.game_exe.unlink()

        app = GOGDiskMonitorApp(
            state_store=self.state_store,
            auto_confirm=False,  # User declines reinstall
            headless=True,
        )

        result = app.handle_drive_inserted(self.drive_info)
        # Should prompt user rather than attempting to launch missing binary
        self.assertEqual(result.get("action"), "prompt_cancelled")


class TestAppIgnoredAndRemovalEvents(unittest.TestCase):
    """Tests for non-GOG drives, unready drives, and drive removal events."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.state_store = StateStore(state_file_path=self.base_path / "state.json")
        self.app = GOGDiskMonitorApp(state_store=self.state_store, headless=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_non_gog_drive_ignored(self):
        """Drive without gog_game.json is ignored gracefully without errors."""
        empty_folder = self.base_path / "empty_usb"
        empty_folder.mkdir()

        drive = DriveInfo(
            letter="F:",
            drive_type="REMOVABLE",
            root_path=f"{empty_folder}\\",
            is_ready=True,
        )

        result = self.app.handle_drive_inserted(drive)
        self.assertEqual(result.get("action"), "ignored")
        self.assertEqual(result.get("reason"), "no_gog_config")

    def test_unready_drive_ignored(self):
        """Drive with is_ready=False (e.g. empty optical tray) is ignored."""
        drive = DriveInfo(
            letter="G:",
            drive_type="CDROM",
            root_path="G:\\",
            is_ready=False,
        )

        result = self.app.handle_drive_inserted(drive)
        self.assertEqual(result.get("action"), "ignored")
        self.assertEqual(result.get("reason"), "not_ready")

    def test_drive_removed_event(self):
        """handle_drive_removed resets tray status and logs event."""
        self.app.tray.set_status("Installed something")
        self.app.handle_drive_removed("E:")
        self.assertEqual(self.app.tray.get_status(), "Monitoring drives...")


class TestAppManualScanAndLaunchById(unittest.TestCase):
    """Tests for scan_now and launch_game_by_id coordinator methods."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.state_store = StateStore(state_file_path=self.base_path / "state.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_scan_now_processes_mounted_drives(self):
        """scan_now performs synchronous scan and processes candidate drives."""
        # Create mock drive
        mock_drive_dir = self.base_path / "disc"
        mock_drive_dir.mkdir()
        cfg = {
            "schema_version": "1.0",
            "game_id": "fallout_2",
            "title": "Fallout 2",
            "version": "1.02",
            "setup": {"executable": "setup.exe"},
            "launcher": {"executable": "fallout2.exe"},
        }
        (mock_drive_dir / "gog_game.json").write_text(json.dumps(cfg), encoding="utf-8")

        mock_detector = MagicMock(spec=WindowsDriveDetector)
        mock_detector.get_logical_drives_mask.return_value = 0b100  # C:
        mock_detector.get_drive_letters_from_mask.return_value = {"X:"}
        mock_detector.inspect_drive.return_value = DriveInfo(
            letter="X:",
            drive_type="REMOVABLE",
            root_path=f"{mock_drive_dir}\\",
            is_ready=True,
        )

        monitor = DriveMonitor(detector=mock_detector)
        app = GOGDiskMonitorApp(
            state_store=self.state_store,
            drive_monitor=monitor,
            auto_confirm=False,
            headless=True,
        )

        results = app.scan_now()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].get("action"), "prompt_cancelled")
        self.assertEqual(results[0].get("game_id"), "fallout_2")

    def test_launch_game_by_id_valid_and_missing(self):
        """launch_game_by_id launches installed game and returns PID, or None if missing."""
        mock_exe = self.base_path / "game.exe"
        mock_exe.write_text("binary", encoding="utf-8")

        self.state_store.mark_installed(
            InstalledGameRecord(
                game_id="quake_2",
                title="Quake II",
                version="3.20",
                executable_path=str(mock_exe),
            )
        )

        app = GOGDiskMonitorApp(state_store=self.state_store, headless=True)

        mock_proc = MagicMock()
        mock_proc.pid = 43210

        with patch("gog_disk_monitor.launcher.ProcessRunner.launch_game", return_value=mock_proc):
            pid = app.launch_game_by_id("quake_2")
            self.assertEqual(pid, 43210)

        # Missing game
        pid_none = app.launch_game_by_id("nonexistent_game_xyz")
        self.assertIsNone(pid_none)


class TestCLI(unittest.TestCase):
    """Functional tests for command-line arguments and entrypoint (gog_disk_monitor.cli)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.state_file = self.base_path / "cli_state.json"
        self.store = StateStore(state_file_path=self.state_file)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cli_version(self):
        """--version prints program version and raises SystemExit(0)."""
        parser = build_parser()
        with self.assertRaises(SystemExit) as cm:
            with patch("sys.stdout", new=io.StringIO()):
                parser.parse_args(["--version"])
        self.assertEqual(cm.exception.code, 0)

    def test_cli_list_installed_empty(self):
        """--list-installed on empty store outputs appropriate message."""
        with patch("sys.stdout", new=io.StringIO()) as fake_out:
            code = main(["--state-file", str(self.state_file), "--list-installed"])
            self.assertEqual(code, 0)
            self.assertIn("No installed GOG games found", fake_out.getvalue())

    def test_cli_list_installed_with_records(self):
        """--list-installed displays table of all installed games."""
        self.store.mark_installed(
            InstalledGameRecord(
                game_id="grim_fandango",
                title="Grim Fandango Remastered",
                version="1.5.4",
                executable_path="C:\\games\\grim.exe",
            )
        )

        with patch("sys.stdout", new=io.StringIO()) as fake_out:
            code = main(["--state-file", str(self.state_file), "--list-installed"])
            self.assertEqual(code, 0)
            output = fake_out.getvalue()
            self.assertIn("grim_fandango", output)
            self.assertIn("Grim Fandango Remastered", output)

    def test_cli_unmark_existing_and_nonexistent(self):
        """--unmark removes existing game (returns 0) or reports not found (returns 1)."""
        self.store.mark_installed(
            InstalledGameRecord(
                game_id="planescape",
                title="Planescape: Torment",
                version="1.0",
                executable_path="C:\\games\\pst.exe",
            )
        )
        self.assertTrue(self.store.is_installed("planescape"))

        with patch("sys.stdout", new=io.StringIO()):
            code = main(["--state-file", str(self.state_file), "--unmark", "planescape"])
            self.assertEqual(code, 0)
            self.store.load()
            self.assertFalse(self.store.is_installed("planescape"))

            code_fail = main(["--state-file", str(self.state_file), "--unmark", "planescape"])
            self.assertEqual(code_fail, 1)


    def test_cli_scan_once(self):
        """--scan-once triggers single scan and exits cleanly with 0."""
        with patch("gog_disk_monitor.app.GOGDiskMonitorApp.scan_now", return_value=[]):
            code = main(["--state-file", str(self.state_file), "--scan-once", "--headless"])
            self.assertEqual(code, 0)

    def test_cli_argument_parser_flags(self):
        """Parser correctly parses full set of CLI flags."""
        parser = build_parser()
        args = parser.parse_args([
            "--poll-interval", "1.25",
            "--auto-confirm",
            "--scan-startup",
            "--install-root", "C:\\CustomGames",
            "-v",
            "--state-file", "C:\\state.json",
        ])
        self.assertEqual(args.poll_interval, 1.25)
        self.assertTrue(args.auto_confirm)
        self.assertTrue(args.scan_startup)
        self.assertEqual(args.install_root, "C:\\CustomGames")
        self.assertTrue(args.verbose)
        self.assertEqual(args.state_file, "C:\\state.json")

    def test_cli_auto_reject_flag(self):
        """--auto-reject sets auto_confirm=False on app."""
        parser = build_parser()
        args = parser.parse_args(["--auto-reject"])
        self.assertTrue(args.auto_reject)


class TestAppLifecycleAndThreading(unittest.TestCase):
    """Tests for application lifecycle orchestration and thread management."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.state_file = self.base_path / "app_state.json"
        self.store = StateStore(state_file_path=self.state_file)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_app_start_and_stop_detached(self):
        """App starts monitoring in background and stops cleanly."""
        app = GOGDiskMonitorApp(
            state_store=self.store,
            poll_interval=0.1,
            headless=True,
        )

        self.assertFalse(app.is_running)
        app.start(block=False)
        self.assertTrue(app.is_running)
        self.assertTrue(app.drive_monitor.is_running())

        # Calling start again while running is a safe no-op
        app.start(block=False)
        self.assertTrue(app.is_running)

        app.stop()
        self.assertFalse(app.is_running)
        self.assertFalse(app.drive_monitor.is_running())

        # Calling stop again when already stopped is a safe no-op
        app.stop()
        self.assertFalse(app.is_running)

    def test_app_open_state_folder(self):
        """open_state_folder invokes TrayManager with correct state folder."""
        app = GOGDiskMonitorApp(state_store=self.store, headless=True)
        with patch.object(TrayManager, "open_state_folder", return_value=True) as mock_open:
            success = app.open_state_folder()
            self.assertTrue(success)
            mock_open.assert_called_once_with(self.store.state_file_path.parent)

    def test_simulated_drive_insertion_detected_and_processed_while_tray_running(self):
        """
        Integration test verifying that simulated drive insertion events are successfully
        detected and processed in real time while the system tray application is actively running.
        """
        # Create a mock game disc directory
        disc_dir = self.base_path / "mock_bg_disc"
        disc_dir.mkdir(parents=True, exist_ok=True)
        setup_exe = disc_dir / "setup.bat"
        setup_exe.write_text("@echo off\nexit /b 0\n", encoding="utf-8")

        cfg = {
            "schema_version": "1.0",
            "game_id": "bg_proc_game",
            "title": "Background Processing Game",
            "version": "1.0.0",
            "setup": {
                "executable": "setup.bat",
                "arguments": ["/SILENT"],
                "default_install_subdir": "bg_proc",
            },
            "launcher": {
                "executable": "game.exe",
            },
        }
        (disc_dir / "gog_game.json").write_text(json.dumps(cfg), encoding="utf-8")

        # Mock detector to simulate dynamic drive insertion
        mock_detector = MagicMock(spec=WindowsDriveDetector)
        mock_detector.get_logical_drives_mask.return_value = 0b100  # C: only initially
        mock_detector.get_drive_letters_from_mask.return_value = set()
        mock_detector.inspect_drive.return_value = DriveInfo(
            letter="Z:",
            drive_type="REMOVABLE",
            root_path=f"{disc_dir}\\",
            is_ready=True,
        )

        monitor = DriveMonitor(
            poll_interval=0.05,
            detector=mock_detector,
            auto_suppress_errors=False,
        )

        app = GOGDiskMonitorApp(
            state_store=self.store,
            drive_monitor=monitor,
            poll_interval=0.05,
            auto_confirm=True,
            headless=False,  # Active tray manager
            install_root=str(self.base_path / "installs"),
        )

        try:
            # Start app with background monitoring and active system tray
            app.start(block=False)
            self.assertTrue(app.is_running)
            self.assertTrue(app.drive_monitor.is_running())
            self.assertTrue(app.tray.is_running())

            # Verify game is not installed yet
            self.assertFalse(self.store.is_installed("bg_proc_game"))

            # Simulate drive insertion while tray is actively running
            mock_detector.get_logical_drives_mask.return_value = (1 << 25)  # Z:
            mock_detector.get_drive_letters_from_mask.return_value = {"Z:"}

            # Wait for background monitoring loop to detect and process event in real-time
            start_time = time.time()
            max_wait = 4.0
            processed = False
            while time.time() - start_time < max_wait:
                if self.store.is_installed("bg_proc_game"):
                    processed = True
                    break
                time.sleep(0.05)

            self.assertTrue(
                processed,
                "Drive insertion was not processed in real time while tray was running.",
            )

            # Verify system tray is still actively running and responsive
            self.assertTrue(app.tray.is_running())
            self.assertEqual(app.tray.get_status(), "Installed: Background Processing Game")

        finally:
            app.stop()
            self.assertFalse(app.is_running)
            self.assertFalse(app.drive_monitor.is_running())

    def test_drive_insertion_processed_in_real_time_when_app_started_with_block_true(self):
        """
        Verify that when GOGDiskMonitorApp is started with block=True, the drive
        monitoring engine runs continuously in the background and processes drive
        insertions in real time without being blocked by the tray main execution loop.
        """
        disc_dir = self.base_path / "mock_block_disc"
        disc_dir.mkdir(parents=True, exist_ok=True)
        setup_exe = disc_dir / "setup.bat"
        setup_exe.write_text("@echo off\nexit /b 0\n", encoding="utf-8")

        cfg = {
            "schema_version": "1.0",
            "game_id": "block_loop_game",
            "title": "Block Loop Game",
            "version": "1.0",
            "setup": {"executable": "setup.bat"},
            "launcher": {"executable": "game.exe"},
        }
        (disc_dir / "gog_game.json").write_text(json.dumps(cfg), encoding="utf-8")

        mock_detector = MagicMock(spec=WindowsDriveDetector)
        mock_detector.get_logical_drives_mask.return_value = 0
        mock_detector.get_drive_letters_from_mask.return_value = set()
        mock_detector.inspect_drive.return_value = DriveInfo(
            letter="Y:",
            drive_type="REMOVABLE",
            root_path=f"{disc_dir}\\",
            is_ready=True,
        )

        monitor = DriveMonitor(
            poll_interval=0.05,
            detector=mock_detector,
            auto_suppress_errors=False,
        )

        app = GOGDiskMonitorApp(
            state_store=self.store,
            drive_monitor=monitor,
            poll_interval=0.05,
            auto_confirm=True,
            headless=False,
            install_root=str(self.base_path / "installs"),
        )

        # Run app.start(block=True) in a separate thread simulating the main execution thread
        app_thread = threading.Thread(target=lambda: app.start(block=True), daemon=True)
        app_thread.start()

        try:
            # Wait for app to become active
            start_time = time.time()
            while not app.is_running and time.time() - start_time < 3.0:
                time.sleep(0.05)

            self.assertTrue(app.is_running)
            self.assertTrue(app.drive_monitor.is_running())

            # Simulate drive insertion
            mock_detector.get_logical_drives_mask.return_value = (1 << 24)  # Y:
            mock_detector.get_drive_letters_from_mask.return_value = {"Y:"}

            # Verify insertion is processed in real time
            start_time = time.time()
            max_wait = 4.0
            detected = False
            while time.time() - start_time < max_wait:
                if self.store.is_installed("block_loop_game"):
                    detected = True
                    break
                time.sleep(0.05)

            self.assertTrue(detected, "Drive insertion was blocked while app was running with block=True")

        finally:
            app.stop()
            app_thread.join(timeout=3.0)
            self.assertFalse(app_thread.is_alive())
            self.assertFalse(app.is_running)


class TestAppEdgeCasesAndErrorHandling(unittest.TestCase):
    """Tests for edge cases, exceptions during setup/launch, and custom icon paths."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.state_file = self.base_path / "state.json"
        self.store = StateStore(state_file_path=self.state_file)
        self.disk_folder = self.base_path / "disk"
        self.disk_folder.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_setup_exception_handling(self):
        """Process execution exceptions during setup are caught gracefully."""
        cfg = {
            "schema_version": "1.0",
            "game_id": "bad_setup_game",
            "title": "Bad Setup Game",
            "version": "1.0",
            "setup": {"executable": "missing_setup.exe"},
            "launcher": {"executable": "game.exe"},
        }
        (self.disk_folder / "gog_game.json").write_text(json.dumps(cfg), encoding="utf-8")

        drive = DriveInfo(
            letter="Z:",
            drive_type="REMOVABLE",
            root_path=f"{self.disk_folder}\\",
            is_ready=True,
        )

        app = GOGDiskMonitorApp(
            state_store=self.store,
            auto_confirm=True,
            headless=True,
        )

        result = app.handle_drive_inserted(drive)
        self.assertEqual(result.get("action"), "setup_failed")
        self.assertIn("error", result)
        self.assertFalse(self.store.is_installed("bad_setup_game"))

    def test_launch_game_exception_handling(self):
        """Process execution exceptions during game launch are caught gracefully."""
        # Pre-record game in state store with real dummy binary
        phantom_file = self.base_path / "phantom.exe"
        phantom_file.write_text("dummy", encoding="utf-8")

        self.store.mark_installed(
            InstalledGameRecord(
                game_id="crash_game",
                title="Crash Game",
                version="1.0",
                executable_path=str(phantom_file),
            )
        )

        cfg = {
            "schema_version": "1.0",
            "game_id": "crash_game",
            "title": "Crash Game",
            "version": "1.0",
            "setup": {"executable": "setup.exe"},
            "launcher": {"executable": "phantom.exe"},
        }
        (self.disk_folder / "gog_game.json").write_text(json.dumps(cfg), encoding="utf-8")

        drive = DriveInfo(
            letter="Y:",
            drive_type="CDROM",
            root_path=f"{self.disk_folder}\\",
            is_ready=True,
        )

        app = GOGDiskMonitorApp(state_store=self.store, headless=True, auto_confirm=False)

        with patch("gog_disk_monitor.launcher.ProcessRunner.launch_game", side_effect=PermissionError("Access Denied")):
            result = app.handle_drive_inserted(drive)
            self.assertEqual(result.get("action"), "launch_failed")
            self.assertIn("error", result)


    def test_launch_with_working_directory_override(self):
        """Working directory override from launcher config is used when launching game."""
        game_dir = self.base_path / "SubGame"
        game_dir.mkdir()
        sub_work_dir = game_dir / "bin"
        sub_work_dir.mkdir()
        game_exe = sub_work_dir / "subgame.exe"
        game_exe.write_text("binary", encoding="utf-8")

        self.store.mark_installed(
            InstalledGameRecord(
                game_id="sub_game",
                title="Sub Game",
                version="1.0",
                install_path=str(game_dir),
                executable_path=str(game_exe),
            )
        )

        cfg = {
            "schema_version": "1.0",
            "game_id": "sub_game",
            "title": "Sub Game",
            "version": "1.0",
            "setup": {"executable": "setup.exe"},
            "launcher": {
                "executable": "bin/subgame.exe",
                "working_directory": "bin",
            },
        }
        (self.disk_folder / "gog_game.json").write_text(json.dumps(cfg), encoding="utf-8")

        drive = DriveInfo(
            letter="W:",
            drive_type="CDROM",
            root_path=f"{self.disk_folder}\\",
            is_ready=True,
        )

        app = GOGDiskMonitorApp(state_store=self.store, headless=True)

        mock_proc = MagicMock()
        mock_proc.pid = 11223

        with patch("gog_disk_monitor.launcher.ProcessRunner.launch_game", return_value=mock_proc) as mock_launch:
            result = app.handle_drive_inserted(drive)
            self.assertEqual(result.get("action"), "launched")
            mock_launch.assert_called_once()
            _, kwargs = mock_launch.call_args
            self.assertEqual(kwargs.get("cwd"), str(sub_work_dir.resolve()))

    def test_multi_disc_flow_with_spaces_and_unicode_under_active_tray(self):
        """
        Integration test verifying a multi-disc flow (Disc 1 install + Disc 2 launch)
        with paths containing spaces and non-ASCII Unicode characters while the system tray
        is actively running.
        """
        install_dest = self.base_path / "GOG Gry" / "Wiedźmin 3 Dziki Gon™"
        install_dest.mkdir(parents=True, exist_ok=True)
        game_binary = install_dest / "bin" / "wiedźmin3.exe"
        game_binary.parent.mkdir(parents=True, exist_ok=True)

        # Create Disc 1 mock
        disc1_dir = self.base_path / "disc1"
        disc1_dir.mkdir(parents=True, exist_ok=True)
        setup1_script = disc1_dir / "instaluj.bat"
        setup1_script.write_text(
            f"@echo off\nchcp 65001 >nul\nmkdir \"{game_binary.parent}\" 2>nul\necho mock_binary > \"{game_binary}\"\nexit /b 0\n",
            encoding="utf-8",
        )

        cfg_disc1 = {
            "schema_version": "1.0",
            "game_id": "witcher_3_pl",
            "title": "Wiedźmin 3: Dziki Gon™",
            "version": "4.04",
            "setup": {
                "executable": "instaluj.bat",
                "arguments": [
                    f'/dir="{install_dest}"',
                    "/SILENT",
                ],
                "default_install_subdir": "Wiedźmin 3 Dziki Gon™",
            },
            "launcher": {
                "executable": "bin/wiedźmin3.exe",
                "arguments": ["-dx12", "-lang=pl-PL"],
            },
            "disk_info": {
                "disc_number": 1,
                "total_discs": 2,
                "label": "W3_DISC1",
            },
        }
        (disc1_dir / "gog_game.json").write_text(json.dumps(cfg_disc1), encoding="utf-8")

        # Create Disc 2 mock
        disc2_dir = self.base_path / "disc2"
        disc2_dir.mkdir(parents=True, exist_ok=True)
        cfg_disc2 = dict(cfg_disc1)
        cfg_disc2["disk_info"] = {
            "disc_number": 2,
            "total_discs": 2,
            "label": "W3_DISC2",
        }
        (disc2_dir / "gog_game.json").write_text(json.dumps(cfg_disc2), encoding="utf-8")

        # Mock detector simulating Disc 1 followed by Disc 2
        mock_detector = MagicMock(spec=WindowsDriveDetector)
        mock_detector.get_logical_drives_mask.return_value = 0
        mock_detector.get_drive_letters_from_mask.return_value = set()

        monitor = DriveMonitor(
            poll_interval=0.05,
            detector=mock_detector,
            auto_suppress_errors=False,
        )

        app = GOGDiskMonitorApp(
            state_store=self.store,
            drive_monitor=monitor,
            poll_interval=0.05,
            auto_confirm=True,
            headless=False,
            install_root=str(self.base_path / "GOG Gry"),
        )

        app.start(block=False)
        try:
            self.assertTrue(app.is_running)
            self.assertTrue(app.tray.is_running())

            # Step 1: Insert Disc 1 (Install Flow)
            disc1_info = DriveInfo(
                letter="V:",
                drive_type="CDROM",
                volume_name="W3_DISC1",
                root_path=f"{disc1_dir}\\",
                is_ready=True,
            )
            mock_detector.inspect_drive.return_value = disc1_info
            mock_detector.get_logical_drives_mask.return_value = (1 << 21)  # V:
            mock_detector.get_drive_letters_from_mask.return_value = {"V:"}

            # Wait for Disc 1 to install
            start = time.time()
            installed = False
            while time.time() - start < 4.0:
                if self.store.is_installed("witcher_3_pl", verify_executable=True):
                    installed = True
                    break
                time.sleep(0.05)

            self.assertTrue(installed, "Disc 1 was not installed in real time.")
            record = self.store.get_game("witcher_3_pl")
            self.assertEqual(record.title, "Wiedźmin 3: Dziki Gon™")
            self.assertEqual(record.last_disk_label, "W3_DISC1")

            # Step 2: Remove Disc 1
            mock_detector.get_logical_drives_mask.return_value = 0
            mock_detector.get_drive_letters_from_mask.return_value = set()
            time.sleep(0.1)

            # Step 3: Insert Disc 2 (Auto-Launch Flow)
            disc2_info = DriveInfo(
                letter="V:",
                drive_type="CDROM",
                volume_name="W3_DISC2",
                root_path=f"{disc2_dir}\\",
                is_ready=True,
            )
            mock_detector.inspect_drive.return_value = disc2_info
            mock_detector.get_logical_drives_mask.return_value = (1 << 21)  # V:
            mock_detector.get_drive_letters_from_mask.return_value = {"V:"}

            mock_proc = MagicMock()
            mock_proc.pid = 55443
            with patch("gog_disk_monitor.launcher.ProcessRunner.launch_game", return_value=mock_proc) as mock_launch:
                start = time.time()
                launched = False
                while time.time() - start < 4.0:
                    if mock_launch.called:
                        launched = True
                        break
                    time.sleep(0.05)

                self.assertTrue(launched, "Disc 2 insertion did not auto-launch game.")
                mock_launch.assert_called_once()
                # Verify state updated with Disc 2 info and launch timestamp
                rec2 = self.store.get_game("witcher_3_pl")
                self.assertIsNotNone(rec2.last_launched_at)
                self.assertEqual(rec2.last_disk_label, "W3_DISC2")

        finally:
            app.stop()
            self.assertFalse(app.is_running)


if __name__ == "__main__":
    unittest.main()

