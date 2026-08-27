"""
gog_disk_monitor.drive_monitor
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Windows Drive Detection & Monitoring Engine.
Provides background monitoring of logical drives (physical removable drives,
optical CD/DVD discs, and simulated subst virtual drives) using Windows Win32
kernel32 APIs with error mode suppression, deduplication, and lifecycle callbacks.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

logger = logging.getLogger("gog_disk_monitor.drive_monitor")

# Windows Error Mode Flags (SetErrorMode)
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOGPFAULTERRORBOX = 0x0002
SEM_NOALIGNMENTFAULTEXCEPT = 0x0004
SEM_NOOPENFILEERRORBOX = 0x8000
DEFAULT_ERROR_MODE = SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX

# Windows Drive Type Constants (GetDriveTypeW)
DRIVE_UNKNOWN = 0       # The drive type cannot be determined.
DRIVE_NO_ROOT_DIR = 1   # The root path is invalid; for example, there is no volume mounted at the specified path.
DRIVE_REMOVABLE = 2     # The drive has removable media; for example, a floppy drive, thumb drive, or flash card reader.
DRIVE_FIXED = 3         # The drive has fixed media; for example, a hard disk drive or flash drive.
DRIVE_REMOTE = 4        # The drive is a remote (network) drive.
DRIVE_CDROM = 5         # The drive is an optical disc drive (CD-ROM, DVD-ROM, BD-ROM).
DRIVE_RAMDISK = 6       # The drive is a RAM disk.

DRIVE_TYPE_NAMES: Dict[int, str] = {
    DRIVE_UNKNOWN: "UNKNOWN",
    DRIVE_NO_ROOT_DIR: "NO_ROOT_DIR",
    DRIVE_REMOVABLE: "REMOVABLE",
    DRIVE_FIXED: "FIXED",
    DRIVE_REMOTE: "NETWORK",
    DRIVE_CDROM: "CDROM",
    DRIVE_RAMDISK: "RAMDISK",
}


def suppress_windows_error_dialogs(mode: int = DEFAULT_ERROR_MODE) -> int:
    """
    Suppresses Windows critical error dialogs process-wide to prevent
    'There is no disk in the drive' system modal popups from blocking background threads.

    Returns the previous error mode integer, or 0 on non-Windows platforms.
    """
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            prev_mode = kernel32.SetErrorMode(mode)
            # Also set thread error mode if available (Windows 7+)
            if hasattr(kernel32, "SetThreadErrorMode"):
                try:
                    kernel32.SetThreadErrorMode(mode, None)
                except Exception:
                    pass
            return prev_mode
        except Exception as ex:
            logger.warning("Failed to invoke SetErrorMode on kernel32: %s", ex)
            return 0
    return 0


# Suppress critical error dialogs on module import
suppress_windows_error_dialogs()


@dataclass
class DriveInfo:
    """
    Metadata describing a detected logical drive volume.

    Attributes:
        letter: Drive letter with colon, e.g. 'X:' or 'C:'
        drive_type: Classification string ('REMOVABLE', 'CDROM', 'SUBST', 'FIXED', etc.)
        volume_name: Volume label reported by the filesystem, e.g. 'BALDURS_GATE'
        serial_number: Volume serial number as unsigned integer (or None if unavailable)
        is_subst: True if mapped via Windows subst / DefineDosDevice symbolic link
        dos_target: Target NT device path (e.g. '\\??\\C:\\games\\cd1' or '\\Device\\HarddiskVolume3')
        file_system: Filesystem name, e.g. 'NTFS', 'FAT32', 'CDFS', 'UDF'
        root_path: Root path with trailing backslash, e.g. 'X:\\'
        raw_type: Raw integer from GetDriveTypeW
        is_ready: True if the volume is mounted, accessible, and readable
        subst_target: Resolved local folder path if is_subst is True
    """
    letter: str
    drive_type: str = "UNKNOWN"
    volume_name: str = ""
    serial_number: Optional[int] = None
    is_subst: bool = False
    dos_target: Optional[str] = None
    file_system: Optional[str] = None
    root_path: str = ""
    raw_type: int = 0
    is_ready: bool = True
    subst_target: Optional[str] = None

    def __post_init__(self) -> None:
        # Normalize letter to uppercase with colon (e.g. 'x' -> 'X:', 'X:\\' -> 'X:')
        clean = self.letter.strip().rstrip("\\/").rstrip(":").upper()
        if clean:
            self.letter = f"{clean}:"
        else:
            self.letter = "UNKNOWN:"

        # Ensure root_path is set properly with trailing backslash
        if not self.root_path:
            self.root_path = f"{self.letter}\\"
        elif not self.root_path.endswith("\\") and not self.root_path.endswith("/"):
            self.root_path = f"{self.root_path}\\"

        # Detect subst targets from dos_target
        if self.dos_target:
            target = self.dos_target.strip()
            if target.startswith("\\??\\"):
                self.is_subst = True
                if self.subst_target is None:
                    self.subst_target = target[4:]
            elif target.startswith("\\DosDevices\\"):
                self.is_subst = True
                if self.subst_target is None:
                    self.subst_target = target[12:]

        # If subst_target was provided directly without dos_target
        if self.subst_target and not self.dos_target:
            self.is_subst = True
            self.dos_target = f"\\??\\{self.subst_target}"

        # Adjust drive_type if is_subst is True
        if self.is_subst and self.drive_type in ("UNKNOWN", "FIXED", ""):
            self.drive_type = "SUBST"

    @property
    def filesystem(self) -> Optional[str]:
        """Alias for file_system."""
        return self.file_system

    @filesystem.setter
    def filesystem(self, value: Optional[str]) -> None:
        self.file_system = value

    @property
    def is_candidate_for_game_disk(self) -> bool:
        """
        True if this drive is a candidate for holding a GOG game disc
        (REMOVABLE, CDROM, or SUBST virtual drive).
        """
        return self.is_ready and (
            self.is_subst or self.drive_type in ("REMOVABLE", "CDROM", "SUBST")
        )


class WindowsDriveDetector:
    """
    Encapsulates low-level Win32 kernel32 drive discovery and volume query APIs.
    """

    def __init__(self, kernel32_dll: Optional[Any] = None) -> None:
        if kernel32_dll is not None:
            self._kernel32 = kernel32_dll
        elif sys.platform == "win32":
            self._kernel32 = ctypes.windll.kernel32
        else:
            self._kernel32 = None

    @property
    def is_available(self) -> bool:
        """True if running on Windows kernel32 or a valid mock is supplied."""
        return self._kernel32 is not None

    def get_logical_drives_mask(self) -> int:
        """
        Returns a 32-bit integer bitmask of all active logical drive letters.
        Bit 0 corresponds to A:, Bit 2 to C:, Bit 25 to Z:.
        """
        if not self._kernel32:
            return 0
        try:
            return int(self._kernel32.GetLogicalDrives())
        except Exception as ex:
            logger.error("Error invoking GetLogicalDrives: %s", ex)
            return 0

    @staticmethod
    def get_drive_letters_from_mask(mask: int) -> Set[str]:
        """
        Converts a 32-bit drive bitmask into a set of drive letter strings (e.g. {'C:', 'D:'}).
        """
        letters: Set[str] = set()
        for i in range(26):
            if mask & (1 << i):
                letter = f"{chr(ord('A') + i)}:"
                letters.add(letter)
        return letters

    def get_dos_device_target(self, drive_letter: str) -> str:
        """
        Queries the MS-DOS device target string for a drive letter (e.g. 'X:').
        Returns target string like '\\??\\C:\\folder' or '\\Device\\HarddiskVolume3'.
        """
        if not self._kernel32:
            return ""
        clean_letter = drive_letter.strip().rstrip("\\/")
        buf = ctypes.create_unicode_buffer(1024)
        try:
            res = self._kernel32.QueryDosDeviceW(clean_letter, buf, 1024)
            if res > 0:
                return buf.value
        except Exception as ex:
            logger.debug("Error querying QueryDosDeviceW for %s: %s", drive_letter, ex)
        return ""

    def is_subst_drive(self, drive_letter: str) -> Tuple[bool, Optional[str]]:
        """
        Determines whether a drive letter is mapped via the Windows subst command.
        Returns (is_subst, subst_target_path).
        """
        target = self.get_dos_device_target(drive_letter)
        if target.startswith("\\??\\"):
            return True, target[4:]
        elif target.startswith("\\DosDevices\\"):
            return True, target[12:]
        return False, None

    def get_drive_type_info(self, root_path: str) -> Tuple[int, str]:
        """
        Retrieves raw drive type integer and normalized name for a drive root (e.g. 'C:\\').
        """
        if not self._kernel32:
            return DRIVE_UNKNOWN, "UNKNOWN"
        try:
            raw_type = int(self._kernel32.GetDriveTypeW(root_path))
            name = DRIVE_TYPE_NAMES.get(raw_type, "UNKNOWN")
            return raw_type, name
        except Exception as ex:
            logger.debug("Error querying GetDriveTypeW for %s: %s", root_path, ex)
            return DRIVE_UNKNOWN, "UNKNOWN"

    def get_volume_information(self, root_path: str) -> Optional[Dict[str, Any]]:
        """
        Safely retrieves volume metadata (volume name, serial number, filesystem name)
        without triggering modal error dialogs.
        """
        if not self._kernel32:
            return None

        vol_name_buf = ctypes.create_unicode_buffer(261)
        file_sys_buf = ctypes.create_unicode_buffer(261)
        serial_num = wintypes.DWORD(0)
        max_comp_len = wintypes.DWORD(0)
        flags = wintypes.DWORD(0)

        try:
            res = self._kernel32.GetVolumeInformationW(
                root_path,
                vol_name_buf,
                261,
                ctypes.byref(serial_num),
                ctypes.byref(max_comp_len),
                ctypes.byref(flags),
                file_sys_buf,
                261,
            )
            if res:
                return {
                    "volume_name": vol_name_buf.value,
                    "serial_number": int(serial_num.value),
                    "file_system": file_sys_buf.value,
                    "max_component_length": int(max_comp_len.value),
                    "flags": int(flags.value),
                }
        except Exception as ex:
            logger.debug("Error in GetVolumeInformationW for %s: %s", root_path, ex)

        return None

    def inspect_drive(self, drive_letter: str, probe_retries: int = 4, retry_delay: float = 0.15) -> DriveInfo:
        """
        Performs a full inspection of a logical drive letter, extracting type,
        subst status, target, and volume information (retrying if media is freshly mounted).
        """
        clean_letter = drive_letter.strip().rstrip("\\/").rstrip(":").upper()
        clean_letter = f"{clean_letter}:"
        root_path = f"{clean_letter}\\"

        raw_type, type_name = self.get_drive_type_info(root_path)
        dos_target = self.get_dos_device_target(clean_letter)
        is_subst, subst_target = self.is_subst_drive(clean_letter)

        if is_subst:
            type_name = "SUBST"

        # Probe volume information with retries (for fresh USB mounts or optical disc spin-up)
        vol_info: Optional[Dict[str, Any]] = None
        for attempt in range(max(1, probe_retries)):
            vol_info = self.get_volume_information(root_path)
            if vol_info is not None:
                break
            if attempt < probe_retries - 1:
                time.sleep(retry_delay)

        if vol_info is not None:
            return DriveInfo(
                letter=clean_letter,
                root_path=root_path,
                drive_type=type_name,
                raw_type=raw_type,
                is_subst=is_subst,
                dos_target=dos_target or (f"\\??\\{subst_target}" if subst_target else None),
                subst_target=subst_target,
                volume_name=vol_info.get("volume_name", ""),
                serial_number=vol_info.get("serial_number"),
                file_system=vol_info.get("file_system", ""),
                is_ready=True,
            )
        else:
            # Drive letter exists but media is not ready (e.g. empty CD-ROM tray)
            return DriveInfo(
                letter=clean_letter,
                root_path=root_path,
                drive_type=type_name,
                raw_type=raw_type,
                is_subst=is_subst,
                dos_target=dos_target or (f"\\??\\{subst_target}" if subst_target else None),
                subst_target=subst_target,
                volume_name="",
                serial_number=None,
                file_system=None,
                is_ready=False,
            )


class DriveMonitor:
    """
    Continuous background daemon monitor for Windows drive arrival, removal,
    and optical disc media changes.

    Features:
        - 500ms lightweight polling via GetLogicalDrives()
        - SetErrorMode suppression against system error modal dialogs
        - Detects USB Removable, Optical CD/DVD, and subst virtual drives
        - Single-event deduplication per mount session
        - Clean removal handling enabling re-mount triggers
        - Optical disc insertion & ejection tracking on existing drive letters
        - Thread-safe public API with graceful callback error isolation
    """

    def __init__(
        self,
        poll_interval: float = 0.5,
        on_inserted: Optional[Callable[[DriveInfo], None]] = None,
        on_removed: Optional[Callable[[str], None]] = None,
        scan_existing_at_startup: bool = False,
        auto_suppress_errors: bool = True,
        detector: Optional[WindowsDriveDetector] = None,
    ) -> None:
        """
        Initializes the DriveMonitor.

        Args:
            poll_interval: Interval in seconds between logical drive bitmask checks (default 0.5s).
            on_inserted: Optional initial callback for drive insertion events.
            on_removed: Optional initial callback for drive removal events.
            scan_existing_at_startup: If True, fires on_inserted for scannable drives existing at start().
            auto_suppress_errors: If True, configures Windows SetErrorMode to suppress modal dialogs.
            detector: Optional WindowsDriveDetector instance (useful for testing and dependency injection).
        """
        self.poll_interval = max(0.05, float(poll_interval))
        self.scan_existing_at_startup = scan_existing_at_startup
        self._detector = detector if detector is not None else WindowsDriveDetector()

        if auto_suppress_errors:
            suppress_windows_error_dialogs()

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._active_drives: Dict[str, DriveInfo] = {}
        self._on_inserted_callbacks: List[Callable[[DriveInfo], None]] = []
        self._on_removed_callbacks: List[Callable[[str], None]] = []

        if on_inserted is not None:
            self.add_on_inserted_callback(on_inserted)
        if on_removed is not None:
            self.add_on_removed_callback(on_removed)

    def add_on_inserted_callback(self, callback: Callable[[DriveInfo], None]) -> None:
        """Registers a callback to be invoked when a drive is inserted or becomes ready."""
        with self._lock:
            if callback not in self._on_inserted_callbacks:
                self._on_inserted_callbacks.append(callback)

    def add_on_removed_callback(self, callback: Callable[[str], None]) -> None:
        """Registers a callback to be invoked when a drive is unmounted or ejected."""
        with self._lock:
            if callback not in self._on_removed_callbacks:
                self._on_removed_callbacks.append(callback)

    def remove_on_inserted_callback(self, callback: Callable[[DriveInfo], None]) -> None:
        """Removes a previously registered insertion callback."""
        with self._lock:
            if callback in self._on_inserted_callbacks:
                self._on_inserted_callbacks.remove(callback)

    def remove_on_removed_callback(self, callback: Callable[[str], None]) -> None:
        """Removes a previously registered removal callback."""
        with self._lock:
            if callback in self._on_removed_callbacks:
                self._on_removed_callbacks.remove(callback)

    def on_inserted(self, callback: Optional[Callable[[DriveInfo], None]] = None) -> Any:
        """
        Decorator or helper method to register an insertion callback.
        Usage:
            @monitor.on_inserted
            def handle_inserted(drive_info: DriveInfo): ...
            # or
            monitor.on_inserted(handle_inserted)
        """
        if callback is None:
            def decorator(fn: Callable[[DriveInfo], None]) -> Callable[[DriveInfo], None]:
                self.add_on_inserted_callback(fn)
                return fn
            return decorator
        self.add_on_inserted_callback(callback)
        return callback

    def on_removed(self, callback: Optional[Callable[[str], None]] = None) -> Any:
        """
        Decorator or helper method to register a removal callback.
        Usage:
            @monitor.on_removed
            def handle_removed(drive_letter: str): ...
            # or
            monitor.on_removed(handle_removed)
        """
        if callback is None:
            def decorator(fn: Callable[[str], None]) -> Callable[[str], None]:
                self.add_on_removed_callback(fn)
                return fn
            return decorator
        self.add_on_removed_callback(callback)
        return callback

    def is_running(self) -> bool:
        """Returns True if the background monitoring thread is actively running."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()

    def start(self, scan_existing_at_startup: Optional[bool] = None) -> None:
        """
        Starts the background monitoring daemon thread.

        Args:
            scan_existing_at_startup: Overrides the initial scan behavior configured in __init__.
        """
        with self._lock:
            if self.is_running():
                logger.debug("DriveMonitor is already running.")
                return

            if scan_existing_at_startup is not None:
                self.scan_existing_at_startup = scan_existing_at_startup

            self._stop_event.clear()

            # Snapshot initial system state
            mask = self._detector.get_logical_drives_mask()
            current_letters = self._detector.get_drive_letters_from_mask(mask)
            self._active_drives.clear()

            for letter in sorted(current_letters):
                info = self._detector.inspect_drive(letter, probe_retries=1)
                self._active_drives[letter] = info
                if self.scan_existing_at_startup and info.is_candidate_for_game_disk:
                    self._dispatch_inserted(info)

            self._thread = threading.Thread(
                target=self._monitor_loop,
                name="DriveMonitorThread",
                daemon=True,
            )
            self._thread.start()
            logger.info("DriveMonitor started (polling interval: %.2fs).", self.poll_interval)

    def stop(self, timeout: float = 2.0) -> None:
        """
        Stops the background monitoring thread cleanly.

        Args:
            timeout: Maximum seconds to wait for thread join (default 2.0s).
        """
        with self._lock:
            if not self.is_running():
                return
            self._stop_event.set()
            thread = self._thread
            self._thread = None

        if thread and thread.is_alive():
            thread.join(timeout=timeout)
            logger.info("DriveMonitor stopped.")

    def get_active_drives(self) -> Dict[str, DriveInfo]:
        """
        Returns a thread-safe snapshot dictionary of currently active/mounted drives.
        """
        with self._lock:
            return dict(self._active_drives)

    def scan_now(self) -> List[DriveInfo]:
        """
        Performs an immediate synchronous scan of all logical drives, updating
        internal tracking and returning a list of current DriveInfo objects.
        """
        mask = self._detector.get_logical_drives_mask()
        letters = self._detector.get_drive_letters_from_mask(mask)
        drives: List[DriveInfo] = []

        with self._lock:
            for letter in sorted(letters):
                info = self._detector.inspect_drive(letter, probe_retries=1)
                self._active_drives[letter] = info
                drives.append(info)
        return drives

    def _dispatch_inserted(self, drive_info: DriveInfo) -> None:
        """Dispatches on_inserted callback to all registered listeners safely."""
        callbacks = list(self._on_inserted_callbacks)
        for cb in callbacks:
            try:
                cb(drive_info)
            except Exception as ex:
                logger.exception("Exception in on_inserted callback: %s", ex)

    def _dispatch_removed(self, drive_letter: str) -> None:
        """Dispatches on_removed callback to all registered listeners safely."""
        callbacks = list(self._on_removed_callbacks)
        for cb in callbacks:
            try:
                cb(drive_letter)
            except Exception as ex:
                logger.exception("Exception in on_removed callback: %s", ex)

    def _monitor_loop(self) -> None:
        """
        Main polling loop executed in the background daemon thread.
        """
        while not self._stop_event.is_set():
            try:
                self._poll_step()
            except Exception as ex:
                logger.exception("Unexpected error in DriveMonitor poll step: %s", ex)

            # Responsive sleep interrupted immediately when stop() is called
            self._stop_event.wait(self.poll_interval)

    def _poll_step(self) -> None:
        """
        Executes a single polling iteration:
        1. Compares drive bitmasks to detect removed and added drive letters.
        2. Detects media state transitions (e.g. optical discs in existing CD-ROM letters).
        3. Fires single-event insertion and removal callbacks.
        """
        mask = self._detector.get_logical_drives_mask()
        current_letters = self._detector.get_drive_letters_from_mask(mask)

        insertions_to_notify: List[DriveInfo] = []
        removals_to_notify: List[str] = []

        with self._lock:
            known_letters = set(self._active_drives.keys())

            # 1. Detect removed drive letters (e.g. subst X: /d or USB unplugged)
            removed_letters = known_letters - current_letters
            for letter in removed_letters:
                del self._active_drives[letter]
                removals_to_notify.append(letter)

            # 2. Detect added drive letters (e.g. subst X: ... or USB plugged in)
            added_letters = current_letters - known_letters
            for letter in added_letters:
                info = self._detector.inspect_drive(letter)
                self._active_drives[letter] = info
                if info.is_ready:
                    insertions_to_notify.append(info)

            # 3. Check existing drives for media changes (optical CD-ROMs or unready drives becoming ready)
            retained_letters = current_letters & known_letters
            for letter in retained_letters:
                prev_info = self._active_drives[letter]

                # Optical CD-ROM disc insertion/ejection or disc swap
                if prev_info.drive_type == "CDROM" or prev_info.raw_type == DRIVE_CDROM:
                    current_info = self._detector.inspect_drive(letter, probe_retries=1)

                    if not prev_info.is_ready and current_info.is_ready:
                        # Disc inserted into empty drive
                        self._active_drives[letter] = current_info
                        insertions_to_notify.append(current_info)
                    elif prev_info.is_ready and not current_info.is_ready:
                        # Disc ejected
                        self._active_drives[letter] = current_info
                        removals_to_notify.append(letter)
                    elif prev_info.is_ready and current_info.is_ready:
                        # Check for media swap (different serial number or volume name)
                        if (
                            prev_info.serial_number != current_info.serial_number
                            or prev_info.volume_name != current_info.volume_name
                        ):
                            self._active_drives[letter] = current_info
                            removals_to_notify.append(letter)
                            insertions_to_notify.append(current_info)
                elif not prev_info.is_ready:
                    # Non-CDROM drive was previously not ready, check if it is ready now
                    current_info = self._detector.inspect_drive(letter, probe_retries=1)
                    if current_info.is_ready:
                        self._active_drives[letter] = current_info
                        insertions_to_notify.append(current_info)

        # Dispatch callbacks outside lock to prevent deadlock in user callbacks
        for letter in removals_to_notify:
            self._dispatch_removed(letter)

        for info in insertions_to_notify:
            self._dispatch_inserted(info)


