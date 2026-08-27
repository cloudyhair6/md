"""
gog_disk_monitor.app
~~~~~~~~~~~~~~~~~~~~

Main Application Coordinator for GOG Game Disk Monitor.
Orchestrates DriveMonitor, StateStore, GOG disk configuration parsing,
icon discovery, user installation prompt dialogs, process execution,
and system tray icon lifecycle.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from .config import (
    GOGDiskConfig,
    find_disk_icon,
    normalize_disk_root,
    parse_disk_config,
)
from .drive_monitor import (
    DriveInfo,
    DriveMonitor,
)
from .launcher import (
    ProcessExecutionError,
    ProcessRunner,
)
from .prompt_dialog import (
    InstallPromptDialog,
)
from .state import (
    InstalledGameRecord,
    StateStore,
)
from .tray import (
    TrayManager,
)

logger = logging.getLogger("gog_disk_monitor.app")


class GOGDiskMonitorApp:
    """
    Application Coordinator that ties together background drive monitoring,
    local PC state tracking, installation prompting, setup execution,
    automatic game launching, and the Windows system tray interface.
    """

    def __init__(
        self,
        state_file_path: Optional[Union[str, Path]] = None,
        poll_interval: float = 0.5,
        auto_confirm: Optional[bool] = None,
        headless: bool = False,
        scan_on_startup: bool = False,
        install_root: Optional[Union[str, Path]] = None,
        drive_monitor: Optional[DriveMonitor] = None,
        state_store: Optional[StateStore] = None,
        tray_manager: Optional[TrayManager] = None,
    ) -> None:
        """
        Initializes the GOG Game Disk Monitor Application.

        Args:
            state_file_path: Custom path to installed_games.json (or None for default %APPDATA%).
            poll_interval: Drive bitmask polling interval in seconds (default: 0.5s).
            auto_confirm: Explicit test override for installation prompts (True=install, False=cancel, None=GUI).
            headless: If True, disables system tray icon and GUI prompts.
            scan_on_startup: If True, scans existing mounted drives immediately when monitoring begins.
            install_root: Base destination folder for simulated or standard game installations.
            drive_monitor: Optional injected DriveMonitor instance (for testing).
            state_store: Optional injected StateStore instance (for testing).
            tray_manager: Optional injected TrayManager instance (for testing).
        """
        self.poll_interval = poll_interval
        self.auto_confirm = auto_confirm
        self.headless = headless
        self.scan_on_startup = scan_on_startup
        self.install_root = str(Path(install_root).resolve()) if install_root else None

        self._lock = threading.RLock()
        self._shutdown_event = threading.Event()
        self._is_running = False

        # 1. State Store
        if state_store is not None:
            self.state_store = state_store
        else:
            self.state_store = StateStore(state_file_path=state_file_path)

        # 2. Tray Manager
        if tray_manager is not None:
            self.tray = tray_manager
        else:
            self.tray = TrayManager(
                app_name="GOG Game Disk Monitor",
                on_scan_now=self.scan_now,
                on_open_state=self.open_state_folder,
                on_exit=self.stop,
                get_installed_games=self.state_store.get_all_installed,
                on_launch_game=self.launch_game_by_id,
                headless=headless,
            )

        # 3. Drive Monitor
        if drive_monitor is not None:
            self.drive_monitor = drive_monitor
            self.drive_monitor.add_on_inserted_callback(self.handle_drive_inserted)
            self.drive_monitor.add_on_removed_callback(self.handle_drive_removed)
        else:
            self.drive_monitor = DriveMonitor(
                poll_interval=self.poll_interval,
                on_inserted=self.handle_drive_inserted,
                on_removed=self.handle_drive_removed,
                scan_existing_at_startup=False,
            )

    @property
    def is_running(self) -> bool:
        """Returns True if the application background services are active."""
        with self._lock:
            return self._is_running

    def _resolve_install_destination(self, config: GOGDiskConfig) -> str:
        """
        Determines the target installation folder on the PC for a game.

        Priority:
            1. Explicit `self.install_root` combined with `config.setup.default_install_subdir` or `config.game_id`.
            2. Absolute path if `config.setup.default_install_subdir` is absolute.
            3. Common GOG installation directories (C:\\GOG Games, %ProgramFiles%\\GOG Games).
            4. Current working directory fallback.
        """
        subdir = config.setup.default_install_subdir or config.game_id

        if self.install_root:
            target = os.path.join(self.install_root, subdir)
            return os.path.abspath(target)

        if os.path.isabs(subdir):
            return os.path.abspath(subdir)

        # Check standard candidate installation locations
        candidates: List[str] = []
        appdata = os.environ.get("APPDATA")
        prog_files = os.environ.get("ProgramFiles", "C:\\Program Files")
        prog_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")

        candidates.append(os.path.join(prog_files, "GOG Games", subdir))
        candidates.append(os.path.join(prog_files_x86, "GOG Galaxy", "Games", subdir))
        candidates.append(os.path.join("C:\\GOG Games", subdir))
        candidates.append(os.path.join(os.getcwd(), subdir))

        for candidate in candidates:
            # If folder already exists or contains the launcher executable
            if os.path.isdir(candidate):
                return os.path.abspath(candidate)
            launcher_candidate = os.path.join(candidate, config.launcher.executable)
            if os.path.isfile(launcher_candidate):
                return os.path.abspath(candidate)

        # Default fallback to standard C:\GOG Games\<subdir> or cwd
        if sys.platform == "win32":
            return os.path.abspath(os.path.join("C:\\GOG Games", subdir))
        return os.path.abspath(os.path.join(os.getcwd(), subdir))

    def _resolve_game_executable_path(
        self,
        config: GOGDiskConfig,
        install_dir: str,
    ) -> str:
        """
        Resolves the full path to the installed game executable on the PC.
        """
        if os.path.isabs(config.launcher.executable):
            return os.path.abspath(config.launcher.executable)

        return os.path.normpath(os.path.join(install_dir, config.launcher.executable))

    def handle_drive_inserted(self, drive_info: DriveInfo) -> Dict[str, Any]:
        """
        Main event pipeline executed when a logical drive is mounted or becomes ready.

        Pipeline:
          1. Inspects drive root for valid GOG game descriptor (`gog_game.json` / `gog_disk.json`).
          2. Resolves custom game icon (.ico/.png) from disk.
          3. Checks `StateStore` to determine if game is already installed.
          4. If NOT installed:
             - Shows `InstallPromptDialog` (or evaluates auto_confirm).
             - If confirmed, runs setup executable via `ProcessRunner.run_setup`.
             - On success (exit 0), registers `InstalledGameRecord` in `StateStore` and updates tray.
          5. If INSTALLED:
             - Updates launch timestamp and disk metadata in `StateStore`.
             - Automatically launches game binary detached via `ProcessRunner.launch_game`.

        Args:
            drive_info: DriveInfo instance describing the inserted drive.

        Returns:
            Dictionary detailing the action taken and metadata.
        """
        with self._lock:
            drive_root = drive_info.root_path or f"{drive_info.letter}\\"
            logger.info("Handling drive insertion event for %s (Type: %s)", drive_info.letter, drive_info.drive_type)

            # 1. Check readiness
            if not drive_info.is_ready:
                logger.debug("Drive %s is not ready. Skipping.", drive_info.letter)
                return {"action": "ignored", "reason": "not_ready", "letter": drive_info.letter}

            # 2. Parse GOG disk configuration
            config = parse_disk_config(drive_root)
            if config is None:
                logger.debug("No valid GOG game descriptor found on drive %s.", drive_info.letter)
                return {"action": "ignored", "reason": "no_gog_config", "letter": drive_info.letter}

            logger.info("Found GOG game '%s' (ID: %s, v%s) on %s", config.title, config.game_id, config.version, drive_info.letter)

            # 3. Resolve custom game icon
            icon_path = find_disk_icon(drive_root, config)

            # 4. Check installation state
            is_installed = self.state_store.is_installed(config.game_id, verify_executable=True)

            if not is_installed:
                # -------------------------------------------------------------
                # BRANCH A: GAME NOT INSTALLED -> PROMPT & INSTALL
                # -------------------------------------------------------------
                logger.info("Game '%s' is not installed on this PC. Presenting installation prompt.", config.title)
                self.tray.set_status(f"Found disc: {config.title}")

                # Show Prompt Dialog
                confirmed = InstallPromptDialog.show_prompt(
                    config=config,
                    icon_path=icon_path,
                    drive_letter=drive_info.letter,
                    auto_confirm=self.auto_confirm,
                )

                if not confirmed:
                    logger.info("User declined installation of '%s'.", config.title)
                    self.tray.set_status("Installation cancelled")
                    return {
                        "action": "prompt_cancelled",
                        "game_id": config.game_id,
                        "title": config.title,
                        "letter": drive_info.letter,
                    }

                # User Accepted -> Run Setup
                self.tray.set_status(f"Installing {config.title}...")
                self.tray.notify(
                    "Starting Installation",
                    f"Installing {config.title} from {drive_info.letter}...",
                    icon_path=icon_path,
                )

                setup_exe = os.path.normpath(os.path.join(drive_root, config.setup.executable))
                setup_args = list(config.setup.arguments)
                setup_cwd = drive_root

                try:
                    exit_code = ProcessRunner.run_setup(
                        setup_exe_path=setup_exe,
                        args=setup_args,
                        cwd=setup_cwd,
                    )
                except Exception as ex:
                    logger.exception("Error executing setup '%s': %s", setup_exe, ex)
                    self.tray.set_status(f"Setup error: {config.title}")
                    self.tray.notify(
                        "Installation Failed",
                        f"An error occurred while running setup for {config.title}: {ex}",
                    )
                    return {
                        "action": "setup_failed",
                        "game_id": config.game_id,
                        "title": config.title,
                        "error": str(ex),
                        "letter": drive_info.letter,
                    }

                if exit_code != 0:
                    logger.error("Setup for '%s' returned non-zero exit code %d.", config.title, exit_code)
                    self.tray.set_status(f"Setup failed (Code {exit_code})")
                    self.tray.notify(
                        "Installation Failed",
                        f"Setup for {config.title} failed (exit code {exit_code}).",
                    )
                    return {
                        "action": "setup_failed",
                        "game_id": config.game_id,
                        "title": config.title,
                        "exit_code": exit_code,
                        "letter": drive_info.letter,
                    }

                # Setup Successful -> Resolve Paths and Commit to State Store
                install_dir = self._resolve_install_destination(config)
                executable_path = self._resolve_game_executable_path(config, install_dir)

                record = InstalledGameRecord(
                    game_id=config.game_id,
                    title=config.title,
                    version=config.version,
                    install_path=install_dir,
                    executable_path=executable_path,
                    last_disk_drive=drive_info.letter,
                    last_disk_label=drive_info.volume_name,
                    custom_args=config.launcher.arguments,
                )

                self.state_store.mark_installed(record)
                self.tray.update_menu()
                self.tray.set_status(f"Installed: {config.title}")
                self.tray.notify(
                    "Installation Complete",
                    f"{config.title} has been installed and is ready to play!",
                    icon_path=icon_path,
                )

                logger.info("Successfully recorded installation of '%s' at '%s'", config.title, install_dir)
                return {
                    "action": "installed",
                    "game_id": config.game_id,
                    "title": config.title,
                    "record": record,
                    "install_path": install_dir,
                    "executable_path": executable_path,
                    "letter": drive_info.letter,
                }

            else:
                # -------------------------------------------------------------
                # BRANCH B: GAME ALREADY INSTALLED -> AUTO-LAUNCH DETACHED
                # -------------------------------------------------------------
                record = self.state_store.get_game(config.game_id)
                logger.info("Game '%s' is already installed on this PC. Auto-launching game executable.", config.title)

                # Resolve executable
                if record and record.executable_path:
                    game_exe = record.executable_path
                else:
                    install_dir = record.install_path if record else self._resolve_install_destination(config)
                    game_exe = self._resolve_game_executable_path(config, install_dir)

                # Resolve working directory
                working_dir: Optional[str] = None
                if config.launcher.working_directory:
                    base = record.install_path if record else os.path.dirname(game_exe)
                    working_dir = os.path.normpath(os.path.join(base, config.launcher.working_directory))
                elif os.path.isfile(game_exe):
                    working_dir = os.path.dirname(game_exe)

                # Update launch time in state store
                self.state_store.update_launch_time(
                    config.game_id,
                    drive_letter=drive_info.letter,
                    disk_label=drive_info.volume_name,
                )

                # Notify via Tray
                self.tray.set_status(f"Launching {config.title}...")
                self.tray.notify(
                    "Launching Game",
                    f"Launching {config.title} from {drive_info.letter}...",
                    icon_path=icon_path,
                )

                try:
                    proc = ProcessRunner.launch_game(
                        game_exe_path=game_exe,
                        args=config.launcher.arguments,
                        cwd=working_dir,
                        detached=True,
                    )
                    pid = getattr(proc, "pid", None)
                    logger.info("Launched game '%s' (PID: %s)", config.title, pid)
                    self.tray.set_status(f"Running: {config.title}")

                    return {
                        "action": "launched",
                        "game_id": config.game_id,
                        "title": config.title,
                        "pid": pid,
                        "executable_path": game_exe,
                        "letter": drive_info.letter,
                    }
                except Exception as ex:
                    logger.exception("Failed to launch game executable '%s': %s", game_exe, ex)
                    self.tray.set_status(f"Launch failed: {config.title}")
                    self.tray.notify(
                        "Launch Failed",
                        f"Could not launch {config.title}: {ex}",
                    )
                    return {
                        "action": "launch_failed",
                        "game_id": config.game_id,
                        "title": config.title,
                        "error": str(ex),
                        "letter": drive_info.letter,
                    }

    def handle_drive_removed(self, drive_letter: str) -> None:
        """
        Event handler executed when a logical drive is unmounted or ejected.

        Args:
            drive_letter: Drive letter string, e.g. 'X:'
        """
        with self._lock:
            logger.info("Logical drive %s removed / unmounted.", drive_letter)
            self.tray.set_status("Monitoring drives...")

    def launch_game_by_id(self, game_id: str) -> Optional[int]:
        """
        Directly launches an installed game from the state store by game ID.

        Args:
            game_id: Unique identifier for the installed game.

        Returns:
            PID of launched process if successful, None otherwise.
        """
        record = self.state_store.get_game(game_id)
        if not record or not record.is_valid_installation():
            logger.warning("Cannot launch game '%s': not found or executable missing.", game_id)
            self.tray.notify("Launch Error", f"Game '{game_id}' is not installed or executable is missing.")
            return None

        try:
            cwd = os.path.dirname(record.executable_path) if os.path.isfile(record.executable_path) else None
            proc = ProcessRunner.launch_game(
                game_exe_path=record.executable_path,
                args=record.custom_args,
                cwd=cwd,
                detached=True,
            )
            self.state_store.update_launch_time(game_id)
            pid = getattr(proc, "pid", None)
            logger.info("Launched '%s' by ID (PID: %s)", record.title, pid)
            self.tray.notify("Game Launched", f"{record.title} has started.")
            return pid
        except Exception as ex:
            logger.exception("Failed to launch game '%s': %s", game_id, ex)
            return None

    def scan_now(self) -> List[Dict[str, Any]]:
        """
        Performs an immediate synchronous scan of all logical drives and
        processes any detected GOG game discs.

        Returns:
            List of result dictionaries from handle_drive_inserted.
        """
        logger.info("Manual drive scan initiated.")
        self.tray.set_status("Scanning drives...")
        results: List[Dict[str, Any]] = []

        drives = self.drive_monitor.scan_now()
        for drive_info in drives:
            if drive_info.is_candidate_for_game_disk and drive_info.is_ready:
                res = self.handle_drive_inserted(drive_info)
                results.append(res)

        self.tray.set_status("Monitoring drives...")
        return results

    def open_state_folder(self) -> bool:
        """Opens the state storage directory in the file browser."""
        state_dir = self.state_store.state_file_path.parent
        return TrayManager.open_state_folder(state_dir)

    def start(self, block: bool = False) -> None:
        """
        Starts the application background monitoring engine and system tray icon.

        Args:
            block: If True, blocks the calling thread until stop() is called.
        """
        with self._lock:
            if self._is_running:
                logger.debug("GOGDiskMonitorApp is already running.")
                return

            self._is_running = True
            self._shutdown_event.clear()

            logger.info("Starting GOG Disk Monitor App (poll_interval=%.2fs, headless=%s)", self.poll_interval, self.headless)

            # Start drive monitoring thread
            self.drive_monitor.start(scan_existing_at_startup=self.scan_on_startup)

            # Start system tray
            self.tray.start(detached=not block)

        if block:
            try:
                while not self._shutdown_event.is_set():
                    self._shutdown_event.wait(timeout=0.5)
            except (KeyboardInterrupt, SystemExit):
                logger.info("Shutdown signal received.")
            finally:
                self.stop()

    def stop(self) -> None:
        """Stops all application threads, monitoring loops, and the system tray icon."""
        with self._lock:
            if not self._is_running:
                return

            self._is_running = False
            self._shutdown_event.set()

            logger.info("Stopping GOG Disk Monitor App...")
            self.drive_monitor.stop()
            self.tray.stop()
            logger.info("GOG Disk Monitor App stopped cleanly.")
