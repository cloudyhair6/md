"""
tests.test_gui_setup
~~~~~~~~~~~~~~~~~~~~

Comprehensive test suite for GUI_setup.py deployment logic and PyQt/PySide GUI:
- Slugification and argument parsing
- Configuration dictionary generation and schema compliance
- Programmatic end-to-end disk deployment (executable, icon, gog_game.json)
- Schema validation with existing gog_disk_monitor.config parser and icon resolver
- Error handling, boundary conditions, and Unicode preservation
- Headless Qt GUI interaction, live preview, form validation, and deployment
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any, Dict, Optional
import unittest

from PIL import Image

# Import deployment logic and UI components from GUI_setup
import GUI_setup
from GUI_setup import (
    QT_BINDING,
    DeploymentResult,
    DiskSetupWindow,
    build_gog_config_dict,
    deploy_game_disk,
    get_available_drives,
    parse_arguments_list,
    slugify_game_id,
)
from gog_disk_monitor.config import (
    GOGDiskConfig,
    find_disk_icon,
    parse_disk_config,
)

# Qt imports for GUI testing
if QT_BINDING is not None:
    if QT_BINDING == "PyQt6":
        from PyQt6.QtWidgets import QApplication
    elif QT_BINDING == "PySide6":
        from PySide6.QtWidgets import QApplication
    elif QT_BINDING == "PyQt5":
        from PyQt5.QtWidgets import QApplication


def _create_mock_binary_file(filepath: Path, content: bytes = b"MZ\x90\x00MOCK_EXE") -> Path:
    """Create a mock binary/executable file on disk."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "wb") as f:
        f.write(content)
    return filepath


