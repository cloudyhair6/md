"""
tests.test_config
~~~~~~~~~~~~~~~~~

Unit tests for GOG Game Disk Monitor configuration models, descriptor parser,
and icon resolution hierarchy (Feature F1).
"""

import json
import os
from pathlib import Path
import tempfile
import unittest

from gog_disk_monitor.config import (
    SetupConfig,
    LauncherConfig,
    GOGDiskConfig,
    parse_disk_config,
    find_disk_icon,
    normalize_disk_root,
    sanitize_argument,
    parse_and_sanitize_arguments,
)


class TestConfigParser(unittest.TestCase):
    """Test suite for disk configuration parsing and data models."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.disk_root = self.temp_dir.name

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_config(self, filename: str, data: dict, encoding: str = "utf-8") -> str:
        filepath = os.path.join(self.disk_root, filename)
        with open(filepath, "w", encoding=encoding) as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return filepath

    def _write_raw(self, filename: str, raw_content: bytes) -> str:
        filepath = os.path.join(self.disk_root, filename)
        with open(filepath, "wb") as f:
            f.write(raw_content)
        return filepath

    def test_parse_valid_full_config(self) -> None:
        """CFG-01: Full valid descriptor with all metadata attributes."""
        payload = {
            "schema_version": "1.0",
            "game_id": "witcher_3",
            "title": "The Witcher 3: Wild Hunt",
            "version": "4.04",
            "developer": "CD PROJEKT RED",
            "publisher": "CD PROJEKT",
            "icon_path": "assets/game.ico",
            "setup": {
                "executable": "installer/setup.exe",
                "arguments": ["/SILENT", "/DIR=C:\\Games\\W3"],
                "default_install_subdir": "The Witcher 3 Wild Hunt",
                "estimated_size_mb": 52000,
                "silent_supported": True,
            },
            "launcher": {
                "executable": "bin/x64/witcher3.exe",
                "arguments": ["-dx12"],
                "working_directory": "bin/x64",
                "requires_admin": False,
            },
            "disk_info": {
                "disc_number": 1,
                "total_discs": 1,
                "label": "W3_WILD_HUNT",
            },
        }
        self._write_config("gog_game.json", payload)

        config = parse_disk_config(self.disk_root)
        self.assertIsNotNone(config)
        self.assertEqual(config.game_id, "witcher_3")
        self.assertEqual(config.title, "The Witcher 3: Wild Hunt")
        self.assertEqual(config.version, "4.04")
        self.assertEqual(config.developer, "CD PROJEKT RED")
        self.assertEqual(config.publisher, "CD PROJEKT")
        self.assertEqual(config.icon_path, os.path.normpath("assets/game.ico"))
        self.assertEqual(config.schema_version, "1.0")

        # Setup assertions
        self.assertEqual(config.setup.executable, os.path.normpath("installer/setup.exe"))
        self.assertEqual(config.setup.arguments, ["/SILENT", "/DIR=C:\\Games\\W3"])
        self.assertEqual(config.setup.default_install_subdir, "The Witcher 3 Wild Hunt")
        self.assertEqual(config.setup.estimated_size_mb, 52000)
        self.assertTrue(config.setup.silent_supported)

        # Launcher assertions
        self.assertEqual(config.launcher.executable, os.path.normpath("bin/x64/witcher3.exe"))
        self.assertEqual(config.launcher.arguments, ["-dx12"])
        self.assertEqual(config.launcher.working_directory, os.path.normpath("bin/x64"))
        self.assertFalse(config.launcher.requires_admin)

        # Disk info & raw data
        self.assertEqual(config.disk_info["label"], "W3_WILD_HUNT")
        self.assertEqual(config.disk_info["disc_number"], 1)
        self.assertIn("schema_version", config.raw_data)

    def test_parse_valid_minimal_config(self) -> None:
        """CFG-02: Minimal valid descriptor with default values."""
        payload = {
            "game_id": "minimal_game",
            "title": "Minimal Game",
            "version": "1.0.0",
            "setup": {
                "executable": "setup.exe"
            },
            "launcher": {
                "executable": "game.exe"
            },
        }
        self._write_config("gog_game.json", payload)

        config = parse_disk_config(self.disk_root)
        self.assertIsNotNone(config)
        self.assertEqual(config.game_id, "minimal_game")
        self.assertEqual(config.title, "Minimal Game")
        self.assertEqual(config.version, "1.0.0")
        self.assertEqual(config.setup.executable, "setup.exe")
        self.assertEqual(config.setup.arguments, [])
        self.assertIsNone(config.setup.estimated_size_mb)
        self.assertFalse(config.setup.silent_supported)
        self.assertEqual(config.launcher.executable, "game.exe")
        self.assertEqual(config.launcher.arguments, [])
        self.assertIsNone(config.launcher.working_directory)
        self.assertFalse(config.launcher.requires_admin)
        self.assertIsNone(config.icon_path)
        self.assertIsNone(config.publisher)
        self.assertIsNone(config.developer)
        self.assertEqual(config.disk_info, {})

    def test_parse_missing_config_file(self) -> None:
        """CFG-03: Empty disk root with no config file returns None."""
        config = parse_disk_config(self.disk_root)
        self.assertIsNone(config)

    def test_parse_nonexistent_directory(self) -> None:
        """CFG-04: Nonexistent path returns None gracefully."""
        nonexistent = os.path.join(self.disk_root, "does_not_exist_xyz_123")
        config = parse_disk_config(nonexistent)
        self.assertIsNone(config)

    def test_parse_malformed_json_syntax(self) -> None:
        """CFG-05: Truncated/corrupted JSON returns None."""
        self._write_raw("gog_game.json", b'{"game_id": "bad", "title":')
        config = parse_disk_config(self.disk_root)
        self.assertIsNone(config)

    def test_parse_empty_file(self) -> None:
        """CFG-06: Zero-byte file returns None."""
        self._write_raw("gog_game.json", b"")
        config = parse_disk_config(self.disk_root)
        self.assertIsNone(config)

    def test_parse_json_root_not_dict(self) -> None:
        """CFG-07: JSON root array or primitive returns None."""
        self._write_raw("gog_game.json", b'["item1", "item2"]')
        self.assertIsNone(parse_disk_config(self.disk_root))

        self._write_raw("gog_game.json", b'"just a string"')
        self.assertIsNone(parse_disk_config(self.disk_root))

        self._write_raw("gog_game.json", b"12345")
        self.assertIsNone(parse_disk_config(self.disk_root))

    def test_parse_missing_game_id(self) -> None:
        """CFG-08: Missing or whitespace-only game_id returns None."""
        payload = {
            "title": "No ID Game",
            "version": "1.0",
            "setup": {"executable": "setup.exe"},
            "launcher": {"executable": "game.exe"},
        }
        self._write_config("gog_game.json", payload)
        self.assertIsNone(parse_disk_config(self.disk_root))

        payload["game_id"] = "   "
        self._write_config("gog_game.json", payload)
        self.assertIsNone(parse_disk_config(self.disk_root))

    def test_parse_missing_title(self) -> None:
        """CFG-09: Missing or whitespace-only title returns None."""
        payload = {
            "game_id": "game_1",
            "version": "1.0",
            "setup": {"executable": "setup.exe"},
            "launcher": {"executable": "game.exe"},
        }
        self._write_config("gog_game.json", payload)
        self.assertIsNone(parse_disk_config(self.disk_root))

        payload["title"] = ""
        self._write_config("gog_game.json", payload)
        self.assertIsNone(parse_disk_config(self.disk_root))

    def test_parse_missing_version(self) -> None:
        """CFG-10: Missing or whitespace-only version returns None."""
        payload = {
            "game_id": "game_1",
            "title": "Game 1",
            "setup": {"executable": "setup.exe"},
            "launcher": {"executable": "game.exe"},
        }
        self._write_config("gog_game.json", payload)
        self.assertIsNone(parse_disk_config(self.disk_root))

    def test_parse_missing_setup_section(self) -> None:
        """CFG-11: Missing or invalid setup section returns None."""
        payload = {
            "game_id": "game_1",
            "title": "Game 1",
            "version": "1.0",
            "launcher": {"executable": "game.exe"},
        }
        self._write_config("gog_game.json", payload)
        self.assertIsNone(parse_disk_config(self.disk_root))

        payload["setup"] = "setup.exe"
        self._write_config("gog_game.json", payload)
        self.assertIsNone(parse_disk_config(self.disk_root))

    def test_parse_missing_setup_executable(self) -> None:
        """CFG-12: Missing executable in setup section returns None."""
        payload = {
            "game_id": "game_1",
            "title": "Game 1",
            "version": "1.0",
            "setup": {"arguments": ["/SILENT"]},
            "launcher": {"executable": "game.exe"},
        }
        self._write_config("gog_game.json", payload)
        self.assertIsNone(parse_disk_config(self.disk_root))

        payload["setup"]["executable"] = ""
        self._write_config("gog_game.json", payload)
        self.assertIsNone(parse_disk_config(self.disk_root))

    def test_parse_missing_launcher_section(self) -> None:
        """CFG-13: Missing or invalid launcher section returns None."""
        payload = {
            "game_id": "game_1",
            "title": "Game 1",
            "version": "1.0",
            "setup": {"executable": "setup.exe"},
        }
        self._write_config("gog_game.json", payload)
        self.assertIsNone(parse_disk_config(self.disk_root))

    def test_parse_missing_launcher_executable(self) -> None:
        """CFG-14: Missing executable in launcher section returns None."""
        payload = {
            "game_id": "game_1",
            "title": "Game 1",
            "version": "1.0",
            "setup": {"executable": "setup.exe"},
            "launcher": {"arguments": ["-fullscreen"]},
        }
        self._write_config("gog_game.json", payload)
        self.assertIsNone(parse_disk_config(self.disk_root))

    def test_parse_invalid_field_types(self) -> None:
        """CFG-15: Resilient type handling (arguments string, estimated size string)."""
        payload = {
            "game_id": "game_types",
            "title": "Type Test Game",
            "version": "1.0.0",
            "setup": {
                "executable": "setup.exe",
                "args": "/SILENT",
                "estimated_size_mb": "1024",
            },
            "launcher": {
                "executable": "game.exe",
                "args": "-debug",
            },
        }
        self._write_config("gog_game.json", payload)

        config = parse_disk_config(self.disk_root)
        self.assertIsNotNone(config)
        self.assertEqual(config.setup.arguments, ["/SILENT"])
        self.assertEqual(config.setup.estimated_size_mb, 1024)
        self.assertEqual(config.launcher.arguments, ["-debug"])

    def test_parse_extra_unknown_fields(self) -> None:
        """CFG-16: Unknown extra fields are safely ignored for forward compatibility."""
        payload = {
            "game_id": "future_game",
            "title": "Future Game",
            "version": "2.0.0",
            "setup": {"executable": "setup.exe"},
            "launcher": {"executable": "game.exe"},
            "cloud_saves_supported": True,
            "minimum_ram_gb": 16,
            "checksums": {"setup.exe": "abc123sha256"},
        }
        self._write_config("gog_game.json", payload)

        config = parse_disk_config(self.disk_root)
        self.assertIsNotNone(config)
        self.assertEqual(config.game_id, "future_game")
        self.assertTrue(config.raw_data["cloud_saves_supported"])

    def test_parse_unicode_utf8_metadata(self) -> None:
        """CFG-17: Non-ASCII and Unicode characters parsed with 100% fidelity."""
        payload = {
            "game_id": "witcher_pl",
            "title": "Wiedźmin 3: Dziki Gon™ — 極限",
            "version": "4.04-日本語",
            "publisher": "CD PROJEKT RED — Варшава",
            "setup": {"executable": "installer/instaluj.exe"},
            "launcher": {"executable": "bin/gra.exe"},
        }
        self._write_config("gog_game.json", payload)

        config = parse_disk_config(self.disk_root)
        self.assertIsNotNone(config)
        self.assertEqual(config.title, "Wiedźmin 3: Dziki Gon™ — 極限")
        self.assertEqual(config.version, "4.04-日本語")
        self.assertEqual(config.publisher, "CD PROJEKT RED — Варшава")

    def test_parse_utf8_bom(self) -> None:
        """CFG-17B: Config with UTF-8 BOM is transparently decoded."""
        payload = {
            "game_id": "bom_game",
            "title": "BOM Encoded Game",
            "version": "1.0",
            "setup": {"executable": "setup.exe"},
            "launcher": {"executable": "game.exe"},
        }
        json_bytes = json.dumps(payload).encode("utf-8")
        bom_bytes = b"\xef\xbb\xbf" + json_bytes
        self._write_raw("gog_game.json", bom_bytes)

        config = parse_disk_config(self.disk_root)
        self.assertIsNotNone(config)
        self.assertEqual(config.game_id, "bom_game")

    def test_parse_fallback_filename_gog_disk_json(self) -> None:
        """Fallback: gog_disk.json is parsed when gog_game.json is absent."""
        payload = {
            "game_id": "disk_game",
            "title": "Disk Fallback Game",
            "version": "1.0",
            "setup": {"executable": "setup.exe"},
            "launcher": {"executable": "game.exe"},
        }
        self._write_config("gog_disk.json", payload)

        config = parse_disk_config(self.disk_root)
        self.assertIsNotNone(config)
        self.assertEqual(config.game_id, "disk_game")

    def test_parse_path_normalization(self) -> None:
        """CFG-18: Path normalization across Windows and relative conventions."""
        payload = {
            "game_id": "norm_game",
            "title": "Norm Game",
            "version": "1.0",
            "setup": {"executable": "nested/path\\setup.exe"},
            "launcher": {
                "executable": "nested/bin/game.exe",
                "working_directory": "nested/bin",
            },
            "icon_path": "assets/icons/game.ico",
        }
        self._write_config("gog_game.json", payload)

        config = parse_disk_config(self.disk_root)
        self.assertIsNotNone(config)
        self.assertEqual(config.setup.executable, os.path.normpath("nested/path/setup.exe"))
        self.assertEqual(config.launcher.executable, os.path.normpath("nested/bin/game.exe"))
        self.assertEqual(config.launcher.working_directory, os.path.normpath("nested/bin"))
        self.assertEqual(config.icon_path, os.path.normpath("assets/icons/game.ico"))

        # Test normalize_disk_root directly
        self.assertTrue(normalize_disk_root("X:").endswith(os.sep))
        self.assertTrue(normalize_disk_root("X:\\").endswith(os.sep))
        self.assertEqual(normalize_disk_root(""), "")

    def test_parse_quoted_arguments_in_setup_and_launcher(self) -> None:
        """CFG-28: Parsing config containing quoted arguments strips literal quotes from path values."""
        payload = {
            "game_id": "witcher_quotes",
            "title": "Witcher With Quotes",
            "version": "1.0",
            "setup": {
                "executable": "setup.exe",
                "arguments": [
                    '/dir="C:\\GOG Games\\The Witcher 3"',
                    '/SILENT',
                    '/OPTION="value with spaces"',
                ],
            },
            "launcher": {
                "executable": "game.exe",
                "arguments": ['--install-dir="C:\\Program Files\\Game"'],
            },
        }
        self._write_config("gog_game.json", payload)

        config = parse_disk_config(self.disk_root)
        self.assertIsNotNone(config)
        self.assertEqual(
            config.setup.arguments,
            [
                r"/dir=C:\GOG Games\The Witcher 3",
                "/SILENT",
                "/OPTION=value with spaces",
            ],
        )
        self.assertEqual(
            config.launcher.arguments,
            [r"--install-dir=C:\Program Files\Game"],
        )

    def test_parse_single_string_with_quoted_arguments(self) -> None:
        """CFG-29: Parsing single-string arguments field containing quoted /dir switch."""
        payload = {
            "game_id": "string_args_game",
            "title": "String Args Game",
            "version": "1.0",
            "setup": {
                "executable": "setup.exe",
                "arguments": '/dir="C:\\Custom Path" /SILENT /VERYSILENT',
            },
            "launcher": {
                "executable": "game.exe",
                "arguments": '-fullscreen -custom="val"',
            },
        }
        self._write_config("gog_game.json", payload)

        config = parse_disk_config(self.disk_root)
        self.assertIsNotNone(config)
        self.assertEqual(
            config.setup.arguments,
            [r"/dir=C:\Custom Path", "/SILENT", "/VERYSILENT"],
        )
        self.assertEqual(
            config.launcher.arguments,
            ["-fullscreen", "-custom=val"],
        )

    def test_sanitize_argument_unit_variations(self) -> None:
        """CFG-30: Direct unit testing of sanitize_argument on various quoting conventions."""
        self.assertEqual(sanitize_argument('/dir="C:\\Path"'), r"/dir=C:\Path")
        self.assertEqual(sanitize_argument('/DIR="C:\\Games\\Witcher 3"'), r"/DIR=C:\Games\Witcher 3")
        self.assertEqual(sanitize_argument('"/dir=\\"C:\\Path\\""'), r"/dir=C:\Path")
        self.assertEqual(sanitize_argument("'/dir=\"C:\\Path\"'"), r"/dir=C:\Path")
        self.assertEqual(sanitize_argument("/dir='C:\\Path'"), r"/dir=C:\Path")
        self.assertEqual(sanitize_argument('/dir="C:\\Games\\W3\\"'), "/dir=C:\\Games\\W3\\")
        self.assertEqual(sanitize_argument('/DIR:"C:\\Games\\W3"'), r"/DIR:C:\Games\W3")
        self.assertEqual(sanitize_argument('--prefix="C:\\Program Files"'), r"--prefix=C:\Program Files")
        self.assertEqual(sanitize_argument('-D="custom value"'), "-D=custom value")
        self.assertEqual(sanitize_argument("/SILENT"), "/SILENT")
        self.assertEqual(sanitize_argument('"C:\\Games\\Setup.exe"'), r"C:\Games\Setup.exe")
        self.assertEqual(sanitize_argument('"/dir=\\"C:\\GOG Gry\\Wiedźmin 3\\""'), r"/dir=C:\GOG Gry\Wiedźmin 3")
        self.assertEqual(sanitize_argument('\"\"/dir=\"\"C:\\Path\"\"\"\"'), r"/dir=C:\Path")
        self.assertEqual(sanitize_argument('/dir=""'), "/dir=")
        self.assertEqual(sanitize_argument(None), "")
        self.assertEqual(sanitize_argument(""), "")

    def test_parse_and_sanitize_arguments_complex_variations(self) -> None:
        """CFG-31: parse_and_sanitize_arguments with mixed sequences, numbers, and unicode."""
        self.assertEqual(
            parse_and_sanitize_arguments(['/dir="C:\\Path"', '--option="val"', 42]),
            [r"/dir=C:\Path", "--option=val", "42"],
        )
        self.assertEqual(
            parse_and_sanitize_arguments('/dir="C:\\Program Files\\Game" /SILENT --lang="pl-PL"'),
            [r"/dir=C:\Program Files\Game", "/SILENT", "--lang=pl-PL"],
        )
        self.assertEqual(
            parse_and_sanitize_arguments('"/dir=\\"C:\\Program Files (x86)\\GOG Games\\Game\\"" /SILENT'),
            [r"/dir=C:\Program Files (x86)\GOG Games\Game", "/SILENT"],
        )
        self.assertEqual(
            parse_and_sanitize_arguments('\'/dir="C:\\Path with spaces"\' /SILENT'),
            [r"/dir=C:\Path with spaces", "/SILENT"],
        )
        self.assertEqual(parse_and_sanitize_arguments(None), [])
        self.assertEqual(parse_and_sanitize_arguments(False), [])


class TestIconResolver(unittest.TestCase):
    """Test suite for icon discovery and resolution fallback chains."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.disk_root = self.temp_dir.name

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _touch(self, rel_path: str) -> str:
        full_path = os.path.join(self.disk_root, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(b"\x00\x00\x01\x00")
        return full_path

    def _create_mock_config(self, icon_path: str = None, setup_exe: str = "setup.exe") -> GOGDiskConfig:
        return GOGDiskConfig(
            game_id="mock_game",
            title="Mock Game",
            version="1.0",
            setup=SetupConfig(executable=setup_exe),
            launcher=LauncherConfig(executable="game.exe"),
            icon_path=icon_path,
        )

    def test_find_icon_from_config_explicit_valid(self) -> None:
        """CFG-19: Explicit valid icon_path in config resolved."""
        icon_file = self._touch("assets/custom_icon.ico")
        config = self._create_mock_config(icon_path="assets/custom_icon.ico")

        found = find_disk_icon(self.disk_root, config)
        self.assertIsNotNone(found)
        self.assertEqual(os.path.abspath(found), os.path.abspath(icon_file))

    def test_find_icon_from_config_missing_fallback(self) -> None:
        """CFG-20: Missing config icon falls back to root autorun.ico."""
        autorun = self._touch("autorun.ico")
        config = self._create_mock_config(icon_path="nonexistent_icon.ico")

        found = find_disk_icon(self.disk_root, config)
        self.assertIsNotNone(found)
        self.assertEqual(os.path.abspath(found), os.path.abspath(autorun))

    def test_find_icon_fallback_autorun_ico(self) -> None:
        """CFG-21: No icon in config, finds root autorun.ico."""
        autorun = self._touch("autorun.ico")
        config = self._create_mock_config(icon_path=None)

        found = find_disk_icon(self.disk_root, config)
        self.assertIsNotNone(found)
        self.assertEqual(os.path.abspath(found), os.path.abspath(autorun))

    def test_find_icon_fallback_game_ico_or_icon_ico(self) -> None:
        """CFG-22: No autorun.ico, finds icon.ico or game.ico."""
        icon_ico = self._touch("icon.ico")
        config = self._create_mock_config(icon_path=None)

        found = find_disk_icon(self.disk_root, config)
        self.assertIsNotNone(found)
        self.assertEqual(os.path.abspath(found), os.path.abspath(icon_ico))

    def test_find_icon_fallback_png(self) -> None:
        """CFG-23: No .ico files, finds icon.png or game.png."""
        icon_png = self._touch("icon.png")
        config = self._create_mock_config(icon_path=None)

        found = find_disk_icon(self.disk_root, config)
        self.assertIsNotNone(found)
        self.assertEqual(os.path.abspath(found), os.path.abspath(icon_png))

    def test_find_icon_case_insensitivity(self) -> None:
        """CFG-24: Case insensitivity (e.g. AUTORUN.ICO, Game.Ico)."""
        upper_ico = self._touch("AUTORUN.ICO")
        found = find_disk_icon(self.disk_root, None)
        self.assertIsNotNone(found)
        self.assertEqual(os.path.abspath(found), os.path.abspath(upper_ico))

    def test_find_icon_none_found(self) -> None:
        """CFG-25: No icon files on disk returns None."""
        config = self._create_mock_config(icon_path="missing.ico")
        found = find_disk_icon(self.disk_root, config)
        self.assertIsNone(found)

    def test_find_icon_with_none_config(self) -> None:
        """CFG-26: Calling with config=None discovers root icon."""
        game_ico = self._touch("game.ico")
        found = find_disk_icon(self.disk_root, None)
        self.assertIsNotNone(found)
        self.assertEqual(os.path.abspath(found), os.path.abspath(game_ico))

    def test_find_icon_setup_adjacent(self) -> None:
        """Setup-adjacent icon discovered when root has no icon."""
        setup_icon = self._touch("installer/icon.ico")
        config = self._create_mock_config(icon_path=None, setup_exe="installer/setup.exe")

        found = find_disk_icon(self.disk_root, config)
        self.assertIsNotNone(found)
    def test_find_icon_nested_case_insensitivity(self) -> None:
        """CFG-27: Nested path case insensitivity (e.g. Assets/SubDir/Game.Ico)."""
        nested_ico = self._touch("Assets/SubDir/Game.Ico")
        config = self._create_mock_config(icon_path="assets/subdir/game.ico")

        found = find_disk_icon(self.disk_root, config)
        self.assertIsNotNone(found)
        self.assertEqual(os.path.abspath(found), os.path.abspath(nested_ico))


if __name__ == "__main__":
    unittest.main()
