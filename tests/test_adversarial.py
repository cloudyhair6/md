"""
tests.test_adversarial
~~~~~~~~~~~~~~~~~~~~~~~

Adversarial Stress Test Suite for GOG Game Disk Monitor.
Tests concurrency, rapid mount/unmount loops, multi-disk race conditions,
corrupted JSON scenarios, path traversal/injection, and fault injection.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock
import unittest

from PIL import Image

from gog_disk_monitor.config import (
    GOGDiskConfig,
    LauncherConfig,
    SetupConfig,
    find_disk_icon,
    parse_disk_config,
)
from gog_disk_monitor.state import InstalledGameRecord, StateStore
from gog_disk_monitor.drive_monitor import DriveInfo, DriveMonitor, WindowsDriveDetector, DriveSimulator
from gog_disk_monitor.launcher import ProcessRunner, ProcessExecutionError
from gog_disk_monitor.app import GOGDiskMonitorApp


class TestAdversarialCorruptedJSON(unittest.TestCase):
    """Stress-tests StateStore and ConfigParser against corrupted, malformed, and adversarial JSON."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="adv_json_test_")
        self.state_file = os.path.join(self.temp_dir, "installed_games.json")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_state_store_truncated_json_at_all_byte_offsets(self):
        """Verify StateStore recovers safely when state file is truncated at any byte offset."""
        sample_data = {
            "version": "1.0",
            "last_updated": "2026-08-26T12:00:00Z",
            "games": {
                "game_1": {
                    "game_id": "game_1",
                    "title": "Adversarial Game 1",
                    "version": "1.0.0",
                    "installed_path": "C:\\Games\\Game1",
                    "executable_path": "C:\\Games\\Game1\\game.exe",
                    "installed_at": "2026-08-26T12:00:00Z"
                },
                "game_2": {
                    "game_id": "game_2",
                    "title": "Adversarial Game 2",
                    "version": "2.0.0",
                    "installed_path": "C:\\Games\\Game2",
                    "executable_path": "C:\\Games\\Game2\\game.exe",
                    "installed_at": "2026-08-26T12:05:00Z"
                }
            }
        }
        full_json = json.dumps(sample_data, indent=2)

        # Test truncation at every 5th byte
        for length in range(1, len(full_json), 5):
            with open(self.state_file, "w", encoding="utf-8") as f:
                f.write(full_json[:length])

            store = StateStore(self.state_file)
            # Store should self-heal, return empty dict or valid recovery, and allow writes
            self.assertIsInstance(store.get_all_installed(), dict)
            # Verify we can mark a new game after recovery
            record = InstalledGameRecord(
                game_id=f"rec_after_trunc_{length}",
                title="Recovery Game",
                version="1.0",
                installed_path="C:\\Games\\Rec",
                executable_path="C:\\Games\\Rec\\game.exe",
                installed_at="2026-08-26T12:00:00Z"
            )
            store.mark_installed(record)
            self.assertTrue(store.is_installed(f"rec_after_trunc_{length}"))

    def test_state_store_null_bytes_and_binary_garbage(self):
        """Verify StateStore handles binary garbage, null bytes, and non-UTF8 data."""
        garbage_samples = [
            b"\x00" * 256,
            b"\xff\xfe\x00\x00\x12\x34\x56\x78",
            b"\x80\x81\x82\x83\x84\x85\x86",
            b"{\x00\"games\": \x00null}",
            b"\xef\xbb\xbfINVALID_NON_JSON",
        ]
        for idx, garbage in enumerate(garbage_samples):
            with open(self.state_file, "wb") as f:
                f.write(garbage)

            store = StateStore(self.state_file)
            self.assertEqual(len(store.get_all_installed()), 0)
            self.assertFalse(store.is_installed("any_game"))

            # Must be able to write cleanly after corruption
            rec = InstalledGameRecord(
                game_id=f"test_after_garbage_{idx}",
                title=f"Garbage Recovery {idx}",
                version="1.0",
                installed_path="C:\\G",
                executable_path="C:\\G\\g.exe",
                installed_at="now"
            )
            store.mark_installed(rec)
            self.assertTrue(store.is_installed(f"test_after_garbage_{idx}"))

    def test_config_parser_adversarial_payloads(self):
        """Stress-test parse_disk_config against diverse malicious or malformed files."""
        bad_configs = [
            "",  # empty
            "null",
            "true",
            "12345",
            "\"string only\"",
            "[]",
            "[{'game_id': 'foo'}]",
            "{'game_id': 12345}",
            "{\"game_id\": \"\", \"title\": \"test\"}",
            "{\"game_id\": \"test\", \"title\": null}",
            "{\"game_id\": \"test\", \"title\": \"test\", \"setup\": []}",
            "{\"game_id\": \"test\", \"title\": \"test\", \"setup\": {\"executable\": null}}",
            "{\"game_id\": \"test\", \"title\": \"test\", \"setup\": {\"executable\": \"\"}}",
            "{\"game_id\": \"test\", \"title\": \"test\", \"setup\": {\"executable\": \"s.exe\"}, \"launcher\": \"not_dict\"}",
        ]

        cfg_dir = os.path.join(self.temp_dir, "cfg_test")
        os.makedirs(cfg_dir, exist_ok=True)
        cfg_file = os.path.join(cfg_dir, "gog_game.json")

        for payload in bad_configs:
            with open(cfg_file, "w", encoding="utf-8") as f:
                f.write(payload)
            res = parse_disk_config(cfg_dir)
            self.assertIsNone(res, f"Expected None for adversarial payload: {payload!r}")