class DriveSimulator:
    """
    Utility helpers for creating, managing, and tearing down virtual subst drives
    for automated tests and E2E simulation.
    """

    @staticmethod
    def find_available_drive_letter(
        preferred: Tuple[str, ...] = ("Z:", "Y:", "X:", "W:", "V:", "U:", "T:", "S:")
    ) -> Optional[str]:
        """
        Finds an unused drive letter on the current system.
        """
        detector = WindowsDriveDetector()
        mask = detector.get_logical_drives_mask()
        used = detector.get_drive_letters_from_mask(mask)
        for letter in preferred:
            clean = letter.strip().rstrip("\\/").rstrip(":").upper() + ":"
            if clean not in used:
                return clean
        # Fallback to checking from Z down to D
        for code in range(ord('Z'), ord('D') - 1, -1):
            letter = f"{chr(code)}:"
            if letter not in used:
                return letter
        return None

    @staticmethod
    def mount_subst(drive_letter: str, folder_path: str) -> bool:
        """
        Mounts folder_path as drive_letter using the Windows subst command.
        """
        if sys.platform != "win32":
            return False
        clean_letter = drive_letter.strip().rstrip("\\/").rstrip(":").upper() + ":"
        abs_folder = os.path.abspath(folder_path)
        os.makedirs(abs_folder, exist_ok=True)
        try:
            res = subprocess.run(
                ["subst", clean_letter, abs_folder],
                capture_output=True,
                text=True,
                check=False,
            )
            return res.returncode == 0
        except Exception as ex:
            logger.error("Error executing subst mount %s -> %s: %s", clean_letter, abs_folder, ex)
            return False

    @staticmethod
    def unmount_subst(drive_letter: str) -> bool:
        """
        Unmounts a subst drive using the Windows 'subst <letter> /d' command.
        """
        if sys.platform != "win32":
            return False
        clean_letter = drive_letter.strip().rstrip("\\/").rstrip(":").upper() + ":"
        try:
            res = subprocess.run(
                ["subst", clean_letter, "/d"],
                capture_output=True,
                text=True,
                check=False,
            )
            return res.returncode == 0
        except Exception as ex:
            logger.error("Error executing subst unmount for %s: %s", clean_letter, ex)
            return False

    @classmethod
    @contextmanager
    def temp_subst(
        cls, folder_path: str, drive_letter: Optional[str] = None
    ) -> Iterator[str]:
        """
        Context manager that temporarily mounts a directory as a subst drive
        and unmounts it on block exit.
        """
        target_letter = drive_letter or cls.find_available_drive_letter()
        if not target_letter:
            raise RuntimeError("No available drive letters found to create temporary subst mount.")

        mounted = cls.mount_subst(target_letter, folder_path)
        if not mounted:
            raise RuntimeError(f"Failed to mount subst drive {target_letter} -> {folder_path}")

        try:
            yield target_letter
        finally:
            cls.unmount_subst(target_letter)
