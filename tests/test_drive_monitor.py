"""
tests.test_drive_monitor
~~~~~~~~~~~~~~~~~~~~~~~~

Comprehensive test suite for Windows Drive Detection & Monitoring Engine.
Covers:
- DriveInfo dataclass attributes, normalization, aliases, and properties
- Bitmask decoding and drive letter conversion across all bit positions
- WindowsDriveDetector low-level queries, Unicode volume info, and subst detection
- Error mode suppression (SetErrorMode)
- DriveMonitor polling, callbacks, and single-event deduplication
- CD-ROM media insertion, ejection, and disc-swap tracking
- Unmount and remount lifecycle
- Simultaneous additions and removals within a single polling interval
- Callback exception safety and thread lifecycle (start, stop, scan_now)
- DriveSimulator available letter resolution and error handling
- Real Windows subst drive simulation and integration
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, call, patch

from gog_disk_monitor.drive_monitor import (
    DEFAULT_ERROR_MODE,
    DRIVE_CDROM,
    DRIVE_FIXED,
    DRIVE_NO_ROOT_DIR,
    DRIVE_RAMDISK,
    DRIVE_REMOTE,
    DRIVE_REMOVABLE,
    DRIVE_TYPE_NAMES,
    DRIVE_UNKNOWN,
    SEM_FAILCRITICALERRORS,
    SEM_NOOPENFILEERRORBOX,
    DriveInfo,
    DriveMonitor,
    DriveSimulator,
    WindowsDriveDetector,
    suppress_windows_error_dialogs,
)


class TestDriveInfo(unittest.TestCase):
    """Unit tests for the DriveInfo dataclass and its properties."""

    def test_drive_info_letter_normalization(self):
        """Letter normalization handles lowercase, trailing slashes, and colons."""
        info1 = DriveInfo(letter="x")
        self.assertEqual(info1.letter, "X:")
        self.assertEqual(info1.root_path, "X:\\")

        info2 = DriveInfo(letter="e:\\")
        self.assertEqual(info2.letter, "E:")
        self.assertEqual(info2.root_path, "E:\\")

        info3 = DriveInfo(letter="Z:")
        self.assertEqual(info3.letter, "Z:")
        self.assertEqual(info3.root_path, "Z:\\")

    def test_drive_info_empty_or_malformed_letter(self):
        """Empty or blank letter falls back to UNKNOWN: with proper root path."""
        info = DriveInfo(letter="")
        self.assertEqual(info.letter, "UNKNOWN:")
        self.assertEqual(info.root_path, "UNKNOWN:\\")

    def test_drive_info_subst_detection_from_dos_target_question_mark(self):
        """dos_target starting with \\??\\ sets is_subst and subst_target."""
        info = DriveInfo(
            letter="V:",
            dos_target=r"\??\C:\Users\User\Games\Disc1",
            raw_type=DRIVE_FIXED,
        )
        self.assertTrue(info.is_subst)
        self.assertEqual(info.subst_target, r"C:\Users\User\Games\Disc1")
        self.assertEqual(info.drive_type, "SUBST")
        self.assertTrue(info.is_candidate_for_game_disk)

    def test_drive_info_subst_detection_from_dos_target_dos_devices(self):
        """dos_target starting with \\DosDevices\\ sets is_subst and subst_target."""
        info = DriveInfo(
            letter="W:",
            dos_target=r"\DosDevices\D:\Mount\Disc2",
            raw_type=DRIVE_FIXED,
        )
        self.assertTrue(info.is_subst)
        self.assertEqual(info.subst_target, r"D:\Mount\Disc2")
        self.assertEqual(info.drive_type, "SUBST")
        self.assertTrue(info.is_candidate_for_game_disk)

    def test_drive_info_subst_target_direct(self):
        """Providing subst_target directly configures dos_target and is_subst."""
        info = DriveInfo(
            letter="T:",
            subst_target=r"C:\MockDisc",
        )
        self.assertTrue(info.is_subst)
        self.assertEqual(info.dos_target, r"\??\C:\MockDisc")
        self.assertEqual(info.drive_type, "SUBST")

    def test_drive_info_filesystem_alias(self):
        """filesystem property aliases file_system attribute correctly."""
        info = DriveInfo(letter="D:", file_system="NTFS")
        self.assertEqual(info.filesystem, "NTFS")
        info.filesystem = "CDFS"
        self.assertEqual(info.file_system, "CDFS")
        self.assertEqual(info.filesystem, "CDFS")

    def test_drive_info_candidate_classification(self):
        """is_candidate_for_game_disk correctly identifies scannable media."""
        removable = DriveInfo(letter="E:", drive_type="REMOVABLE", is_ready=True)
        self.assertTrue(removable.is_candidate_for_game_disk)

        cdrom = DriveInfo(letter="D:", drive_type="CDROM", is_ready=True)
        self.assertTrue(cdrom.is_candidate_for_game_disk)

        subst = DriveInfo(letter="X:", drive_type="SUBST", is_subst=True, is_ready=True)
        self.assertTrue(subst.is_candidate_for_game_disk)

        # Fixed internal OS drive is not candidate
        fixed_drive = DriveInfo(
            letter="C:",
            drive_type="FIXED",
            is_subst=False,
            dos_target=r"\Device\HarddiskVolume1",
            is_ready=True,
        )
        self.assertFalse(fixed_drive.is_candidate_for_game_disk)

        # Unready CD-ROM is not ready for immediate scanning
        empty_cdrom = DriveInfo(letter="D:", drive_type="CDROM", is_ready=False)
        self.assertFalse(empty_cdrom.is_candidate_for_game_disk)


class TestWindowsDriveDetector(unittest.TestCase):
    """Unit tests for low-level drive queries and bitmask conversion."""

    def test_get_drive_letters_from_mask_empty(self):
        """Bitmask 0 yields empty set."""
        self.assertEqual(WindowsDriveDetector.get_drive_letters_from_mask(0), set())

    def test_get_drive_letters_from_mask_single_and_multiple(self):
        """Bitmask correctly decodes standard drive letters."""
        # Bit 0 = A:
        self.assertEqual(WindowsDriveDetector.get_drive_letters_from_mask(1), {"A:"})
        # Bit 2 = C: (1 << 2 = 4)
        self.assertEqual(WindowsDriveDetector.get_drive_letters_from_mask(4), {"C:"})
        # Bit 2 (C:) and Bit 3 (D:) = 4 + 8 = 12
        self.assertEqual(WindowsDriveDetector.get_drive_letters_from_mask(12), {"C:", "D:"})
        # Bit 23 (X:) and Bit 25 (Z:)
        mask = (1 << 23) | (1 << 25)
        self.assertEqual(WindowsDriveDetector.get_drive_letters_from_mask(mask), {"X:", "Z:"})

    def test_get_drive_letters_from_mask_all_26(self):
        """All 26 bits set yields all letters from A: to Z:."""
        full_mask = (1 << 26) - 1
        letters = WindowsDriveDetector.get_drive_letters_from_mask(full_mask)
        self.assertEqual(len(letters), 26)
        for i in range(26):
            self.assertIn(f"{chr(ord('A') + i)}:", letters)

    def test_is_subst_drive_logic(self):
        """is_subst_drive identifies prefixes and extracts source paths."""
        mock_kernel32 = MagicMock()
        detector = WindowsDriveDetector(kernel32_dll=mock_kernel32)

        with patch.object(detector, "get_dos_device_target", return_value=r"\??\C:\MyGames\Disk1"):
            is_subst, target = detector.is_subst_drive("X:")
            self.assertTrue(is_subst)
            self.assertEqual(target, r"C:\MyGames\Disk1")

        with patch.object(detector, "get_dos_device_target", return_value=r"\DosDevices\D:\Disc2"):
            is_subst, target = detector.is_subst_drive("Y:")
            self.assertTrue(is_subst)
            self.assertEqual(target, r"D:\Disc2")

        with patch.object(detector, "get_dos_device_target", return_value=r"\Device\HarddiskVolume2"):
            is_subst, target = detector.is_subst_drive("C:")
            self.assertFalse(is_subst)
            self.assertIsNone(target)

        with patch.object(detector, "get_dos_device_target", return_value=""):
            is_subst, target = detector.is_subst_drive("Z:")
            self.assertFalse(is_subst)
            self.assertIsNone(target)

    def test_get_drive_type_info_mapping(self):
        """Drive type integers map to expected category strings."""
        mock_kernel32 = MagicMock()
        detector = WindowsDriveDetector(kernel32_dll=mock_kernel32)

        mock_kernel32.GetDriveTypeW.return_value = DRIVE_REMOVABLE
        raw, name = detector.get_drive_type_info("E:\\")
        self.assertEqual(raw, DRIVE_REMOVABLE)
        self.assertEqual(name, "REMOVABLE")

        mock_kernel32.GetDriveTypeW.return_value = DRIVE_CDROM
        raw, name = detector.get_drive_type_info("D:\\")
        self.assertEqual(raw, DRIVE_CDROM)
        self.assertEqual(name, "CDROM")

        mock_kernel32.GetDriveTypeW.return_value = DRIVE_FIXED
        raw, name = detector.get_drive_type_info("C:\\")
        self.assertEqual(raw, DRIVE_FIXED)
        self.assertEqual(name, "FIXED")

        mock_kernel32.GetDriveTypeW.return_value = DRIVE_REMOTE
        raw, name = detector.get_drive_type_info("Z:\\")
        self.assertEqual(raw, DRIVE_REMOTE)
        self.assertEqual(name, "NETWORK")

        mock_kernel32.GetDriveTypeW.return_value = DRIVE_RAMDISK
        raw, name = detector.get_drive_type_info("R:\\")
        self.assertEqual(raw, DRIVE_RAMDISK)
        self.assertEqual(name, "RAMDISK")

        mock_kernel32.GetDriveTypeW.return_value = 999
        raw, name = detector.get_drive_type_info("Q:\\")
        self.assertEqual(raw, 999)
        self.assertEqual(name, "UNKNOWN")

    def test_inspect_drive_unready_media(self):
        """inspect_drive handles unready/empty drives without raising errors."""
        mock_kernel32 = MagicMock()
        detector = WindowsDriveDetector(kernel32_dll=mock_kernel32)

        mock_kernel32.GetDriveTypeW.return_value = DRIVE_CDROM
        with patch.object(detector, "get_dos_device_target", return_value=r"\Device\CdRom0"):
            with patch.object(detector, "get_volume_information", return_value=None):
                info = detector.inspect_drive("D:", probe_retries=1)
                self.assertEqual(info.letter, "D:")
                self.assertEqual(info.drive_type, "CDROM")
                self.assertFalse(info.is_ready)
                self.assertEqual(info.volume_name, "")
                self.assertIsNone(info.serial_number)

    def test_inspect_drive_ready_media_unicode(self):
        """inspect_drive populates complete metadata including Unicode volume labels."""
        mock_kernel32 = MagicMock()
        detector = WindowsDriveDetector(kernel32_dll=mock_kernel32)

        mock_kernel32.GetDriveTypeW.return_value = DRIVE_REMOVABLE
        vol_data = {
            "volume_name": "巫师3_WILD_HUNT",
            "serial_number": 12345678,
            "file_system": "FAT32",
        }
        with patch.object(detector, "get_dos_device_target", return_value=r"\Device\Harddisk1\DP(1)0-0+6"):
            with patch.object(detector, "get_volume_information", return_value=vol_data):
                info = detector.inspect_drive("G:", probe_retries=1)
                self.assertEqual(info.letter, "G:")
                self.assertEqual(info.drive_type, "REMOVABLE")
                self.assertTrue(info.is_ready)
                self.assertEqual(info.volume_name, "巫师3_WILD_HUNT")
                self.assertEqual(info.serial_number, 12345678)
                self.assertEqual(info.file_system, "FAT32")

    def test_detector_not_available_fallback(self):
        """When kernel32 is None (e.g. non-Windows platform), detector functions return safe defaults."""
        detector = WindowsDriveDetector(kernel32_dll=None)
        detector._kernel32 = None
        self.assertFalse(detector.is_available)
        self.assertEqual(detector.get_logical_drives_mask(), 0)
        self.assertEqual(detector.get_dos_device_target("C:"), "")
        self.assertIsNone(detector.get_volume_information("C:\\"))
        raw, name = detector.get_drive_type_info("C:\\")
        self.assertEqual(raw, DRIVE_UNKNOWN)


class TestErrorSuppression(unittest.TestCase):
    """Test Windows SetErrorMode suppression against modal popups."""

    def test_suppress_windows_error_dialogs(self):
        """Calling suppress_windows_error_dialogs invokes SetErrorMode on Windows."""
        if sys.platform == "win32":
            res = suppress_windows_error_dialogs()
            self.assertIsInstance(res, int)
            self.assertEqual(DEFAULT_ERROR_MODE, 0x8001)


class TestDriveMonitorCallbacksAndLifecycle(unittest.TestCase):
    """Unit tests for DriveMonitor polling, callbacks, deduplication, and error isolation."""

    def setUp(self):
        self.mock_detector = MagicMock(spec=WindowsDriveDetector)
        self.mock_detector.get_drive_letters_from_mask.side_effect = WindowsDriveDetector.get_drive_letters_from_mask
        self.monitor = DriveMonitor(
            poll_interval=0.05,
            auto_suppress_errors=False,
            detector=self.mock_detector,
        )

    def tearDown(self):
        if self.monitor.is_running():
            self.monitor.stop()

    def test_init_with_callbacks(self):
        """DriveMonitor accepts initial on_inserted and on_removed callbacks in __init__."""
        ins = MagicMock()
        rem = MagicMock()
        mon = DriveMonitor(on_inserted=ins, on_removed=rem, auto_suppress_errors=False)
        self.assertIn(ins, mon._on_inserted_callbacks)
        self.assertIn(rem, mon._on_removed_callbacks)

    def test_callback_registration_via_methods_and_decorators(self):
        """Callbacks can be registered via init, add_ methods, or decorators."""
        inserted_1 = MagicMock()
        inserted_2 = MagicMock()
        removed_1 = MagicMock()
        removed_2 = MagicMock()

        self.monitor.add_on_inserted_callback(inserted_1)
        self.monitor.add_on_removed_callback(removed_1)

        @self.monitor.on_inserted
        def dec_inserted(info):
            inserted_2(info)

        @self.monitor.on_removed
        def dec_removed(letter):
            removed_2(letter)

        self.assertIn(inserted_1, self.monitor._on_inserted_callbacks)
        self.assertIn(dec_inserted, self.monitor._on_inserted_callbacks)
        self.assertIn(removed_1, self.monitor._on_removed_callbacks)
        self.assertIn(dec_removed, self.monitor._on_removed_callbacks)

        # Removal of callbacks
        self.monitor.remove_on_inserted_callback(inserted_1)
        self.assertNotIn(inserted_1, self.monitor._on_inserted_callbacks)
        self.monitor.remove_on_removed_callback(removed_1)
        self.assertNotIn(removed_1, self.monitor._on_removed_callbacks)

    def test_start_and_stop_lifecycle(self):
        """start() launches background thread, is_running() reports True, stop() terminates it."""
        self.mock_detector.get_logical_drives_mask.return_value = 4  # C:
        c_info = DriveInfo(letter="C:", drive_type="FIXED", is_ready=True)
        self.mock_detector.inspect_drive.return_value = c_info

        self.assertFalse(self.monitor.is_running())
        self.monitor.start()
        self.assertTrue(self.monitor.is_running())

        # Idempotent start call
        self.monitor.start()
        self.assertTrue(self.monitor.is_running())

        self.monitor.stop()
        self.assertFalse(self.monitor.is_running())

        # Idempotent stop call
        self.monitor.stop()
        self.assertFalse(self.monitor.is_running())

    def test_scan_existing_at_startup_flag(self):
        """When scan_existing_at_startup=True, existing scannable drives trigger callback."""
        on_inserted = MagicMock()
        monitor = DriveMonitor(
            poll_interval=0.05,
            scan_existing_at_startup=True,
            auto_suppress_errors=False,
            detector=self.mock_detector,
        )
        monitor.add_on_inserted_callback(on_inserted)

        self.mock_detector.get_logical_drives_mask.return_value = 4 | (1 << 23)  # C: and X:

        def mock_inspect(letter, probe_retries=1):
            if letter == "C:":
                return DriveInfo(letter="C:", drive_type="FIXED", is_ready=True)
            elif letter == "X:":
                return DriveInfo(letter="X:", drive_type="SUBST", is_subst=True, is_ready=True)
            return DriveInfo(letter=letter)

        self.mock_detector.inspect_drive.side_effect = mock_inspect

        monitor.start()
        try:
            # X: should have fired on_inserted, but C: should not
            on_inserted.assert_called_once()
            call_info = on_inserted.call_args[0][0]
            self.assertEqual(call_info.letter, "X:")
            self.assertTrue(call_info.is_subst)
        finally:
            monitor.stop()

    def test_insertion_and_single_event_deduplication(self):
        """Inserted drive triggers on_inserted exactly once while remaining mounted."""
        on_inserted = MagicMock()
        on_removed = MagicMock()
        self.monitor.add_on_inserted_callback(on_inserted)
        self.monitor.add_on_removed_callback(on_removed)

        # Baseline: only C: mounted
        self.monitor._active_drives["C:"] = DriveInfo(letter="C:", drive_type="FIXED", is_ready=True)

        # Step 1: Poll once with baseline
        self.mock_detector.get_logical_drives_mask.return_value = 4  # C:
        self.mock_detector.inspect_drive.return_value = DriveInfo(letter="C:", drive_type="FIXED", is_ready=True)
        self.monitor._poll_step()
        on_inserted.assert_not_called()

        # Step 2: New USB drive E: (bit 4, 1 << 4 = 16) added
        e_info = DriveInfo(letter="E:", drive_type="REMOVABLE", volume_name="USB_DISK", serial_number=999, is_ready=True)
        self.mock_detector.get_logical_drives_mask.return_value = 4 | 16  # C: and E:
        self.mock_detector.inspect_drive.side_effect = lambda l, **kw: e_info if l == "E:" else DriveInfo(letter="C:")

        self.monitor._poll_step()
        self.assertEqual(on_inserted.call_count, 1)
        self.assertEqual(on_inserted.call_args[0][0].letter, "E:")

        # Step 3: Next poll cycle with E: still present -> NO duplicate trigger!
        self.monitor._poll_step()
        self.assertEqual(on_inserted.call_count, 1)

        self.monitor._poll_step()
        self.assertEqual(on_inserted.call_count, 1)
        on_removed.assert_not_called()

    def test_removal_and_remount_lifecycle(self):
        """Unmounting fires on_removed, remounting fires on_inserted again."""
        on_inserted = MagicMock()
        on_removed = MagicMock()
        self.monitor.add_on_inserted_callback(on_inserted)
        self.monitor.add_on_removed_callback(on_removed)

        # Baseline: C: and X: (subst drive)
        x_info = DriveInfo(letter="X:", drive_type="SUBST", is_subst=True, volume_name="GOG_GAME", is_ready=True)
        self.monitor._active_drives["C:"] = DriveInfo(letter="C:", drive_type="FIXED", is_ready=True)
        self.monitor._active_drives["X:"] = x_info

        # Step 1: Drive X: is unmounted (subst X: /d)
        self.mock_detector.get_logical_drives_mask.return_value = 4  # Only C:
        self.mock_detector.inspect_drive.side_effect = lambda l, **kw: DriveInfo(letter="C:")

        self.monitor._poll_step()
        on_removed.assert_called_once_with("X:")
        self.assertEqual(on_inserted.call_count, 0)
        self.assertNotIn("X:", self.monitor._active_drives)

        # Step 2: Drive X: is remounted (subst X: folder)
        self.mock_detector.get_logical_drives_mask.return_value = 4 | (1 << 23)  # C: and X:
        self.mock_detector.inspect_drive.side_effect = lambda l, **kw: x_info if l == "X:" else DriveInfo(letter="C:")

        self.monitor._poll_step()
        self.assertEqual(on_inserted.call_count, 1)
        self.assertEqual(on_inserted.call_args[0][0].letter, "X:")
        self.assertIn("X:", self.monitor._active_drives)

    def test_simultaneous_addition_and_removal(self):
        """DriveMonitor handles one drive being removed and another added in the same step."""
        on_inserted = MagicMock()
        on_removed = MagicMock()
        self.monitor.add_on_inserted_callback(on_inserted)
        self.monitor.add_on_removed_callback(on_removed)

        # Initial state: C: and E: (USB 1)
        e_info = DriveInfo(letter="E:", drive_type="REMOVABLE", volume_name="USB1", is_ready=True)
        self.monitor._active_drives["C:"] = DriveInfo(letter="C:", drive_type="FIXED", is_ready=True)
        self.monitor._active_drives["E:"] = e_info

        # Transition: E: removed, F: added (bit 5 = 32)
        f_info = DriveInfo(letter="F:", drive_type="REMOVABLE", volume_name="USB2", is_ready=True)
        self.mock_detector.get_logical_drives_mask.return_value = 4 | 32  # C: and F:
        self.mock_detector.inspect_drive.side_effect = lambda l, **kw: f_info if l == "F:" else DriveInfo(letter="C:")

        self.monitor._poll_step()
        on_removed.assert_called_once_with("E:")
        on_inserted.assert_called_once()
        self.assertEqual(on_inserted.call_args[0][0].letter, "F:")
        self.assertNotIn("E:", self.monitor._active_drives)
        self.assertIn("F:", self.monitor._active_drives)

    def test_cdrom_media_insertion_and_ejection(self):
        """Optical CD-ROM disc insertion and ejection on fixed drive letter is detected."""
        on_inserted = MagicMock()
        on_removed = MagicMock()
        self.monitor.add_on_inserted_callback(on_inserted)
        self.monitor.add_on_removed_callback(on_removed)

        # Optical drive D: is present in bitmask but initially empty (is_ready=False)
        empty_d = DriveInfo(letter="D:", drive_type="CDROM", raw_type=DRIVE_CDROM, is_ready=False)
        self.monitor._active_drives["C:"] = DriveInfo(letter="C:", drive_type="FIXED", is_ready=True)
        self.monitor._active_drives["D:"] = empty_d

        self.mock_detector.get_logical_drives_mask.return_value = 4 | 8  # C: and D:
        self.mock_detector.inspect_drive.side_effect = lambda l, **kw: empty_d if l == "D:" else DriveInfo(letter="C:")

        self.monitor._poll_step()
        self.assertEqual(on_inserted.call_count, 0)

        # Disc is inserted into D: -> is_ready becomes True
        inserted_d = DriveInfo(letter="D:", drive_type="CDROM", raw_type=DRIVE_CDROM, volume_name="DISC_1", serial_number=55555, is_ready=True)
        self.mock_detector.inspect_drive.side_effect = lambda l, **kw: inserted_d if l == "D:" else DriveInfo(letter="C:")

        self.monitor._poll_step()
        self.assertEqual(on_inserted.call_count, 1)
        self.assertEqual(on_inserted.call_args[0][0].volume_name, "DISC_1")
        self.assertEqual(on_removed.call_count, 0)

        # Disc is ejected from D: -> is_ready becomes False
        self.mock_detector.inspect_drive.side_effect = lambda l, **kw: empty_d if l == "D:" else DriveInfo(letter="C:")

        self.monitor._poll_step()
        self.assertEqual(on_removed.call_count, 1)
        self.assertEqual(on_removed.call_args[0][0], "D:")

    def test_cdrom_disc_swap(self):
        """Swapping disc in CD-ROM drive triggers removal of old disc and insertion of new disc."""
        on_inserted = MagicMock()
        on_removed = MagicMock()
        self.monitor.add_on_inserted_callback(on_inserted)
        self.monitor.add_on_removed_callback(on_removed)

        disc1 = DriveInfo(letter="D:", drive_type="CDROM", raw_type=DRIVE_CDROM, volume_name="DISC_1", serial_number=11111, is_ready=True)
        disc2 = DriveInfo(letter="D:", drive_type="CDROM", raw_type=DRIVE_CDROM, volume_name="DISC_2", serial_number=22222, is_ready=True)

        self.monitor._active_drives["C:"] = DriveInfo(letter="C:", drive_type="FIXED", is_ready=True)
        self.monitor._active_drives["D:"] = disc1

        self.mock_detector.get_logical_drives_mask.return_value = 4 | 8  # C: and D:

        # Disc 2 swapped in
        self.mock_detector.inspect_drive.side_effect = lambda l, **kw: disc2 if l == "D:" else DriveInfo(letter="C:")
        self.monitor._poll_step()

        self.assertEqual(on_removed.call_count, 1)
        self.assertEqual(on_removed.call_args[0][0], "D:")
        self.assertEqual(on_inserted.call_count, 1)
        self.assertEqual(on_inserted.call_args[0][0].volume_name, "DISC_2")

    def test_scan_now_and_get_active_drives(self):
        """scan_now performs synchronous scan and get_active_drives returns snapshot."""
        self.mock_detector.get_logical_drives_mask.return_value = 4 | 16
        c_info = DriveInfo(letter="C:", drive_type="FIXED", is_ready=True)
        e_info = DriveInfo(letter="E:", drive_type="REMOVABLE", is_ready=True)

        def mock_inspect(letter, **kw):
            return e_info if letter == "E:" else c_info

        self.mock_detector.inspect_drive.side_effect = mock_inspect

        drives = self.monitor.scan_now()
        self.assertEqual(len(drives), 2)
        letters = {d.letter for d in drives}
        self.assertEqual(letters, {"C:", "E:"})

        active = self.monitor.get_active_drives()
        self.assertIn("C:", active)
        self.assertIn("E:", active)

    def test_callback_exception_isolation(self):
        """An exception raised inside a user callback does not stop the monitor loop."""
        def bad_inserted_callback(info):
            raise RuntimeError("Intentional error in callback")

        def bad_removed_callback(letter):
            raise ValueError("Intentional removal error")

        good_inserted_callback = MagicMock()
        good_removed_callback = MagicMock()

        self.monitor.add_on_inserted_callback(bad_inserted_callback)
        self.monitor.add_on_inserted_callback(good_inserted_callback)
        self.monitor.add_on_removed_callback(bad_removed_callback)
        self.monitor.add_on_removed_callback(good_removed_callback)

        self.monitor._active_drives["C:"] = DriveInfo(letter="C:", drive_type="FIXED", is_ready=True)

        # Add drive
        e_info = DriveInfo(letter="E:", drive_type="REMOVABLE", is_ready=True)
        self.mock_detector.get_logical_drives_mask.return_value = 4 | 16
        self.mock_detector.inspect_drive.side_effect = lambda l, **kw: e_info if l == "E:" else DriveInfo(letter="C:")

        self.monitor._poll_step()
        # good_inserted_callback should still have executed
        good_inserted_callback.assert_called_once_with(e_info)

        # Remove drive
        self.mock_detector.get_logical_drives_mask.return_value = 4
        self.mock_detector.inspect_drive.side_effect = lambda l, **kw: DriveInfo(letter="C:")

        self.monitor._poll_step()
        # good_removed_callback should still have executed
        good_removed_callback.assert_called_once_with("E:")


class TestDriveSimulatorAndWindowsIntegration(unittest.TestCase):
    """
    Integration tests using the Windows subst command (or simulated when non-Windows).
    Validates end-to-end mounting, detection, unmounting, and remounting.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="gog_test_disc_")
        self.test_drive = DriveSimulator.find_available_drive_letter()

    def tearDown(self):
        if self.test_drive and sys.platform == "win32":
            DriveSimulator.unmount_subst(self.test_drive)
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_find_available_drive_letter_custom_list(self):
        """find_available_drive_letter picks first free letter from candidates."""
        with patch.object(WindowsDriveDetector, "get_logical_drives_mask", return_value=4):  # only C:
            letter = DriveSimulator.find_available_drive_letter(preferred=("C:", "Z:", "Y:"))
            self.assertEqual(letter, "Z:")

    @unittest.skipUnless(sys.platform == "win32", "Requires Windows OS for subst testing")
    def test_real_windows_subst_detection_and_lifecycle(self):
        """End-to-end integration test of DriveMonitor with Windows subst."""
        self.assertIsNotNone(self.test_drive, "Available drive letter must be found")

        # Create mock disc content in temp_dir
        with open(os.path.join(self.temp_dir, "gog_game.json"), "w", encoding="utf-8") as f:
            f.write('{"game_id": "test_game", "title": "Test Game", "version": "1.0.0"}')

        inserted_events = []
        removed_events = []
        inserted_cv = threading.Condition()
        removed_cv = threading.Condition()

        def handle_inserted(info: DriveInfo):
            with inserted_cv:
                inserted_events.append(info)
                inserted_cv.notify_all()

        def handle_removed(letter: str):
            with removed_cv:
                removed_events.append(letter)
                removed_cv.notify_all()

        monitor = DriveMonitor(poll_interval=0.15)
        monitor.add_on_inserted_callback(handle_inserted)
        monitor.add_on_removed_callback(handle_removed)

        monitor.start()
        try:
            # 1. Mount subst drive
            mounted = DriveSimulator.mount_subst(self.test_drive, self.temp_dir)
            self.assertTrue(mounted, f"Failed to mount {self.test_drive} -> {self.temp_dir}")

            # Wait for insertion event
            with inserted_cv:
                if not any(e.letter == self.test_drive for e in inserted_events):
                    inserted_cv.wait(timeout=3.0)

            matching_inserted = [e for e in inserted_events if e.letter == self.test_drive]
            self.assertTrue(len(matching_inserted) >= 1, f"Expected on_inserted callback to fire for {self.test_drive}")
            event_info = matching_inserted[0]
            self.assertEqual(event_info.letter, self.test_drive)
            self.assertTrue(event_info.is_subst)
            self.assertEqual(event_info.drive_type, "SUBST")
            self.assertTrue(event_info.is_ready)
            self.assertTrue(event_info.is_candidate_for_game_disk)

            # 2. Unmount subst drive
            unmounted = DriveSimulator.unmount_subst(self.test_drive)
            self.assertTrue(unmounted, f"Failed to unmount {self.test_drive}")

            # Wait for removal event
            with removed_cv:
                if self.test_drive not in removed_events:
                    removed_cv.wait(timeout=3.0)

            self.assertTrue(len(removed_events) >= 1, "Expected on_removed callback to fire")
            self.assertIn(self.test_drive, removed_events)

            # 3. Remount subst drive (verifying remount trigger for launch flow)
            inserted_events.clear()
            mounted_again = DriveSimulator.mount_subst(self.test_drive, self.temp_dir)
            self.assertTrue(mounted_again)

            with inserted_cv:
                if not any(e.letter == self.test_drive for e in inserted_events):
                    inserted_cv.wait(timeout=3.0)

            matching_remount = [e for e in inserted_events if e.letter == self.test_drive]
            self.assertTrue(len(matching_remount) >= 1, f"Expected on_inserted callback on remount for {self.test_drive}")
            self.assertEqual(matching_remount[0].letter, self.test_drive)

        finally:
            DriveSimulator.unmount_subst(self.test_drive)
            monitor.stop()

    @unittest.skipUnless(sys.platform == "win32", "Requires Windows OS for subst testing")
    def test_temp_subst_context_manager(self):
        """DriveSimulator.temp_subst context manager mounts and automatically cleans up."""
        self.assertIsNotNone(self.test_drive)

        with DriveSimulator.temp_subst(self.temp_dir, self.test_drive) as drive_letter:
            self.assertEqual(drive_letter, self.test_drive)
            # Verify drive is accessible and detected as subst
            detector = WindowsDriveDetector()
            mask = detector.get_logical_drives_mask()
            letters = detector.get_drive_letters_from_mask(mask)
            self.assertIn(self.test_drive, letters)

            info = detector.inspect_drive(self.test_drive)
            self.assertTrue(info.is_subst)
            self.assertEqual(info.drive_type, "SUBST")

        # After block exit, verify unmounted
        detector = WindowsDriveDetector()
        mask = detector.get_logical_drives_mask()
        letters = detector.get_drive_letters_from_mask(mask)
        self.assertNotIn(self.test_drive, letters)


if __name__ == "__main__":
    unittest.main()