def _create_mock_icon_file(filepath: Path, fmt: str = "ICO", size: tuple = (32, 32)) -> Path:
    """Create a valid mock .ico or .png file on disk."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", size, color=(30, 144, 255, 255))
    if fmt.upper() == "ICO":
        img.save(filepath, format="ICO", sizes=[size])
    else:
        img.save(filepath, format="PNG")
    return filepath


class TestDeploymentUtilities(unittest.TestCase):
    """Test suite for helper utilities and schema generation."""

    def test_slugify_game_id(self) -> None:
        """Test snake_case slug generation across diverse titles."""
        self.assertEqual(slugify_game_id("The Witcher 3: Wild Hunt"), "the_witcher_3_wild_hunt")
        self.assertEqual(slugify_game_id("Cyberpunk 2077"), "cyberpunk_2077")
        self.assertEqual(slugify_game_id("Heroes of Might and Magic III HD"), "heroes_of_might_and_magic_iii_hd")
        self.assertEqual(slugify_game_id("Game---With--Dashes!"), "game_with_dashes")
        self.assertEqual(slugify_game_id("Game - Part 1 - "), "game_part_1")
        self.assertEqual(slugify_game_id("---"), "game")
        self.assertEqual(slugify_game_id("!@#$%^&*()"), "game")
        self.assertEqual(slugify_game_id(""), "game")
        self.assertEqual(slugify_game_id(None), "game")
        self.assertEqual(slugify_game_id("   "), "game")

    def test_parse_arguments_list(self) -> None:
        """Test argument string and list parsing with edge cases."""
        self.assertEqual(parse_arguments_list(None), [])
        self.assertEqual(parse_arguments_list(""), [])
        self.assertEqual(parse_arguments_list("   "), [])
        self.assertEqual(parse_arguments_list("/SILENT /VERYSILENT"), ["/SILENT", "/VERYSILENT"])
        self.assertEqual(
            parse_arguments_list('/DIR="C:\\GOG Games\\Witcher" /NOICONS'),
            ['/DIR="C:\\GOG Games\\Witcher"', "/NOICONS"],
        )
        self.assertEqual(parse_arguments_list(["-dx12", "--fullscreen"]), ["-dx12", "--fullscreen"])
        self.assertEqual(parse_arguments_list(("/OPT1", "/OPT2")), ["/OPT1", "/OPT2"])
        self.assertEqual(parse_arguments_list(123), ["123"])

    def test_sanitize_dest_relpath(self) -> None:
        """Test destination path sanitization preventing path traversal and escapes."""
        from GUI_setup import _sanitize_dest_relpath
        self.assertEqual(_sanitize_dest_relpath("setup.exe", "setup.exe"), "setup.exe")
        self.assertEqual(_sanitize_dest_relpath("/setup.exe", "setup.exe"), "setup.exe")
        self.assertEqual(_sanitize_dest_relpath("\\setup.exe", "setup.exe"), "setup.exe")
        self.assertEqual(_sanitize_dest_relpath("installer/setup.exe", "setup.exe"), "installer/setup.exe")
        self.assertEqual(_sanitize_dest_relpath("C:/evil/setup.exe", "setup.exe"), "evil/setup.exe")
        self.assertEqual(_sanitize_dest_relpath("../../escape.exe", "setup.exe"), "setup.exe")
        self.assertEqual(_sanitize_dest_relpath("", "default.exe"), "default.exe")
        self.assertEqual(_sanitize_dest_relpath(None, "default.exe"), "default.exe")

    def test_build_gog_config_dict_full(self) -> None:
        """Test building a complete GOG disk configuration dictionary."""
        config_dict = build_gog_config_dict(
            game_id="witcher_3",
            title="The Witcher 3: Wild Hunt",
            version="4.04",
            setup_executable="installer/setup.exe",
            setup_arguments=["/SILENT", "/DIR=C:\\Games"],
            default_install_subdir="Witcher 3",
            estimated_size_mb=52000,
            silent_supported=True,
            launcher_executable="bin/x64/witcher3.exe",
            launcher_arguments=["-dx12"],
            working_directory="bin/x64",
            requires_admin=True,
            icon_path="assets/game.ico",
            publisher="CD PROJEKT",
            developer="CD PROJEKT RED",
            disk_number=1,
            total_disks=2,
            disk_label="W3_DISC1",
        )

        self.assertEqual(config_dict["schema_version"], "1.0")
        self.assertEqual(config_dict["game_id"], "witcher_3")
        self.assertEqual(config_dict["title"], "The Witcher 3: Wild Hunt")
        self.assertEqual(config_dict["version"], "4.04")
        self.assertEqual(config_dict["publisher"], "CD PROJEKT")
        self.assertEqual(config_dict["developer"], "CD PROJEKT RED")
        self.assertEqual(config_dict["icon_path"], "assets/game.ico")

        # Setup assertions
        self.assertEqual(config_dict["setup"]["executable"], "installer/setup.exe")
        self.assertEqual(config_dict["setup"]["arguments"], ["/SILENT", "/DIR=C:\\Games"])
        self.assertEqual(config_dict["setup"]["default_install_subdir"], "Witcher 3")
        self.assertEqual(config_dict["setup"]["estimated_size_mb"], 52000)
        self.assertTrue(config_dict["setup"]["silent_supported"])

        # Launcher assertions
        self.assertEqual(config_dict["launcher"]["executable"], "bin/x64/witcher3.exe")
        self.assertEqual(config_dict["launcher"]["arguments"], ["-dx12"])
        self.assertEqual(config_dict["launcher"]["working_directory"], "bin/x64")
        self.assertTrue(config_dict["launcher"]["requires_admin"])

        # Disk info
        self.assertEqual(config_dict["disk_info"]["disk_number"], 1)
        self.assertEqual(config_dict["disk_info"]["total_disks"], 2)
        self.assertEqual(config_dict["disk_info"]["label"], "W3_DISC1")

    def test_build_gog_config_dict_minimal(self) -> None:
        """Test building minimal configuration with default fallbacks."""
        config_dict = build_gog_config_dict(
            game_id="",
            title="Minimal Game",
            setup_executable="setup.exe",
        )
        self.assertEqual(config_dict["game_id"], "minimal_game")
        self.assertEqual(config_dict["title"], "Minimal Game")
        self.assertEqual(config_dict["version"], "1.0.0")
        self.assertEqual(config_dict["setup"]["executable"], "setup.exe")
        self.assertEqual(config_dict["launcher"]["executable"], "setup.exe")
        self.assertNotIn("icon_path", config_dict)
        self.assertNotIn("publisher", config_dict)

    def test_slugify_consecutive_underscores(self) -> None:
        """Test collapsing multiple consecutive underscores and hyphens in slugify."""
        self.assertEqual(slugify_game_id("Game _-_ Part 1"), "game_part_1")
        self.assertEqual(slugify_game_id("Cyberpunk___2077"), "cyberpunk_2077")
        self.assertEqual(slugify_game_id("___Multi___Underscore___"), "multi_underscore")

    def test_parse_arguments_list_boolean_and_edge_inputs(self) -> None:
        """Test argument parsing ignores booleans and cleans whitespace-only items."""
        self.assertEqual(parse_arguments_list(True), [])
        self.assertEqual(parse_arguments_list(False), [])
        self.assertEqual(parse_arguments_list(["", "   ", "/VALID"]), ["/VALID"])

    def test_build_gog_config_dict_whitespace_and_empty_inputs(self) -> None:
        """Verify build_gog_config_dict handles whitespace-only strings safely."""
        config = build_gog_config_dict(
            game_id="   ",
            title="The Witcher",
            version="   ",
            setup_executable="setup.exe",
            working_directory="   ",
            icon_path="   ",
            publisher="   ",
            developer="   ",
            disk_label="   ",
            default_install_subdir="   ",
        )
        self.assertEqual(config["game_id"], "the_witcher")
        self.assertEqual(config["version"], "1.0.0")
        self.assertNotIn("icon_path", config)
        self.assertNotIn("publisher", config)
        self.assertNotIn("developer", config)
        self.assertNotIn("working_directory", config["launcher"])
        self.assertNotIn("default_install_subdir", config["setup"])
        self.assertEqual(config["disk_info"]["label"], "THE_WITCHER_DISC1")

    def test_get_available_drives(self) -> None:
        """Test drive enumeration helper returns list."""
        drives = get_available_drives()
        self.assertIsInstance(drives, list)
        if sys.platform == "win32" and os.path.exists("C:\\"):
            self.assertIn("C:\\", drives)

    def test_build_gog_config_dict_negative_values(self) -> None:
        """Verify negative values for disk counts and sizes are safely normalized."""
        config = build_gog_config_dict(
            title="Boundary Game",
            estimated_size_mb=-500,
            disk_number=-3,
            total_disks=0,
        )
        self.assertNotIn("estimated_size_mb", config["setup"])
        self.assertEqual(config["disk_info"]["disk_number"], 1)
        self.assertEqual(config["disk_info"]["total_disks"], 1)


class TestDeploymentEngine(unittest.TestCase):
    """End-to-end file deployment and schema verification test suite."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.src_dir = self.base_path / "source"
        self.src_dir.mkdir(parents=True, exist_ok=True)
        self.target_dir = self.base_path / "target_disk"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_deploy_mock_game_executable_icon_and_target_folder(self) -> None:
        """
        Acceptance Criteria: Verify that providing mock game executable,
        mock icon, and target folder results in all 3 files being correctly copied
        and validated by config.py parser.
        """
        mock_exe = _create_mock_binary_file(self.src_dir / "setup.exe")
        mock_ico = _create_mock_icon_file(self.src_dir / "game.ico", fmt="ICO")

        result = deploy_game_disk(
            source_executable=str(mock_exe),
            source_icon=str(mock_ico),
            target_dir=str(self.target_dir),
            title="Cyberpunk 2077",
            game_id="cyberpunk_2077",
            version="2.12",
            publisher="CD PROJEKT RED",
            developer="CD PROJEKT RED",
            setup_arguments=["/SILENT"],
            default_install_subdir="Cyberpunk 2077",
            estimated_size_mb=70000,
            silent_supported=True,
            launcher_executable="bin/x64/Cyberpunk2077.exe",
            launcher_arguments=["--fullscreen"],
            working_directory="bin/x64",
            requires_admin=False,
            disk_label="CYBERPUNK_DISC1",
        )

        self.assertTrue(result.success, f"Deployment failed: {result.message} {result.errors}")
        self.assertEqual(len(result.errors), 0)

        # 1. Verify all 3 files exist in the target folder
        expected_config_file = self.target_dir / "gog_game.json"
        expected_exe_file = self.target_dir / "setup.exe"
        expected_icon_file = self.target_dir / "game.ico"

        self.assertTrue(expected_config_file.is_file(), "gog_game.json must exist in target folder")
        self.assertTrue(expected_exe_file.is_file(), "Executable must exist in target folder")
        self.assertTrue(expected_icon_file.is_file(), "Icon file must exist in target folder")

        # 2. Verify file content fidelity
        self.assertEqual(expected_exe_file.read_bytes(), mock_exe.read_bytes())
        self.assertEqual(expected_icon_file.read_bytes(), mock_ico.read_bytes())

        # 3. Verify that config.py parser can successfully parse the deployed target folder
        parsed_config = parse_disk_config(str(self.target_dir))
        self.assertIsNotNone(parsed_config, "config.py parser failed to parse deployed target folder")
        self.assertEqual(parsed_config.game_id, "cyberpunk_2077")
        self.assertEqual(parsed_config.title, "Cyberpunk 2077")
        self.assertEqual(parsed_config.version, "2.12")
        self.assertEqual(parsed_config.publisher, "CD PROJEKT RED")
        self.assertEqual(parsed_config.developer, "CD PROJEKT RED")
        self.assertEqual(parsed_config.setup.executable, "setup.exe")
        self.assertEqual(parsed_config.setup.arguments, ["/SILENT"])
        self.assertEqual(parsed_config.setup.default_install_subdir, "Cyberpunk 2077")
        self.assertEqual(parsed_config.setup.estimated_size_mb, 70000)
        self.assertTrue(parsed_config.setup.silent_supported)
        self.assertEqual(parsed_config.launcher.executable, os.path.normpath("bin/x64/Cyberpunk2077.exe"))
        self.assertEqual(parsed_config.launcher.arguments, ["--fullscreen"])
        self.assertEqual(parsed_config.launcher.working_directory, os.path.normpath("bin/x64"))
        self.assertFalse(parsed_config.launcher.requires_admin)
        self.assertEqual(parsed_config.icon_path, "game.ico")
        self.assertEqual(parsed_config.disk_info["label"], "CYBERPUNK_DISC1")

        # 4. Verify icon resolution
        resolved_icon = find_disk_icon(str(self.target_dir), parsed_config)
        self.assertIsNotNone(resolved_icon)
        self.assertEqual(os.path.abspath(resolved_icon), os.path.abspath(str(expected_icon_file)))

    def test_deploy_with_png_icon(self) -> None:
        """Verify deployment with a custom .png icon."""
        mock_exe = _create_mock_binary_file(self.src_dir / "installer.exe")
        mock_png = _create_mock_icon_file(self.src_dir / "custom_icon.png", fmt="PNG")

        result = deploy_game_disk(
            source_executable=str(mock_exe),
            source_icon=str(mock_png),
            target_dir=str(self.target_dir),
            title="Pixel Art Adventure",
        )

        self.assertTrue(result.success)
        self.assertTrue((self.target_dir / "gog_game.json").is_file())
        self.assertTrue((self.target_dir / "installer.exe").is_file())
        self.assertTrue((self.target_dir / "custom_icon.png").is_file())

        parsed = parse_disk_config(str(self.target_dir))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.title, "Pixel Art Adventure")
        self.assertEqual(parsed.icon_path, "custom_icon.png")

    def test_deploy_minimal_without_icon(self) -> None:
        """Verify deployment when no custom icon is specified."""
        mock_exe = _create_mock_binary_file(self.src_dir / "game_setup.exe")

        result = deploy_game_disk(
            source_executable=str(mock_exe),
            source_icon=None,
            target_dir=str(self.target_dir),
            title="Simple Game",
        )

        self.assertTrue(result.success)
        self.assertTrue((self.target_dir / "gog_game.json").is_file())
        self.assertTrue((self.target_dir / "game_setup.exe").is_file())

        parsed = parse_disk_config(str(self.target_dir))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.game_id, "simple_game")
        self.assertIsNone(parsed.icon_path)

    def test_deploy_custom_dest_filenames_and_nested_paths(self) -> None:
        """Verify deploying with custom target subpaths (e.g. installer/setup.exe)."""
        mock_exe = _create_mock_binary_file(self.src_dir / "my_temp_build.exe")
        mock_ico = _create_mock_icon_file(self.src_dir / "raw_icon.ico")

        result = deploy_game_disk(
            source_executable=str(mock_exe),
            source_icon=str(mock_ico),
            target_dir=str(self.target_dir),
            title="Nested Structure Game",
            dest_setup_name="installer/setup.exe",
            dest_icon_name="assets/icons/game.ico",
        )

        self.assertTrue(result.success)
        self.assertTrue((self.target_dir / "installer" / "setup.exe").is_file())
        self.assertTrue((self.target_dir / "assets" / "icons" / "game.ico").is_file())

        parsed = parse_disk_config(str(self.target_dir))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.setup.executable, os.path.normpath("installer/setup.exe"))
        self.assertEqual(parsed.icon_path, os.path.normpath("assets/icons/game.ico"))

    def test_deploy_unicode_title_and_metadata(self) -> None:
        """Verify Unicode preservation in deployment and descriptor."""
        mock_exe = _create_mock_binary_file(self.src_dir / "instalator.exe")

        result = deploy_game_disk(
            source_executable=str(mock_exe),
            target_dir=str(self.target_dir),
            title="Wiedźmin 3: Dziki Gon™ — 極限",
            publisher="CD PROJEKT RED — Варшава",
            developer="CD PROJEKT — 日本",
        )

        self.assertTrue(result.success)
        parsed = parse_disk_config(str(self.target_dir))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.title, "Wiedźmin 3: Dziki Gon™ — 極限")
        self.assertEqual(parsed.publisher, "CD PROJEKT RED — Варшава")
        self.assertEqual(parsed.developer, "CD PROJEKT — 日本")

    def test_deploy_missing_source_executable_fails(self) -> None:
        """Verify deployment fails gracefully when executable path does not exist."""
        nonexistent = self.src_dir / "does_not_exist.exe"
        result = deploy_game_disk(
            source_executable=str(nonexistent),
            target_dir=str(self.target_dir),
            title="Test Game",
        )

        self.assertFalse(result.success)
        self.assertIn("Source file verification failed", result.message)
        self.assertTrue(any("does not exist" in e for e in result.errors))

    def test_deploy_missing_source_icon_fails(self) -> None:
        """Verify deployment fails gracefully when icon path is specified but missing."""
        mock_exe = _create_mock_binary_file(self.src_dir / "setup.exe")
        nonexistent_icon = self.src_dir / "missing.ico"

        result = deploy_game_disk(
            source_executable=str(mock_exe),
            source_icon=str(nonexistent_icon),
            target_dir=str(self.target_dir),
            title="Test Game",
        )

        self.assertFalse(result.success)
        self.assertTrue(any("Source icon file does not exist" in e for e in result.errors))

    def test_deploy_missing_title_or_target_fails(self) -> None:
        """Verify deployment fails when mandatory arguments are empty."""
        mock_exe = _create_mock_binary_file(self.src_dir / "setup.exe")

        # Empty title
        res1 = deploy_game_disk(
            source_executable=str(mock_exe),
            target_dir=str(self.target_dir),
            title="",
        )
        self.assertFalse(res1.success)
        self.assertTrue(any("title cannot be empty" in e for e in res1.errors))

        # Empty target
        res2 = deploy_game_disk(
            source_executable=str(mock_exe),
            target_dir="",
            title="Test Game",
        )
        self.assertFalse(res2.success)
        self.assertTrue(any("Target directory must be specified" in e for e in res2.errors))

    def test_deploy_same_source_and_destination_file(self) -> None:
        """Verify deploy handles source and destination pointing to the same file."""
        self.target_dir.mkdir(parents=True, exist_ok=True)
        exe_in_target = _create_mock_binary_file(self.target_dir / "setup.exe")
        ico_in_target = _create_mock_icon_file(self.target_dir / "icon.ico")

        result = deploy_game_disk(
            source_executable=str(exe_in_target),
            source_icon=str(ico_in_target),
            target_dir=str(self.target_dir),
            title="In-Place Game",
        )

        self.assertTrue(result.success)
        parsed = parse_disk_config(str(self.target_dir))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.title, "In-Place Game")

    def test_deploy_overwrite_false_rejects_existing_files(self) -> None:
        """Verify deploy with overwrite=False fails cleanly when destination files exist."""
        self.target_dir.mkdir(parents=True, exist_ok=True)
        mock_exe = _create_mock_binary_file(self.src_dir / "setup.exe", content=b"EXE_V1")
        mock_ico = _create_mock_icon_file(self.src_dir / "icon.ico")

        # Pre-create target files
        _create_mock_binary_file(self.target_dir / "setup.exe", content=b"OLD_TARGET_EXE")

        result = deploy_game_disk(
            source_executable=str(mock_exe),
            source_icon=str(mock_ico),
            target_dir=str(self.target_dir),
            title="Existing Game",
            overwrite=False,
        )

        self.assertFalse(result.success)
        self.assertIn("overwrite is False", result.message)
        # Verify target file was not overwritten
        self.assertEqual((self.target_dir / "setup.exe").read_bytes(), b"OLD_TARGET_EXE")

    def test_deploy_overwrite_true_replaces_existing_files(self) -> None:
        """Verify deploy with overwrite=True successfully replaces existing files."""
        self.target_dir.mkdir(parents=True, exist_ok=True)
        mock_exe = _create_mock_binary_file(self.src_dir / "setup.exe", content=b"NEW_TARGET_EXE")
        mock_ico = _create_mock_icon_file(self.src_dir / "icon.ico")

        # Pre-create target file
        _create_mock_binary_file(self.target_dir / "setup.exe", content=b"OLD_TARGET_EXE")

        result = deploy_game_disk(
            source_executable=str(mock_exe),
            source_icon=str(mock_ico),
            target_dir=str(self.target_dir),
            title="Replaced Game",
            overwrite=True,
        )

        self.assertTrue(result.success)
        self.assertEqual((self.target_dir / "setup.exe").read_bytes(), b"NEW_TARGET_EXE")

    def test_deploy_sanitizes_leading_slash_destination_paths(self) -> None:
        """Verify leading slashes in dest_setup_name do not escape target directory root."""
        mock_exe = _create_mock_binary_file(self.src_dir / "setup.exe")
        mock_ico = _create_mock_icon_file(self.src_dir / "icon.ico")

        result = deploy_game_disk(
            source_executable=str(mock_exe),
            source_icon=str(mock_ico),
            target_dir=str(self.target_dir),
            title="Sanitized Paths Game",
            dest_setup_name="/nested/bin/installer.exe",
            dest_icon_name="/assets/media.ico",
        )

        self.assertTrue(result.success)
        self.assertTrue((self.target_dir / "nested" / "bin" / "installer.exe").is_file())
        self.assertTrue((self.target_dir / "assets" / "media.ico").is_file())

    def test_build_gog_config_dict_robust_coercions(self) -> None:
        """Verify build_gog_config_dict handles string and out-of-range numeric inputs safely."""
        config = build_gog_config_dict(
            game_id="",
            title="Coercion Test Game",
            estimated_size_mb="85000",
            disk_number="3",
            total_disks="2",  # total_disks less than disk_number should be adjusted to 3
        )
        self.assertEqual(config["setup"]["estimated_size_mb"], 85000)
        self.assertEqual(config["disk_info"]["disk_number"], 3)
        self.assertEqual(config["disk_info"]["total_disks"], 3)

    def test_deploy_gog_disk_json_config_filename(self) -> None:
        """Verify deploy_game_disk succeeds with alternative standard descriptor 'gog_disk.json'."""
        mock_exe = _create_mock_binary_file(self.src_dir / "setup.exe")
        result = deploy_game_disk(
            source_executable=str(mock_exe),
            target_dir=str(self.target_dir),
            title="Alternate Config Name Game",
            config_filename="gog_disk.json",
        )
        self.assertTrue(result.success)
        self.assertTrue((self.target_dir / "gog_disk.json").is_file())
        parsed = parse_disk_config(str(self.target_dir))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.title, "Alternate Config Name Game")

    def test_deploy_destination_collision_rejection(self) -> None:
        """Verify deploy rejects colliding destination names for distinct source components."""
        mock_exe = _create_mock_binary_file(self.src_dir / "setup.exe")
        mock_ico = _create_mock_icon_file(self.src_dir / "icon.ico")

        # Exe colliding with config
        res1 = deploy_game_disk(
            source_executable=str(mock_exe),
            target_dir=str(self.target_dir),
            title="Collision Game 1",
            dest_setup_name="gog_game.json",
        )
        self.assertFalse(res1.success)
        self.assertIn("collision", res1.message.lower())

        # Exe colliding with icon
        res2 = deploy_game_disk(
            source_executable=str(mock_exe),
            source_icon=str(mock_ico),
            target_dir=str(self.target_dir),
            title="Collision Game 2",
            dest_setup_name="common.bin",
            dest_icon_name="common.bin",
        )
        self.assertFalse(res2.success)
        self.assertIn("collision", res2.message.lower())

    def test_deploy_destination_is_existing_directory_fails(self) -> None:
        """Verify deploy fails cleanly when destination target path is an existing directory."""
        mock_exe = _create_mock_binary_file(self.src_dir / "setup.exe")
        self.target_dir.mkdir(parents=True, exist_ok=True)
        (self.target_dir / "setup.exe").mkdir(parents=True, exist_ok=True)

        result = deploy_game_disk(
            source_executable=str(mock_exe),
            target_dir=str(self.target_dir),
            title="Dir Conflict Game",
            dest_setup_name="setup.exe",
        )
        self.assertFalse(result.success)
        self.assertIn("directory", result.message.lower())

    def test_deploy_replaces_readonly_files_when_overwrite_true(self) -> None:
        """Verify deploy replaces existing files even if they have read-only attribute set."""
        mock_exe = _create_mock_binary_file(self.src_dir / "setup.exe", content=b"NEW_EXE")
        self.target_dir.mkdir(parents=True, exist_ok=True)
        old_exe = _create_mock_binary_file(self.target_dir / "setup.exe", content=b"OLD_EXE")
        try:
            os.chmod(old_exe, stat.S_IREAD)
        except Exception:
            pass

        result = deploy_game_disk(
            source_executable=str(mock_exe),
            target_dir=str(self.target_dir),
            title="Readonly Overwrite Game",
            overwrite=True,
        )
        self.assertTrue(result.success)
        self.assertEqual((self.target_dir / "setup.exe").read_bytes(), b"NEW_EXE")

    def test_deploy_with_deeply_nested_custom_config_filename(self) -> None:
        """Verify deploy safely normalizes nested config_filename to root descriptor."""
        mock_exe = _create_mock_binary_file(self.src_dir / "setup.exe")
        result = deploy_game_disk(
            source_executable=str(mock_exe),
            target_dir=str(self.target_dir),
            title="Nested Config Game",
            config_filename="metadata/config/gog_game.json",
        )
        self.assertTrue(result.success)
        self.assertTrue((self.target_dir / "gog_game.json").is_file())
        parsed = parse_disk_config(str(self.target_dir))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.title, "Nested Config Game")