class TestAdversarialConcurrency(unittest.TestCase):
    """Stress-tests multi-threaded concurrency, rapid operations, and race conditions."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="adv_concurr_test_")
        self.state_file = os.path.join(self.temp_dir, "state.json")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_concurrent_multi_process_state_store_hammer(self):
        """50 threads hammering StateStore with concurrent mark, unmark, and queries."""
        store = StateStore(self.state_file)
        num_threads = 50
        ops_per_thread = 20

        errors = []

        def worker(thread_id: int):
            try:
                for op in range(ops_per_thread):
                    game_id = f"game_{thread_id}_{op % 5}"
                    if op % 3 == 0:
                        rec = InstalledGameRecord(
                            game_id=game_id,
                            title=f"Title {thread_id}-{op}",
                            version="1.0.0",
                            installed_path=f"C:\\Games\\{game_id}",
                            executable_path=f"C:\\Games\\{game_id}\\game.exe",
                            installed_at="2026-08-26T12:00:00Z"
                        )
                        store.mark_installed(rec)
                    elif op % 3 == 1:
                        store.is_installed(game_id)
                        store.get_game(game_id)
                    else:
                        store.unmark_installed(game_id)
            except Exception as e:
                errors.append((thread_id, e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrency errors encountered: {errors}")

        # Verify state store file is intact and valid JSON
        reloaded = StateStore(self.state_file)
        all_games = reloaded.get_all_installed()
        self.assertIsInstance(all_games, dict)

    def test_concurrent_app_drive_insert_and_remove_races(self):
        """Simulate concurrent insertion and removal events across 30 tasks."""
        app = GOGDiskMonitorApp(
            state_file_path=self.state_file,
            install_root=self.temp_dir,
            auto_confirm=True,
            headless=True
        )

        errors = []
        num_iterations = 30

        def event_worker(i: int):
            try:
                drive_letter = f"V{i % 5}:"
                # Mock drive info
                info = DriveInfo(
                    letter=drive_letter,
                    drive_type="REMOVABLE",
                    is_ready=True,
                    root_path=self.temp_dir
                )
                app.handle_drive_inserted(info)
                app.handle_drive_removed(drive_letter)
            except Exception as ex:
                errors.append(ex)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(event_worker, i) for i in range(num_iterations)]
            concurrent.futures.wait(futures)

        self.assertEqual(len(errors), 0, f"Encountered errors in concurrent event handling: {errors}")


class TestAdversarialPathTraversalAndSecurity(unittest.TestCase):
    """Tests path traversal, shell injection attempts, and boundary path conditions."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="adv_sec_test_")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_path_traversal_in_config_icon(self):
        """find_disk_icon with path traversal escaping disk root."""
        cfg = GOGDiskConfig(
            game_id="sec_test",
            title="Sec Test",
            version="1.0",
            setup=SetupConfig(executable="setup.exe"),
            launcher=LauncherConfig(executable="game.exe"),
            icon_path="../../../../../../../Windows/System32/notepad.exe"
        )
        icon = find_disk_icon(self.temp_dir, cfg)
        if icon is not None:
            self.assertTrue(os.path.isabs(icon))

    def test_missing_and_invalid_setup_runner(self):
        """ProcessRunner correctly raises FileNotFoundError for missing executables and non-executables."""
        with self.assertRaises(FileNotFoundError):
            ProcessRunner.run_setup("C:\\NonExistent_Folder_XYZ\\nonexistent_setup.exe")

        with self.assertRaises(FileNotFoundError):
            ProcessRunner.run_setup(self.temp_dir)

    def test_run_setup_timeout_adversarial(self):
        """Verify ProcessRunner terminates hanging process on timeout without deadlock."""
        hang_script = os.path.join(self.temp_dir, "hang.py")
        with open(hang_script, "w") as f:
            f.write("import time\ntime.sleep(10)\n")

        start = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            ProcessRunner.run_setup(
                setup_exe_path=sys.executable,
                args=[hang_script],
                timeout_seconds=0.3
            )
        elapsed = time.time() - start
        self.assertLess(elapsed, 3.0, "Timeout expired was not handled promptly!")


