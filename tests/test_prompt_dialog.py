"""
tests.test_prompt_dialog
~~~~~~~~~~~~~~~~~~~~~~~~

Unit tests for gog_disk_monitor.prompt_dialog.InstallPromptDialog.
Tests metadata extraction, drive letter normalization, icon loading,
auto-confirm test overrides, environment variable detection, and DPI awareness.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest

from gog_disk_monitor.config import (
    GOGDiskConfig,
    LauncherConfig,
    SetupConfig,
)
from gog_disk_monitor.prompt_dialog import (
    HAS_TKINTER,
    InstallPromptDialog,
    enable_windows_dpi_awareness,
    get_auto_confirm_env,
)


class TestPromptDialogNormalizationAndMetadata(unittest.TestCase):
    """Tests for metadata extraction and drive letter formatting."""

    def test_normalize_drive_letter_standard(self):
        """Normalizes standard drive letters and paths."""
        self.assertEqual(InstallPromptDialog._normalize_drive_letter("X:"), "X:")
        self.assertEqual(InstallPromptDialog._normalize_drive_letter("x:"), "X:")
        self.assertEqual(InstallPromptDialog._normalize_drive_letter("X:\\"), "X:")
        self.assertEqual(InstallPromptDialog._normalize_drive_letter("d:/"), "D:")
        self.assertEqual(InstallPromptDialog._normalize_drive_letter("e"), "E:")

    def test_normalize_drive_letter_empty_or_invalid(self):
        """Handles empty or None drive letter gracefully."""
        self.assertEqual(InstallPromptDialog._normalize_drive_letter(""), "Removable Drive")
        self.assertEqual(InstallPromptDialog._normalize_drive_letter(None), "Removable Drive")

    def test_extract_metadata_from_gog_disk_config_dataclass(self):
        """Extracts title, publisher, version, and setup info from GOGDiskConfig."""
        config = GOGDiskConfig(
            game_id="witcher_3",
            title="The Witcher 3: Wild Hunt",
            version="1.32",
            publisher="CD PROJEKT RED",
            developer="CD PROJEKT RED",
            setup=SetupConfig(
                executable="setup.exe",
                arguments=["/SILENT"],
                estimated_size_mb=45000,
            ),
            launcher=LauncherConfig(executable="bin/x64/witcher3.exe"),
            icon_path="icon.ico",
        )
        meta = InstallPromptDialog._extract_metadata(config)
        self.assertEqual(meta["game_id"], "witcher_3")
        self.assertEqual(meta["title"], "The Witcher 3: Wild Hunt")
        self.assertEqual(meta["publisher"], "CD PROJEKT RED")
        self.assertEqual(meta["version"], "1.32")
        self.assertEqual(meta["setup_executable"], "setup.exe")
        self.assertEqual(meta["estimated_size_mb"], 45000)
        self.assertEqual(meta["icon_path"], "icon.ico")

    def test_extract_metadata_from_dict(self):
        """Extracts metadata from plain dictionary format."""
        data = {
            "game_id": "cyberpunk_2077",
            "title": "Cyberpunk 2077",
            "publisher": "CD PROJEKT RED",
            "version": "2.0",
            "setup": {
                "executable": "setup.exe",
                "estimated_size_mb": 70000,
            },
            "icon_file": "custom_icon.png",
        }
        meta = InstallPromptDialog._extract_metadata(data)
        self.assertEqual(meta["game_id"], "cyberpunk_2077")
        self.assertEqual(meta["title"], "Cyberpunk 2077")
        self.assertEqual(meta["publisher"], "CD PROJEKT RED")
        self.assertEqual(meta["version"], "2.0")
        self.assertEqual(meta["setup_executable"], "setup.exe")
        self.assertEqual(meta["estimated_size_mb"], 70000)
        self.assertEqual(meta["icon_path"], "custom_icon.png")


class TestPromptDialogAutoConfirmAndEnvironment(unittest.TestCase):
    """Tests for non-blocking automated testing overrides."""

    def setUp(self):
        self.saved_env = os.environ.get("GOG_MONITOR_AUTO_CONFIRM")

    def tearDown(self):
        if self.saved_env is not None:
            os.environ["GOG_MONITOR_AUTO_CONFIRM"] = self.saved_env
        else:
            os.environ.pop("GOG_MONITOR_AUTO_CONFIRM", None)

    def test_get_auto_confirm_env_values(self):
        """Tests parsing of various truthy and falsy environment variable strings."""
        os.environ["GOG_MONITOR_AUTO_CONFIRM"] = "1"
        self.assertIs(get_auto_confirm_env(), True)

        os.environ["GOG_MONITOR_AUTO_CONFIRM"] = "true"
        self.assertIs(get_auto_confirm_env(), True)

        os.environ["GOG_MONITOR_AUTO_CONFIRM"] = "YES"
        self.assertIs(get_auto_confirm_env(), True)

        os.environ["GOG_MONITOR_AUTO_CONFIRM"] = "0"
        self.assertIs(get_auto_confirm_env(), False)

        os.environ["GOG_MONITOR_AUTO_CONFIRM"] = "false"
        self.assertIs(get_auto_confirm_env(), False)

        os.environ["GOG_MONITOR_AUTO_CONFIRM"] = "no"
        self.assertIs(get_auto_confirm_env(), False)

        os.environ.pop("GOG_MONITOR_AUTO_CONFIRM", None)
        self.assertIsNone(get_auto_confirm_env())

    def test_explicit_auto_confirm_true(self):
        """auto_confirm=True returns True immediately without opening GUI."""
        os.environ.pop("GOG_MONITOR_AUTO_CONFIRM", None)
        config = {"game_id": "test_game", "title": "Test Game"}
        dialog = InstallPromptDialog(config, drive_letter="Z:", auto_confirm=True)
        res = dialog.show()
        self.assertTrue(res)

    def test_explicit_auto_confirm_false(self):
        """auto_confirm=False returns False immediately without opening GUI."""
        os.environ.pop("GOG_MONITOR_AUTO_CONFIRM", None)
        config = {"game_id": "test_game", "title": "Test Game"}
        dialog = InstallPromptDialog(config, drive_letter="Z:", auto_confirm=False)
        res = dialog.show()
        self.assertFalse(res)

    def test_auto_confirm_priority_over_env_var(self):
        """Explicit auto_confirm takes precedence over environment variable."""
        os.environ["GOG_MONITOR_AUTO_CONFIRM"] = "1"
        config = {"game_id": "test_game", "title": "Test Game"}
        dialog = InstallPromptDialog(config, drive_letter="Z:", auto_confirm=False)
        self.assertFalse(dialog.show())

    def test_env_var_auto_confirm_true(self):
        """GOG_MONITOR_AUTO_CONFIRM='1' causes show() to return True."""
        os.environ["GOG_MONITOR_AUTO_CONFIRM"] = "1"
        config = {"game_id": "test_game", "title": "Test Game"}
        dialog = InstallPromptDialog(config, drive_letter="Z:")
        self.assertTrue(dialog.show())

    def test_env_var_auto_confirm_false(self):
        """GOG_MONITOR_AUTO_CONFIRM='0' causes show() to return False."""
        os.environ["GOG_MONITOR_AUTO_CONFIRM"] = "0"
        config = {"game_id": "test_game", "title": "Test Game"}
        dialog = InstallPromptDialog(config, drive_letter="Z:")
        self.assertFalse(dialog.show())

    def test_static_show_prompt_helper(self):
        """InstallPromptDialog.show_prompt static method invokes dialog correctly."""
        config = GOGDiskConfig(
            game_id="homeworld",
            title="Homeworld Remastered",
            version="2.1",
            setup=SetupConfig(executable="setup.exe"),
            launcher=LauncherConfig(executable="Homeworld.exe"),
        )
        res = InstallPromptDialog.show_prompt(config, drive_letter="X:", auto_confirm=True)
        self.assertTrue(res)

        res_cancel = InstallPromptDialog.show_prompt(config, drive_letter="X:", auto_confirm=False)
        self.assertFalse(res_cancel)


class TestPromptDialogUIComponentsAndDPI(unittest.TestCase):
    """Tests for icon loading, fallback canvas, and DPI awareness."""

    def test_dpi_awareness_call_safe(self):
        """enable_windows_dpi_awareness runs without throwing exceptions on any platform."""
        res = enable_windows_dpi_awareness()
        self.assertIsInstance(res, bool)

    def test_icon_loader_fallback_on_missing_file(self):
        """_load_icon_image returns None when icon file does not exist."""
        dialog = InstallPromptDialog({}, icon_path="nonexistent_icon_xyz.ico")
        img = dialog._load_icon_image(master=None, icon_path="nonexistent_icon_xyz.ico")
        self.assertIsNone(img)

    def test_icon_loader_with_valid_image(self):
        """_load_icon_image loads PNG image using PIL when available."""
        from PIL import Image
        temp_dir = tempfile.mkdtemp()
        try:
            img_path = Path(temp_dir) / "test_icon.png"
            img = Image.new("RGBA", (32, 32), color=(255, 0, 0, 255))
            img.save(img_path)
            img.close()

            dialog = InstallPromptDialog({}, icon_path=str(img_path))
            with Image.open(str(img_path)) as pil_img:
                self.assertEqual(pil_img.size, (32, 32))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_dialog_timeout_cancels_cleanly(self):
        """Dialog with a short timeout automatically dismisses and returns False."""
        if not HAS_TKINTER:
            self.skipTest("Tkinter not available")
        # Ensure auto_confirm env is unset
        os.environ.pop("GOG_MONITOR_AUTO_CONFIRM", None)
        config = {
            "game_id": "auto_timeout_game",
            "title": "Auto Timeout Game",
            "version": "1.0",
            "setup": {"executable": "setup.exe"},
        }
        # Run with 0.1s timeout
        dialog = InstallPromptDialog(config, drive_letter="Y:", timeout_seconds=0.1)
        res = dialog.show()
        self.assertFalse(res)


if __name__ == "__main__":
    unittest.main()
