"""
gog_disk_monitor.state
~~~~~~~~~~~~~~~~~~~~~~

Persistent local PC state storage for GOG Game Disk Monitor.
Tracks installed games, installation paths, launcher executables,
and launch timestamps. Implements atomic persistence and self-healing corruption recovery.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Any, Dict, List, Optional, Union
import uuid

logger = logging.getLogger("gog_disk_monitor.state")


def _get_utc_now_iso() -> str:
    """Returns current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class InstalledGameRecord:
    """
    Represents an installed game tracked by GOG Disk Monitor.

    Attributes:
        game_id: Unique identifier for the game (e.g. 'witcher_3').
        title: Human-readable game title.
        version: Version string of the game release.
        install_path: Root folder where the game was installed on PC.
        executable_path: Full path to game launcher binary on PC.
        installed_at: ISO 8601 UTC timestamp when installation completed.
        last_launched_at: ISO 8601 UTC timestamp of last launch (or None).
        last_disk_drive: Drive letter of the disk when last inserted/launched.
        last_disk_label: Volume label of the disk when last inserted/launched.
        status: Installation status ('installed', 'broken_missing_exe', 'uninstalled').
        custom_args: Optional list of custom command-line launch arguments.
    """
    game_id: str
    title: str
    version: str = "1.0.0"
    install_path: str = ""
    executable_path: str = ""
    installed_at: str = field(default_factory=_get_utc_now_iso)
    last_launched_at: Optional[str] = None
    last_disk_drive: Optional[str] = None
    last_disk_label: Optional[str] = None
    status: str = "installed"
    custom_args: List[str] = field(default_factory=list)

    def __init__(
        self,
        game_id: str,
        title: str,
        version: str = "1.0.0",
        install_path: Optional[str] = None,
        installed_path: Optional[str] = None,
        executable_path: str = "",
        installed_at: Optional[str] = None,
        last_launched_at: Optional[str] = None,
        last_disk_drive: Optional[str] = None,
        last_disk_label: Optional[str] = None,
        disk_label: Optional[str] = None,
        status: str = "installed",
        custom_args: Optional[List[str]] = None,
    ) -> None:
        self.game_id = str(game_id)
        self.title = str(title)
        self.version = str(version)
        # Support both install_path and installed_path
        if install_path is not None:
            self.install_path = str(install_path)
        elif installed_path is not None:
            self.install_path = str(installed_path)
        else:
            self.install_path = ""

        self.executable_path = str(executable_path)
        self.installed_at = str(installed_at or _get_utc_now_iso())
        self.last_launched_at = last_launched_at
        self.last_disk_drive = last_disk_drive
        # Support both last_disk_label and disk_label
        if last_disk_label is not None:
            self.last_disk_label = last_disk_label
        elif disk_label is not None:
            self.last_disk_label = disk_label
        else:
            self.last_disk_label = None

        self.status = str(status)
        self.custom_args = list(custom_args) if custom_args is not None else []

    @property
    def installed_path(self) -> str:
        """Alias for install_path for interface compatibility."""
        return self.install_path

    @installed_path.setter
    def installed_path(self, value: str) -> None:
        self.install_path = value

    @property
    def disk_label(self) -> Optional[str]:
        """Alias for last_disk_label for interface compatibility."""
        return self.last_disk_label

    @disk_label.setter
    def disk_label(self, value: Optional[str]) -> None:
        self.last_disk_label = value

    def is_valid_installation(self) -> bool:
        """Checks if the recorded launcher executable exists on the filesystem."""
        if not self.executable_path:
            return False
        return Path(self.executable_path).is_file()

    def to_dict(self) -> Dict[str, Any]:
        """Serializes the record to a standard dictionary."""
        return {
            "game_id": self.game_id,
            "title": self.title,
            "version": self.version,
            "install_path": self.install_path,
            "installed_path": self.install_path,
            "executable_path": self.executable_path,
            "installed_at": self.installed_at,
            "last_launched_at": self.last_launched_at,
            "last_disk_drive": self.last_disk_drive,
            "last_disk_label": self.last_disk_label,
            "disk_label": self.last_disk_label,
            "status": self.status,
            "custom_args": list(self.custom_args),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InstalledGameRecord":
        """
        Constructs an InstalledGameRecord from a dictionary.
        Accepts aliased field names (install_path / installed_path, disk_label / last_disk_label).
        """
        if not isinstance(data, dict):
            raise TypeError(f"Expected dict, got {type(data).__name__}")

        game_id = str(data.get("game_id", "")).strip()
        if not game_id:
            raise ValueError("InstalledGameRecord requires a non-empty 'game_id'.")

        install_path = data.get("install_path") or data.get("installed_path", "")
        disk_label = data.get("last_disk_label") or data.get("disk_label")

        return cls(
            game_id=game_id,
            title=str(data.get("title", game_id)),
            version=str(data.get("version", "1.0.0")),
            install_path=str(install_path),
            executable_path=str(data.get("executable_path", "")),
            installed_at=str(data.get("installed_at") or _get_utc_now_iso()),
            last_launched_at=data.get("last_launched_at"),
            last_disk_drive=data.get("last_disk_drive"),
            last_disk_label=disk_label,
            status=str(data.get("status", "installed")),
            custom_args=list(data.get("custom_args", [])),
        )


class StateStore:
    """
    Thread-safe, atomic persistent store for installed game records.
    Defaults to %APPDATA%/GOGDiskMonitor/installed_games.json.
    """

    SCHEMA_VERSION = "1.0"

    def __init__(
        self,
        state_file_path: Optional[Union[str, Path]] = None,
        auto_load: bool = True,
    ) -> None:
        """
        Initializes the StateStore.

        Args:
            state_file_path: Optional path to installed_games.json.
                             If None, resolves to default %APPDATA% location.
            auto_load: If True, immediately loads existing state from disk.
        """
        self._lock = threading.RLock()
        self._games: Dict[str, InstalledGameRecord] = {}

        if state_file_path is not None:
            self._state_file_path = Path(state_file_path).resolve()
        else:
            self._state_file_path = self._resolve_default_path()

        if auto_load:
            self.load()

    @property
    def state_file_path(self) -> Path:
        """Returns the resolved Path of the state file."""
        return self._state_file_path

    @staticmethod
    def _resolve_default_path() -> Path:
        """Resolves the default state storage path based on environment."""
        appdata = os.environ.get("APPDATA")
        if appdata and appdata.strip():
            base_dir = Path(appdata) / "GOGDiskMonitor"
        else:
            try:
                base_dir = Path.home() / ".gog_disk_monitor"
            except (RuntimeError, Exception):
                base_dir = Path.cwd() / ".gog_disk_monitor"
        return (base_dir / "installed_games.json").resolve()

    def get_state_file_path(self) -> Path:
        """Public getter for state file path."""
        return self._state_file_path

    def load(self) -> None:
        """
        Loads the installed games state from disk.
        If the file is corrupted, backs it up and initializes a clean store.
        """
        with self._lock:
            if not self._state_file_path.is_file():
                logger.debug("State file '%s' does not exist. Initializing empty store.", self._state_file_path)
                self._games = {}
                return

            try:
                with open(self._state_file_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()

                if not content:
                    logger.warning("State file '%s' is empty. Initializing empty store.", self._state_file_path)
                    self._games = {}
                    return

                data = json.loads(content)
                if not isinstance(data, dict):
                    raise ValueError(f"State root must be a JSON object, got {type(data).__name__}")

                games_dict = data.get("games", {})
                if not isinstance(games_dict, dict):
                    raise ValueError(f"'games' field must be a JSON object, got {type(games_dict).__name__}")

                loaded_games: Dict[str, InstalledGameRecord] = {}
                for gid, rec_data in games_dict.items():
                    if isinstance(rec_data, dict):
                        if "game_id" not in rec_data:
                            rec_data["game_id"] = gid
                        record = InstalledGameRecord.from_dict(rec_data)
                        loaded_games[record.game_id] = record

                self._games = loaded_games
                logger.info("Loaded %d installed game record(s) from '%s'.", len(self._games), self._state_file_path)

            except Exception as exc:
                logger.error("Failed to parse state file '%s': %s. Initiating recovery.", self._state_file_path, exc)
                self._handle_corrupted_file(exc)

    def _handle_corrupted_file(self, error: Exception) -> None:
        """Backs up corrupted state file and resets to clean state."""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            unique_id = uuid.uuid4().hex[:6]
            corrupt_backup = self._state_file_path.parent / f"{self._state_file_path.name}.corrupt.{timestamp}_{unique_id}.bak"

            if self._state_file_path.exists():
                shutil.copy2(self._state_file_path, corrupt_backup)
                logger.warning("Corrupted state file backed up to '%s'.", corrupt_backup)

            self._games = {}
            self.save()
            logger.info("Initialized fresh state store at '%s'.", self._state_file_path)
        except Exception as recovery_exc:
            logger.critical("Error during corruption recovery: %s", recovery_exc)
            self._games = {}

    def save(self) -> None:
        """
        Atomically saves the current in-memory state to disk.
        Writes to a temporary file in the same directory, syncs, then renames.
        """
        with self._lock:
            parent_dir = self._state_file_path.parent
            parent_dir.mkdir(parents=True, exist_ok=True)

            payload = {
                "schema_version": self.SCHEMA_VERSION,
                "updated_at": _get_utc_now_iso(),
                "games": {
                    gid: rec.to_dict() for gid, rec in self._games.items()
                },
            }

            temp_filename = f"{self._state_file_path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
            temp_path = parent_dir / temp_filename

            try:
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())

                # On Windows, os.replace might rarely hit temporary file locks, retry if needed
                for attempt in range(5):
                    try:
                        os.replace(temp_path, self._state_file_path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.02)

                logger.debug("Successfully saved %d game(s) to '%s'.", len(self._games), self._state_file_path)
            except Exception as exc:
                logger.error("Failed to save state file to '%s': %s", self._state_file_path, exc)
                raise
            finally:
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass

    def is_installed(self, game_id: str, verify_executable: bool = False) -> bool:
        """
        Checks if a game is installed.

        Args:
            game_id: Unique game identifier.
            verify_executable: If True, checks physical existence of the executable.
                               If missing, marks status as 'broken_missing_exe' and returns False.
        """
        with self._lock:
            record = self._games.get(game_id)
            if not record or record.status != "installed":
                return False

            if verify_executable and not record.is_valid_installation():
                logger.warning("Game '%s' marked installed but executable missing: %s", game_id, record.executable_path)
                record.status = "broken_missing_exe"
                self.save()
                return False

            return True

    def get_game(self, game_id: str) -> Optional[InstalledGameRecord]:
        """Returns the InstalledGameRecord for the given game_id, or None if not found."""
        with self._lock:
            return self._games.get(game_id)

    def mark_installed(self, record: InstalledGameRecord) -> None:
        """
        Records or updates an installed game and commits to disk atomically.
        """
        with self._lock:
            record.status = "installed"
            self._games[record.game_id] = record
            self.save()
            logger.info("Marked game '%s' (%s) as installed.", record.game_id, record.title)

    def unmark_installed(self, game_id: str) -> bool:
        """
        Removes a game from the installed store and commits to disk.
        Returns True if the game was removed, False if it was not found.
        """
        with self._lock:
            if game_id in self._games:
                del self._games[game_id]
                self.save()
                logger.info("Unmarked game '%s' from state store.", game_id)
                return True
            return False

    def get_all_installed(self) -> Dict[str, InstalledGameRecord]:
        """Returns a copy of all installed game records."""
        with self._lock:
            return {
                gid: rec for gid, rec in self._games.items()
                if rec.status == "installed"
            }

    def update_launch_time(
        self,
        game_id: str,
        drive_letter: Optional[str] = None,
        disk_label: Optional[str] = None,
    ) -> bool:
        """
        Updates the last_launched_at timestamp and disk info for an installed game.
        """
        with self._lock:
            record = self._games.get(game_id)
            if not record:
                return False

            record.last_launched_at = _get_utc_now_iso()
            if drive_letter:
                record.last_disk_drive = drive_letter
            if disk_label:
                record.last_disk_label = disk_label
            self.save()
            return True

    def update_status(self, game_id: str, status: str) -> bool:
        """Updates the status string for a given game_id."""
        with self._lock:
            record = self._games.get(game_id)
            if not record:
                return False
            record.status = status
            self.save()
            return True

    def clear(self) -> None:
        """Clears all records and commits empty state to disk."""
        with self._lock:
            self._games = {}
            self.save()