class TestGUIComponents(unittest.TestCase):
    """Test suite for Qt GUI window and interactive form logic."""

    @classmethod
    def setUpClass(cls) -> None:
        """Ensure QApplication instance is initialized in headless/offscreen mode."""
        if QT_BINDING is not None:
            cls.app = QApplication.instance()
            if cls.app is None:
                cls.app = QApplication(["-platform", "offscreen"])
        else:
            cls.app = None

    def setUp(self) -> None:
        if QT_BINDING is None:
            self.skipTest("No supported Qt framework (PyQt6/PySide6/PyQt5) installed.")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.temp_dir.name)
        self.window = DiskSetupWindow()

    def tearDown(self) -> None:
        self.window.close()
        self.temp_dir.cleanup()

    def test_gui_window_initial_state(self) -> None:
        """Verify GUI window initializes with correct default values and controls."""
        self.assertEqual(self.window.version_input.text(), "1.0.0")
        self.assertEqual(self.window.disc_num_spin.value(), 1)
        self.assertEqual(self.window.total_discs_spin.value(), 1)
        self.assertIn("schema_version", self.window.json_preview.toPlainText())
        self.assertIn("setup", self.window.json_preview.toPlainText())

    def test_gui_title_auto_slugifies_id_and_subdir(self) -> None:
        """Verify typing in Title field auto-populates Game ID and install subfolder."""
        self.window.title_input.setText("The Witcher 3: Wild Hunt")
        self.assertEqual(self.window.game_id_input.text(), "the_witcher_3_wild_hunt")
        self.assertEqual(self.window.install_subdir_input.text(), "The Witcher 3: Wild Hunt")

        # Custom override preserves manual edits
        self.window.game_id_input.setText("custom_w3_id")
        self.window.title_input.setText("The Witcher 3 Complete Edition")
        self.assertEqual(self.window.game_id_input.text(), "custom_w3_id")

    def test_gui_form_validation(self) -> None:
        """Verify validate_form checks all required fields."""
        # Initial empty form is invalid
        valid, errors = self.window.validate_form()
        self.assertFalse(valid)
        self.assertTrue(any("Title is required" in e for e in errors))

        # Partial form
        self.window.title_input.setText("Test Game")
        valid, errors = self.window.validate_form()
        self.assertFalse(valid)
        self.assertTrue(any("Executable path is required" in e for e in errors))

    def test_gui_programmatic_deploy(self) -> None:
        """Verify deploying directly from the GUI window widgets."""
        mock_exe = _create_mock_binary_file(self.work_dir / "bin" / "game_installer.exe")
        mock_ico = _create_mock_icon_file(self.work_dir / "res" / "app.ico")
        target_path = self.work_dir / "deployed_drive"

        # Populate form
        self.window.title_input.setText("Baldur's Gate 3")
        self.window.publisher_input.setText("Larian Studios")
        self.window.developer_input.setText("Larian Studios")
        self.window.version_input.setText("4.1.1")
        self.window.exe_path_input.setText(str(mock_exe))
        self.window.icon_path_input.setText(str(mock_ico))
        self.window.target_dir_input.setText(str(target_path))
        self.window.setup_args_input.setText("/SILENT")
        self.window.size_spin.setValue(125000)
        self.window.silent_chk.setChecked(True)
        self.window.launcher_exe_input.setText("bin/bg3_dx11.exe")
        self.window.launcher_args_input.setText("--skip-launcher")

        # Deploy without modal dialog
        result = self.window.deploy(confirm_dialog=False)

        self.assertTrue(result.success, f"GUI deploy failed: {result.message} {result.errors}")
        self.assertTrue((target_path / "gog_game.json").is_file())
        self.assertTrue((target_path / "game_installer.exe").is_file())
        self.assertTrue((target_path / "app.ico").is_file())

        # Verify parsed config
        parsed = parse_disk_config(str(target_path))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.game_id, "baldurs_gate_3")
        self.assertEqual(parsed.title, "Baldur's Gate 3")
        self.assertEqual(parsed.version, "4.1.1")
        self.assertEqual(parsed.publisher, "Larian Studios")
        self.assertEqual(parsed.setup.executable, "game_installer.exe")
        self.assertEqual(parsed.setup.arguments, ["/SILENT"])
        self.assertEqual(parsed.setup.estimated_size_mb, 125000)
        self.assertTrue(parsed.setup.silent_supported)
        self.assertEqual(parsed.launcher.executable, os.path.normpath("bin/bg3_dx11.exe"))
        self.assertEqual(parsed.launcher.arguments, ["--skip-launcher"])

    def test_gui_reset_form(self) -> None:
        """Verify resetting form restores initial defaults."""
        self.window.title_input.setText("Dirty Form Title")
        self.window.size_spin.setValue(500)
        self.window.silent_chk.setChecked(True)

        self.window.reset_form()

        self.assertEqual(self.window.title_input.text(), "")
        self.assertEqual(self.window.size_spin.value(), 0)
        self.assertFalse(self.window.silent_chk.isChecked())
        self.assertEqual(self.window.version_input.text(), "1.0.0")

    def test_gui_form_validation_rejects_existing_file_as_target(self) -> None:
        """Verify validate_form detects when target directory points to an existing file."""
        mock_exe = _create_mock_binary_file(self.work_dir / "setup.exe")
        file_target = _create_mock_binary_file(self.work_dir / "file_not_dir.txt")

        self.window.title_input.setText("Valid Title")
        self.window.exe_path_input.setText(str(mock_exe))
        self.window.target_dir_input.setText(str(file_target))

        valid, errors = self.window.validate_form()
        self.assertFalse(valid)
        self.assertTrue(any("cannot be an existing file" in e.lower() for e in errors))

    def test_gui_update_json_preview_accepts_signal_arguments(self) -> None:
        """Verify update_json_preview handles extraneous signal arguments without TypeError."""
        # Calling directly with int or str arguments (like from stateChanged / textChanged)
        try:
            self.window.update_json_preview(2)
            self.window.update_json_preview("some_text")
        except TypeError as ex:
            self.fail(f"update_json_preview raised TypeError with signal args: {ex}")

    def test_gui_icon_preview_nonexistent_and_invalid_file(self) -> None:
        """Verify icon preview clears safely when file is missing or invalid."""
        self.window._on_icon_changed("non_existent_path.ico")
        self.assertEqual(self.window.icon_preview_lbl.pixmap().isNull() if self.window.icon_preview_lbl.pixmap() else True, True)

        # Invalid file content (not an image)
        invalid_txt = self.work_dir / "not_an_image.ico"
        invalid_txt.write_text("Hello World", encoding="utf-8")
        self.window._on_icon_changed(str(invalid_txt))
        self.assertEqual(self.window.icon_preview_lbl.text(), "?")

    def test_gui_deployment_completed_signal(self) -> None:
        """Verify deployment_completed signal fires with the DeploymentResult object."""
        mock_exe = _create_mock_binary_file(self.work_dir / "bin" / "game.exe")
        target_path = self.work_dir / "signal_drive"

        self.window.title_input.setText("Signal Test Game")
        self.window.exe_path_input.setText(str(mock_exe))
        self.window.target_dir_input.setText(str(target_path))

        received_results = []
        self.window.deployment_completed.connect(lambda res: received_results.append(res))

        result = self.window.deploy(confirm_dialog=False)
        self.assertTrue(result.success)
        self.assertEqual(len(received_results), 1)
        self.assertIsInstance(received_results[0], DeploymentResult)
        self.assertTrue(received_results[0].success)
        self.assertEqual(received_results[0].target_dir, str(target_path))

    def test_main_cli_help_and_version(self) -> None:
        """Verify main() handles --help and --version cleanly without launching GUI."""
        self.assertEqual(GUI_setup.main(["--help"]), 0)
        self.assertEqual(GUI_setup.main(["-h"]), 0)
        self.assertEqual(GUI_setup.main(["--version"]), 0)

    def test_main_cli_execution_offscreen(self) -> None:
        """Verify main() entry point executes offscreen and returns 0."""
        ret = GUI_setup.main(["--offscreen"])
        self.assertEqual(ret, 0)


if __name__ == "__main__":
    unittest.main()
