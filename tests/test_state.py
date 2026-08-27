"""
tests.test_state
~~~~~~~~~~~~~~~~

Unit tests for GOG Game Disk Monitor local PC state management, atomic persistence,
InstalledGameRecord models, and self-healing corruption recovery (Feature F2).
"""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gog_disk_monitor.state import (
    InstalledGameRecord,
    StateStore,
)


class TestStateStore(unittest.TestCase):
    """Test suite for StateStore local PC persistence and recovery."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp_dir.name) / "installed_games.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_record(
        self,
        game_id: str = "witcher_3",
        title: str = "The Witcher 3: Wild Hunt",
        version: str = "4.04",
        install_path: str = "C:\\Games\\Witcher3",
        executable_path: str = "C:\\Games\\Witcher3\\bin\\x64\\witcher3.exe",
        disk_label: str = "W3_WILD_HUNT",
    ) -> InstalledGameRecord:
        return InstalledGameRecord(
            game_id=game_id,
            title=title,
            version=version,
            install_path=install_path,
            executable_path=executable_path,
            disk_label=disk_label,
        )

    def test_fresh_state_nonexistent_file(self) -> None:
        """STA-01: Non-existent state file initializes clean empty store."""
        store = StateStore(self.state_file)
        self.assertFalse(store.is_installed("witcher_3"))
        self.assertIsNone(store.get_game("witcher_3"))
        self.assertEqual(store.get_all_installed(), {})

    def test_mark_installed_single_game(self) -> None:
        """STA-02: Marking a game installed commits to disk and updates queries."""
        store = StateStore(self.state_file)
        record = self._create_record()
        store.mark_installed(record)

        # In-memory query
        self.assertTrue(store.is_installed("witcher_3"))
        retrieved = store.get_game("witcher_3")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.game_id, "witcher_3")
        self.assertEqual(retrieved.title, "The Witcher 3: Wild Hunt")
        self.assertEqual(retrieved.version, "4.04")
        self.assertEqual(retrieved.installed_path, "C:\\Games\\Witcher3")
        self.assertEqual(retrieved.disk_label, "W3_WILD_HUNT")

        # Verify disk file content
        self.assertTrue(self.state_file.is_file())
        with open(self.state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["schema_version"], "1.0")
        self.assertIn("witcher_3", data["games"])
        self.assertEqual(data["games"]["witcher_3"]["title"], "The Witcher 3: Wild Hunt")

    def test_mark_installed_nested_directory_creation(self) -> None:
        """STA-03: Auto-creates intermediate directories if they do not exist."""
        nested_file = Path(self.temp_dir.name) / "sub1" / "sub2" / "state.json"
        store = StateStore(nested_file)
        record = self._create_record(game_id="cyberpunk")
        store.mark_installed(record)

        self.assertTrue(nested_file.is_file())
        self.assertTrue(store.is_installed("cyberpunk"))

    def test_persistence_across_store_reloads(self) -> None:
        """STA-04: Multiple store instances read committed records."""
        store_a = StateStore(self.state_file)
        store_a.mark_installed(self._create_record(game_id="game_a", title="Game A"))
        store_a.mark_installed(self._create_record(game_id="game_b", title="Game B"))

        # Create new store instance pointing to same file
        store_b = StateStore(self.state_file)
        self.assertTrue(store_b.is_installed("game_a"))
        self.assertTrue(store_b.is_installed("game_b"))
        self.assertEqual(store_b.get_game("game_a").title, "Game A")
        self.assertEqual(store_b.get_game("game_b").title, "Game B")

    def test_update_existing_game_record(self) -> None:
        """STA-05: Re-marking a game updates attributes without duplicate entries."""
        store = StateStore(self.state_file)
        rec_v1 = self._create_record(game_id="game_1", version="1.0.0")
        store.mark_installed(rec_v1)

        rec_v2 = self._create_record(
            game_id="game_1",
            version="2.0.0",
            install_path="D:\\NewGames\\Game1",
        )
        store.mark_installed(rec_v2)

        self.assertEqual(len(store.get_all_installed()), 1)
        updated = store.get_game("game_1")
        self.assertEqual(updated.version, "2.0.0")
        self.assertEqual(updated.installed_path, "D:\\NewGames\\Game1")

    def test_unmark_installed_existing_game(self) -> None:
        """STA-06: Unmarking an installed game removes it from state and disk."""
        store = StateStore(self.state_file)
        store.mark_installed(self._create_record(game_id="remove_me"))
        self.assertTrue(store.is_installed("remove_me"))

        result = store.unmark_installed("remove_me")
        self.assertTrue(result)
        self.assertFalse(store.is_installed("remove_me"))
        self.assertIsNone(store.get_game("remove_me"))

        # Verify disk persistence
        store_reloaded = StateStore(self.state_file)
        self.assertFalse(store_reloaded.is_installed("remove_me"))

    def test_unmark_installed_nonexistent_game(self) -> None:
        """STA-07: Unmarking non-existent game returns False gracefully."""
        store = StateStore(self.state_file)
        result = store.unmark_installed("non_existent_game")
        self.assertFalse(result)

    def test_get_all_installed_multiple_records(self) -> None:
        """STA-08: Querying all installed games returns full collection."""
        store = StateStore(self.state_file)
        for i in range(10):
            store.mark_installed(self._create_record(game_id=f"game_{i}", title=f"Game {i}"))

        all_games = store.get_all_installed()
        self.assertEqual(len(all_games), 10)
        for i in range(10):
            self.assertIn(f"game_{i}", all_games)
            self.assertEqual(all_games[f"game_{i}"].title, f"Game {i}")

    def test_atomic_write_guarantee(self) -> None:
        """STA-09: Atomic write cleans temporary files and ensures data integrity."""
        store = StateStore(self.state_file)
        store.mark_installed(self._create_record(game_id="atomic_game"))

        parent_dir = self.state_file.parent
        tmp_files = list(parent_dir.glob("*.tmp.*"))
        self.assertEqual(len(tmp_files), 0, "Temporary files should be removed after save")

    def test_corrupt_json_self_healing_backup(self) -> None:
        """STA-10: Corrupt JSON triggers backup creation and recovers clean store."""
        # Write corrupted JSON to state file
        with open(self.state_file, "w", encoding="utf-8") as f:
            f.write('{"games": {"bad_entry": {incomplete...')

        store = StateStore(self.state_file)
        self.assertEqual(len(store.get_all_installed()), 0)

        # Verify backup was created
        backups = list(self.state_file.parent.glob("*.corrupt.*.bak"))
        self.assertGreaterEqual(len(backups), 1)

        # Verify store can now save and work properly
        store.mark_installed(self._create_record(game_id="recovered_game"))
        self.assertTrue(store.is_installed("recovered_game"))

    def test_empty_zero_byte_state_file(self) -> None:
        """STA-11: 0-byte state file treated as empty store without error."""
        with open(self.state_file, "wb") as f:
            f.write(b"")

        store = StateStore(self.state_file)
        self.assertEqual(store.get_all_installed(), {})
        self.assertFalse(store.is_installed("any_game"))

    def test_invalid_json_schema_root(self) -> None:
        """STA-12: Non-object JSON root recovers safely."""
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(["not", "a", "dict"], f)

        store = StateStore(self.state_file)
        self.assertEqual(store.get_all_installed(), {})

        # Non-dict games field
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump({"schema_version": "1.0", "games": "not_a_dict"}, f)

        store_2 = StateStore(self.state_file)
        self.assertEqual(store_2.get_all_installed(), {})

    def test_record_dataclass_serialization(self) -> None:
        """STA-13: InstalledGameRecord serialization and aliased properties."""
        rec = InstalledGameRecord(
            game_id="witcher_3",
            title="The Witcher 3",
            version="4.04",
            installed_path="C:\\Games\\W3",
            executable_path="C:\\Games\\W3\\bin\\w3.exe",
            installed_at="2026-08-26T12:00:00Z",
            disk_label="W3_DISK",
            custom_args=["-dx11"],
        )

        d = rec.to_dict()
        self.assertEqual(d["game_id"], "witcher_3")
        self.assertEqual(d["install_path"], "C:\\Games\\W3")
        self.assertEqual(d["installed_path"], "C:\\Games\\W3")
        self.assertEqual(d["disk_label"], "W3_DISK")
        self.assertEqual(d["last_disk_label"], "W3_DISK")
        self.assertEqual(d["custom_args"], ["-dx11"])

        # Deserialize from dict
        rec2 = InstalledGameRecord.from_dict(d)
        self.assertEqual(rec2.game_id, "witcher_3")
        self.assertEqual(rec2.install_path, "C:\\Games\\W3")
        self.assertEqual(rec2.installed_path, "C:\\Games\\W3")
        self.assertEqual(rec2.disk_label, "W3_DISK")
        self.assertEqual(rec2.last_disk_label, "W3_DISK")

        # Setter aliases
        rec2.installed_path = "D:\\NewPath"
        self.assertEqual(rec2.install_path, "D:\\NewPath")
        rec2.disk_label = "NEW_LABEL"
        self.assertEqual(rec2.last_disk_label, "NEW_LABEL")

    def test_default_state_path_resolution(self) -> None:
        """STA-14: Resolves to APPDATA, user home, or current working directory fallback."""
        # 1. APPDATA set
        with patch.dict(os.environ, {"APPDATA": self.temp_dir.name}):
            store = StateStore()
            resolved = store.get_state_file_path()
            expected = Path(self.temp_dir.name) / "GOGDiskMonitor" / "installed_games.json"
            self.assertEqual(resolved, expected.resolve())

        # 2. APPDATA empty, Path.home() returns custom path
        mock_home = Path(self.temp_dir.name) / "mock_home"
        with patch.dict(os.environ, {"APPDATA": ""}):
            with patch.object(Path, "home", return_value=mock_home):
                store = StateStore()
                resolved = store.get_state_file_path()
                expected = mock_home / ".gog_disk_monitor" / "installed_games.json"
                self.assertEqual(resolved, expected.resolve())

        # 3. APPDATA empty, Path.home() raises RuntimeError -> fallback to cwd
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(Path, "home", side_effect=RuntimeError("Could not determine home directory.")):
                store = StateStore()
                resolved = store.get_state_file_path()
                expected = Path.cwd() / ".gog_disk_monitor" / "installed_games.json"
                self.assertEqual(resolved, expected.resolve())

        # 4. Cleared environment directly (standard fallback)
        with patch.dict(os.environ, {}, clear=True):
            store = StateStore()
            resolved = store.get_state_file_path()
            self.assertTrue(str(resolved).endswith(os.path.normpath(".gog_disk_monitor/installed_games.json")))

    def test_unicode_support_in_state(self) -> None:
        """STA-15: Non-ASCII paths and titles preserve 100% fidelity."""
        store = StateStore(self.state_file)
        unicode_rec = self._create_record(
            game_id="witcher_pl",
            title="Wiedźmin 3: Dziki Gon™ — 極限",
            install_path="C:\\Gry\\Wiedźmin 3\\폴더",
            executable_path="C:\\Gry\\Wiedźmin 3\\폴더\\gra.exe",
            disk_label="WIEDŹMIN_D1",
        )
        store.mark_installed(unicode_rec)

        store_reloaded = StateStore(self.state_file)
        loaded = store_reloaded.get_game("witcher_pl")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.title, "Wiedźmin 3: Dziki Gon™ — 極限")
        self.assertEqual(loaded.install_path, "C:\\Gry\\Wiedźmin 3\\폴더")
        self.assertEqual(loaded.disk_label, "WIEDŹMIN_D1")

    def test_update_launch_time(self) -> None:
        """Update last launched timestamp and disk metadata."""
        store = StateStore(self.state_file)
        store.mark_installed(self._create_record(game_id="launch_game"))

        updated = store.update_launch_time("launch_game", drive_letter="E:", disk_label="E_DRIVE")
        self.assertTrue(updated)

        rec = store.get_game("launch_game")
        self.assertIsNotNone(rec.last_launched_at)
        self.assertEqual(rec.last_disk_drive, "E:")
        self.assertEqual(rec.last_disk_label, "E_DRIVE")

        # Nonexistent game update
        self.assertFalse(store.update_launch_time("missing_game"))

    def test_is_installed_verify_executable(self) -> None:
        """Executable verification flag detects missing launcher files."""
        store = StateStore(self.state_file)
        dummy_exe = Path(self.temp_dir.name) / "game.exe"
        with open(dummy_exe, "w") as f:
            f.write("exe")

        rec = self._create_record(game_id="real_game", executable_path=str(dummy_exe))
        store.mark_installed(rec)

        # Exists -> True
        self.assertTrue(store.is_installed("real_game", verify_executable=True))

        # Delete exe -> False and marked broken
        dummy_exe.unlink()
        self.assertFalse(store.is_installed("real_game", verify_executable=True))
        self.assertEqual(store.get_game("real_game").status, "broken_missing_exe")

    def test_multithreaded_concurrency(self) -> None:
        """Concurrent writes and queries across 20 threads maintain integrity."""
        store = StateStore(self.state_file)

        def worker(thread_idx: int) -> None:
            gid = f"thread_game_{thread_idx}"
            store.mark_installed(self._create_record(game_id=gid, title=f"Game {thread_idx}"))
            self.assertTrue(store.is_installed(gid))
            store.update_launch_time(gid, drive_letter="Z:", disk_label=f"DISK_{thread_idx}")
            self.assertIsNotNone(store.get_game(gid))

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(worker, i) for i in range(20)]
            for future in futures:
                future.result()

        all_games = store.get_all_installed()
        self.assertEqual(len(all_games), 20)


if __name__ == "__main__":
    unittest.main()