class TestAdversarialMediaSwapAndDriveMonitor(unittest.TestCase):
    """Stress tests drive monitor detection, rapid media swaps, and multi-drive polling."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="adv_media_test_")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_simulated_rapid_disc_swap_sequence(self):
        """Simulate rapid disc insertions, swaps, and ejections on optical drive."""
        events = []
        cv = threading.Condition()

        def on_ins(info: DriveInfo):
            with cv:
                events.append(("inserted", info.letter, info.volume_name, info.serial_number))
                cv.notify_all()

        def on_rem(letter: str):
            with cv:
                events.append(("removed", letter))
                cv.notify_all()

        monitor = DriveMonitor(poll_interval=0.05, on_inserted=on_ins, on_removed=on_rem)

        # Mock detector with simulated media changes
        detector = MagicMock()
        monitor._detector = detector

        d1 = DriveInfo(letter="E:", drive_type="CDROM", is_ready=True, volume_name="DISC_1", serial_number=1111)
        d2 = DriveInfo(letter="E:", drive_type="CDROM", is_ready=True, volume_name="DISC_2", serial_number=2222)
        d_empty = DriveInfo(letter="E:", drive_type="CDROM", is_ready=False, volume_name="", serial_number=None)

        # Step 1: Initial disc insertion (E: added)
        detector.get_logical_drives_mask.return_value = (1 << 4)  # E: bit
        detector.get_drive_letters_from_mask.return_value = {"E:"}
        detector.inspect_drive.return_value = d1

        monitor._poll_step()
        self.assertIn(("inserted", "E:", "DISC_1", 1111), events)

        # Step 2: Same disc poll (deduplicated, no new events)
        events.clear()
        monitor._poll_step()
        self.assertEqual(len(events), 0)

        # Step 3: Disc Swap (DISC_1 -> DISC_2)
        events.clear()
        detector.inspect_drive.return_value = d2
        monitor._poll_step()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0], ("removed", "E:"))
        self.assertEqual(events[1], ("inserted", "E:", "DISC_2", 2222))

        # Step 4: Disc Ejection (DISC_2 -> empty)
        events.clear()
        detector.inspect_drive.return_value = d_empty
        monitor._poll_step()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], ("removed", "E:"))

        # Step 5: Remount DISC_1
        events.clear()
        detector.inspect_drive.return_value = d1
        monitor._poll_step()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0], ("inserted", "E:", "DISC_1", 1111))


if __name__ == "__main__":
    unittest.main()
