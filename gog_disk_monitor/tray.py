"""
gog_disk_monitor.tray
~~~~~~~~~~~~~~~~~~~~~

Windows System Tray Integration for GOG Game Disk Monitor.
Manages tray icon lifecycle (via pystray + Pillow), dynamic context menus
(status indicator, immediate drive scan, state folder access, installed
games listing, exit action), toast/balloon notifications, and graceful
headless/fallback mode.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger("gog_disk_monitor.tray")

# PIL / Pillow import handling with graceful fallback
try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore

# Pystray import handling with graceful fallback
try:
    import pystray
    HAS_PYSTRAY = True
except ImportError:
    HAS_PYSTRAY = False
    pystray = None  # type: ignore


def create_default_icon_image(
    size: Tuple[int, int] = (64, 64),
    bg_color: Tuple[int, int, int, int] = (37, 99, 235, 255),
    accent_color: Tuple[int, int, int, int] = (96, 165, 250, 255),
    hole_color: Tuple[int, int, int, int] = (15, 23, 42, 255),
) -> Any:
    """
    Generates a crisp, procedural vector-style optical disc icon in RGBA format.

    Args:
        size: Width and height tuple in pixels (default: 64x64).
        bg_color: Primary RGBA color tuple for the disc body.
        accent_color: Accent RGBA color tuple for disc track highlights.
        hole_color: RGBA color tuple for center spindle hole.

    Returns:
        PIL.Image.Image instance if PIL is available, otherwise None.
    """
    if not HAS_PIL or Image is None or ImageDraw is None:
        logger.debug("Pillow is not available; cannot generate default tray icon.")
        return None

    try:
        w, h = size
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # 1. Outer disc circle
        margin = max(2, w // 16)
        draw.ellipse(
            [margin, margin, w - margin, h - margin],
            fill=bg_color,
            outline=accent_color,
            width=max(1, w // 32),
        )

        # 2. Data track groove ring
        r_mid1 = int(w * 0.28)
        r_mid2 = int(w * 0.72)
        draw.ellipse(
            [r_mid1, r_mid1, r_mid2, r_mid2],
            outline=accent_color,
            width=max(1, w // 40),
        )

        # 3. Center transparent / dark spindle ring
        r_hole1 = int(w * 0.38)
        r_hole2 = int(w * 0.62)
        draw.ellipse(
            [r_hole1, r_hole1, r_hole2, r_hole2],
            fill=hole_color,
            outline=accent_color,
            width=max(1, w // 40),
        )

        # 4. Spindle center hole
        r_spindle1 = int(w * 0.44)
        r_spindle2 = int(w * 0.56)
        draw.ellipse(
            [r_spindle1, r_spindle1, r_spindle2, r_spindle2],
            fill=(0, 0, 0, 0),
        )

        return img
    except Exception as ex:
        logger.debug("Error creating procedural tray icon: %s", ex)
        return None


def load_tray_icon(
    icon_path: Optional[Union[str, Path]] = None,
    size: Tuple[int, int] = (64, 64),
) -> Any:
    """
    Loads an icon image from disk or falls back to the default procedural icon.

    Args:
        icon_path: Optional path to an .ico, .png, or .jpg file.
        size: Target icon dimensions in pixels.

    Returns:
        PIL.Image.Image instance if PIL is available, otherwise None.
    """
    if not HAS_PIL or Image is None:
        return None

    if icon_path:
        str_path = str(icon_path).strip()
        if str_path and os.path.isfile(str_path):
            try:
                with Image.open(str_path) as raw:
                    rgba = raw.convert("RGBA")
                    return rgba.resize(size, Image.Resampling.LANCZOS)
            except Exception as ex:
                logger.debug("Failed to load icon from '%s': %s. Using default.", str_path, ex)

    return create_default_icon_image(size=size)


class TrayManager:
    """
    Manages the Windows system tray icon, notification dispatches, and
    dynamic context menus for GOG Game Disk Monitor.

    Features:
        - Dynamic context menu with real-time status and installed games list
        - Balloon / Toast notification dispatching
        - Immediate manual drive scan triggering
        - Open State Folder shortcut in Windows Explorer
        - Full thread safety and non-blocking detached operation
        - Clean headless fallback for testing or non-GUI environments
    """

    def __init__(
        self,
        app_name: str = "GOG Game Disk Monitor",
        icon_path: Optional[Union[str, Path]] = None,
        on_scan_now: Optional[Callable[[], Any]] = None,
        on_open_state: Optional[Callable[[], Any]] = None,
        on_exit: Optional[Callable[[], Any]] = None,
        get_installed_games: Optional[Callable[[], Dict[str, Any]]] = None,
        on_launch_game: Optional[Callable[[str], Any]] = None,
        headless: bool = False,
    ) -> None:
        """
        Initializes the TrayManager.

        Args:
            app_name: Tooltip title displayed in the system tray.
            icon_path: Optional path to custom icon file.
            on_scan_now: Callback invoked when 'Scan Drives Now' menu item is clicked.
            on_open_state: Callback invoked when 'Open State Folder' menu item is clicked.
            on_exit: Callback invoked when 'Exit' menu item is clicked.
            get_installed_games: Callable returning dictionary of {game_id: record/title}.
            on_launch_game: Callback invoked when an installed game item is clicked.
            headless: If True, operates in headless mode without creating a system tray icon.
        """
        self.app_name = app_name
        self.icon_path = str(icon_path) if icon_path else None
        self.on_scan_now = on_scan_now
        self.on_open_state = on_open_state
        self.on_exit = on_exit
        self.get_installed_games = get_installed_games
        self.on_launch_game = on_launch_game
        self.headless = headless or not HAS_PYSTRAY or not HAS_PIL

        self._lock = threading.RLock()
        self._status_text: str = "Monitoring drives..."
        self._icon: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None
        self._running: bool = False
        self._icon_image: Optional[Any] = None

        if not self.headless:
            self._icon_image = load_tray_icon(self.icon_path)

    @property
    def is_headless(self) -> bool:
        """Returns True if the tray manager is operating in headless mode."""
        return self.headless

    def set_status(self, status: str) -> None:
        """
        Updates the status message displayed in tooltip and menu header.

        Args:
            status: New status string (e.g. 'Idle', 'Scanning drives...', 'Installing Witcher 3').
        """
        with self._lock:
            self._status_text = status.strip()
            if self._icon is not None and not self.headless:
                try:
                    self._icon.title = f"{self.app_name} — {self._status_text}"
                    self.update_menu()
                except Exception as ex:
                    logger.debug("Failed to update tray title/menu: %s", ex)

    def get_status(self) -> str:
        """Returns the current status string."""
        with self._lock:
            return self._status_text

    def _build_installed_games_menu_items(self) -> List[Any]:
        """Builds menu items for the Installed Games submenu."""
        if not HAS_PYSTRAY or pystray is None:
            return []

        games: Dict[str, Any] = {}
        if self.get_installed_games is not None:
            try:
                games = self.get_installed_games() or {}
            except Exception as ex:
                logger.debug("Error querying installed games for tray menu: %s", ex)

        if not games:
            return [
                pystray.MenuItem("No installed games found", None, enabled=False)
            ]

        items: List[Any] = []
        for game_id, rec in sorted(
            games.items(),
            key=lambda item: getattr(item[1], "title", str(item[1])).lower()
            if hasattr(item[1], "title") else str(item[0]).lower(),
        ):
            title = getattr(rec, "title", str(rec)) if hasattr(rec, "title") else str(rec)
            version = getattr(rec, "version", "") if hasattr(rec, "version") else ""
            label = f"{title} (v{version})" if version else title

            # Callback closure capturing game_id
            def make_launch_handler(gid: str) -> Callable[[Any, Any], None]:
                def handler(icon: Any, item: Any) -> None:
                    self._handle_launch_game(gid)
                return handler

            items.append(
                pystray.MenuItem(label, make_launch_handler(game_id))
            )

        return items

    def build_menu(self) -> Optional[Any]:
        """
        Constructs the dynamic context menu for the system tray icon.

        Returns:
            pystray.Menu instance if pystray is available, else None.
        """
        if not HAS_PYSTRAY or pystray is None:
            return None

        status_display = f"{self.app_name} ({self.get_status()})"

        installed_items = self._build_installed_games_menu_items()
        installed_submenu = pystray.Menu(*installed_items)

        menu_items = [
            # 1. Status header item (non-clickable)
            pystray.MenuItem(status_display, None, enabled=False),
            pystray.Menu.SEPARATOR,
            # 2. Manual drive scan
            pystray.MenuItem("Scan Drives Now", self._on_menu_scan_now),
            # 3. Installed Games submenu
            pystray.MenuItem("Installed Games", installed_submenu),
            # 4. Open State Storage folder
            pystray.MenuItem("Open State Folder", self._on_menu_open_state),
            pystray.Menu.SEPARATOR,
            # 5. Exit application
            pystray.MenuItem("Exit", self._on_menu_exit),
        ]

        return pystray.Menu(*menu_items)

    def update_menu(self) -> None:
        """Rebuilds and updates the system tray menu dynamically."""
        with self._lock:
            if self._icon is not None and not self.headless:
                try:
                    menu = self.build_menu()
                    if menu is not None:
                        self._icon.menu = menu
                except Exception as ex:
                    logger.debug("Error updating tray menu: %s", ex)

    def notify(
        self,
        title: str,
        message: str,
        icon_path: Optional[Union[str, Path]] = None,
    ) -> bool:
        """
        Displays a Windows balloon or toast notification from the system tray.

        Args:
            title: Notification header title.
            message: Notification body text.
            icon_path: Optional custom icon file for notification.

        Returns:
            True if notification was dispatched, False otherwise.
        """
        logger.info("[Tray Notification] %s: %s", title, message)

        if self.headless or self._icon is None:
            return False

        try:
            # pystray notify takes (message, title=None) or (title, message)
            if hasattr(self._icon, "notify"):
                try:
                    self._icon.notify(message, title)
                    return True
                except TypeError:
                    self._icon.notify(title, message)
                    return True
        except Exception as ex:
            logger.debug("Failed to dispatch tray notification: %s", ex)

        return False

    def _on_menu_scan_now(self, icon: Any = None, item: Any = None) -> None:
        """Internal callback for 'Scan Drives Now' menu item."""
        logger.info("Tray menu: Scan Drives Now requested.")
        if self.on_scan_now is not None:
            try:
                self.on_scan_now()
            except Exception as ex:
                logger.exception("Error executing on_scan_now callback: %s", ex)

    def _on_menu_open_state(self, icon: Any = None, item: Any = None) -> None:
        """Internal callback for 'Open State Folder' menu item."""
        logger.info("Tray menu: Open State Folder requested.")
        if self.on_open_state is not None:
            try:
                self.on_open_state()
            except Exception as ex:
                logger.exception("Error executing on_open_state callback: %s", ex)
        else:
            self.open_state_folder()

    def _on_menu_exit(self, icon: Any = None, item: Any = None) -> None:
        """Internal callback for 'Exit' menu item."""
        logger.info("Tray menu: Exit requested.")
        self.stop()

    def _handle_launch_game(self, game_id: str) -> None:
        """Internal callback when an installed game is selected from the menu."""
        logger.info("Tray menu: Launch requested for game '%s'", game_id)
        if self.on_launch_game is not None:
            try:
                self.on_launch_game(game_id)
            except Exception as ex:
                logger.exception("Error in on_launch_game callback for '%s': %s", game_id, ex)

    @staticmethod
    def open_state_folder(folder_path: Optional[Union[str, Path]] = None) -> bool:
        """
        Opens a folder in Windows Explorer or the native file browser.

        Args:
            folder_path: Target folder path. If None, resolves to default %APPDATA% directory.

        Returns:
            True if folder opened successfully, False otherwise.
        """
        if folder_path is None:
            appdata = os.environ.get("APPDATA")
            if appdata:
                target_dir = Path(appdata) / "GOGDiskMonitor"
            else:
                target_dir = Path.home() / ".gog_disk_monitor"
        else:
            target_dir = Path(folder_path)

        target_dir.mkdir(parents=True, exist_ok=True)
        abs_str = str(target_dir.resolve())

        try:
            if sys.platform == "win32":
                os.startfile(abs_str)  # type: ignore[attr-defined]
                return True
            elif sys.platform == "darwin":
                subprocess.Popen(["open", abs_str])
                return True
            else:
                subprocess.Popen(["xdg-open", abs_str])
                return True
        except Exception as ex:
            logger.warning("Failed to open state folder '%s': %s", abs_str, ex)
            return False

    def is_running(self) -> bool:
        """Returns True if the system tray icon is actively running."""
        with self._lock:
            return self._running

    def start(
        self,
        setup: Optional[Callable[[Any], None]] = None,
        detached: bool = True,
    ) -> None:
        """
        Starts the system tray icon loop.

        Args:
            setup: Optional initialization callback invoked when tray icon becomes visible.
            detached: If True, runs the icon loop in a background daemon thread.
        """
        with self._lock:
            if self._running:
                logger.debug("TrayManager is already running.")
                return

            self._running = True

            if self.headless or not HAS_PYSTRAY or pystray is None or self._icon_image is None:
                logger.info("TrayManager running in headless / fallback mode.")
                if setup is not None:
                    try:
                        setup(None)
                    except Exception as ex:
                        logger.debug("Error in headless setup callback: %s", ex)
                return

            menu = self.build_menu()
            self._icon = pystray.Icon(
                name="gog_disk_monitor",
                icon=self._icon_image,
                title=f"{self.app_name} — {self._status_text}",
                menu=menu,
            )

            if detached:
                self._thread = threading.Thread(
                    target=self._run_icon_loop,
                    args=(setup,),
                    name="TrayManagerThread",
                    daemon=True,
                )
                self._thread.start()
                logger.info("TrayManager started detached in background thread.")
            else:
                self._run_icon_loop(setup)

    def _run_icon_loop(self, setup: Optional[Callable[[Any], None]] = None) -> None:
        """Internal execution loop for pystray Icon."""
        try:
            if self._icon is not None:
                self._icon.run(setup=setup)
        except Exception as ex:
            logger.warning("Pystray icon loop encountered exception: %s. Falling back to headless.", ex)
            self.headless = True
        finally:
            with self._lock:
                self._running = False
                self._icon = None

    def run(self, setup: Optional[Callable[[Any], None]] = None) -> None:
        """Runs the tray icon loop blocking the current thread."""
        self.start(setup=setup, detached=False)

    def stop(self) -> None:
        """Stops the system tray icon and triggers on_exit callback."""
        with self._lock:
            if not self._running and self._icon is None:
                return

            self._running = False
            icon = self._icon
            self._icon = None

        if icon is not None:
            try:
                icon.stop()
            except Exception as ex:
                logger.debug("Error stopping pystray icon: %s", ex)

        if self._thread and self._thread.is_alive() and self._thread != threading.current_thread():
            self._thread.join(timeout=2.0)
            self._thread = None

        logger.info("TrayManager stopped.")

        if self.on_exit is not None:
            try:
                self.on_exit()
            except Exception as ex:
                logger.debug("Error in on_exit callback: %s", ex)
