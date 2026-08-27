"""
gog_disk_monitor.prompt_dialog
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Installation prompt modal dialog for GOG Game Disk Monitor.
Displays high-DPI aware Tkinter dialog with game icon, metadata, drive letter,
and Install/Cancel buttons with keyboard navigation and automated test overrides.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import sys
from typing import Any, Dict, Optional, Union

# Import GOGDiskConfig for type annotations
try:
    from .config import GOGDiskConfig
except ImportError:
    GOGDiskConfig = Any  # type: ignore

logger = logging.getLogger("gog_disk_monitor.prompt_dialog")

# PIL / Pillow import handling with graceful fallback
try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    Image = None  # type: ignore
    ImageTk = None  # type: ignore

# Tkinter import handling
try:
    import tkinter as tk
    from tkinter import ttk
    HAS_TKINTER = True
except ImportError:
    HAS_TKINTER = False
    tk = None  # type: ignore
    ttk = None  # type: ignore


def enable_windows_dpi_awareness() -> bool:
    """
    Enables Windows High-DPI scaling awareness process-wide to prevent blurred UI rendering.

    Returns:
        True if DPI awareness was successfully configured, False otherwise.
    """
    if sys.platform == "win32":
        try:
            # Per-monitor DPI awareness (Windows 8.1+)
            res = ctypes.windll.shcore.SetProcessDpiAwareness(1)
            return res == 0
        except Exception:
            try:
                # System DPI awareness fallback (Windows Vista+)
                return bool(ctypes.windll.user32.SetProcessDPIAware())
            except Exception:
                pass
    return False


# Suppress DPI blur on module import
enable_windows_dpi_awareness()


def get_auto_confirm_env() -> Optional[bool]:
    """
    Checks the GOG_MONITOR_AUTO_CONFIRM environment variable for test automation.

    Returns:
        True if truthy ('1', 'true', 'yes', 'y', 't'),
        False if falsy ('0', 'false', 'no', 'n', 'f'),
        None if unset or unrecognized.
    """
    val = os.environ.get("GOG_MONITOR_AUTO_CONFIRM")
    if val is None:
        return None
    cleaned = str(val).strip().lower()
    if cleaned in ("1", "true", "yes", "y", "t", "enable", "enabled", "on"):
        return True
    if cleaned in ("0", "false", "no", "n", "f", "disable", "disabled", "off"):
        return False
    return None


class InstallPromptDialog:
    """
    Modal confirmation dialog displayed when an uninstalled GOG game disk is inserted.

    Displays game custom icon, title, publisher, version, drive letter, and size,
    providing Install Game and Cancel actions with full keyboard bindings.
    """

    def __init__(
        self,
        config: Union[GOGDiskConfig, Dict[str, Any]],
        drive_letter: str = "",
        icon_path: Optional[str] = None,
        auto_confirm: Optional[bool] = None,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        """
        Initializes the InstallPromptDialog.

        Args:
            config: Parsed GOGDiskConfig instance or dictionary containing game metadata.
            drive_letter: Detected drive letter (e.g. 'E:' or 'E:\\').
            icon_path: Optional path to custom icon file on disk.
            auto_confirm: Explicit override for automated testing (skips UI if not None).
            timeout_seconds: Optional timeout in seconds after which the dialog automatically cancels.
        """
        self.config = config
        self.raw_data = self._extract_metadata(config)
        self.drive_letter = self._normalize_drive_letter(drive_letter)
        self.icon_path = icon_path or self.raw_data.get("icon_path")
        self.auto_confirm = auto_confirm
        self.timeout_seconds = timeout_seconds
        self.result: bool = False

    @staticmethod
    def _normalize_drive_letter(drive: str) -> str:
        """Normalizes drive letter string to 'X:' format."""
        if not drive or not isinstance(drive, str):
            return "Removable Drive"
        clean = drive.strip().rstrip("\\/").rstrip(":").upper()
        if len(clean) == 1 and clean.isalpha():
            return f"{clean}:"
        if clean:
            return f"{clean}:"
        return "Removable Drive"

    @staticmethod
    def _extract_metadata(config: Union[GOGDiskConfig, Dict[str, Any]]) -> Dict[str, Any]:
        """Extracts standard dictionary of metadata from GOGDiskConfig or dict."""
        if isinstance(config, dict):
            setup_dict = config.get("setup") if isinstance(config.get("setup"), dict) else {}
            return {
                "game_id": config.get("game_id", "unknown_game"),
                "title": config.get("title", "Unknown Game"),
                "publisher": config.get("publisher", "GOG.com"),
                "developer": config.get("developer"),
                "version": config.get("version", "1.0"),
                "icon_path": config.get("icon_path") or config.get("icon_file"),
                "setup_executable": setup_dict.get("executable") or config.get("setup_executable", "setup.exe"),
                "estimated_size_mb": setup_dict.get("estimated_size_mb") or config.get("estimated_size_mb"),
            }

        # GOGDiskConfig dataclass
        setup_exe = getattr(config.setup, "executable", "setup.exe") if hasattr(config, "setup") else "setup.exe"
        size_mb = getattr(config.setup, "estimated_size_mb", None) if hasattr(config, "setup") else None

        return {
            "game_id": getattr(config, "game_id", "unknown_game"),
            "title": getattr(config, "title", "Unknown Game"),
            "publisher": getattr(config, "publisher", "GOG.com") or "GOG.com",
            "developer": getattr(config, "developer", None),
            "version": getattr(config, "version", "1.0"),
            "icon_path": getattr(config, "icon_path", None),
            "setup_executable": setup_exe,
            "estimated_size_mb": size_mb,
        }

    def _load_icon_image(self, master: Any, icon_path: Optional[str]) -> Optional[Any]:
        """Loads and resizes game icon using PIL/Pillow or Tkinter PhotoImage."""
        if not icon_path or not os.path.isfile(icon_path):
            return None

        # 1. Try PIL / Pillow with context manager to avoid holding file locks
        if HAS_PIL and Image is not None and ImageTk is not None:
            try:
                with Image.open(icon_path) as raw_img:
                    pil_img = raw_img.convert("RGBA")
                    # Resize to crisp 64x64 icon
                    resized_img = pil_img.resize((64, 64), Image.Resampling.LANCZOS)
                    return ImageTk.PhotoImage(resized_img, master=master)
            except Exception as ex:
                logger.debug("Failed to load icon with PIL from %s: %s", icon_path, ex)

        # 2. Try native Tkinter PhotoImage (supports PNG/GIF)
        if HAS_TKINTER and tk is not None:
            try:
                return tk.PhotoImage(file=icon_path, master=master)
            except Exception as ex:
                logger.debug("Failed to load icon with Tkinter PhotoImage from %s: %s", icon_path, ex)

        return None

    def _create_fallback_icon(self, parent: Any) -> Any:
        """Generates a clean vector-drawn disc and gamepad icon canvas."""
        canvas = tk.Canvas(
            parent,
            width=72,
            height=72,
            bg="#1E293B",
            highlightthickness=0,
            relief="flat",
        )
        # Outer disc circle
        canvas.create_oval(6, 6, 66, 66, fill="#2563EB", outline="#60A5FA", width=2)
        # Inner disc ring
        canvas.create_oval(24, 24, 48, 48, fill="#1E293B", outline="#93C5FD", width=1)
        # Center spindle hole
        canvas.create_oval(30, 30, 42, 42, fill="#0F172A", outline="#3B82F6", width=1)
        # Gamepad emoji badge
        canvas.create_text(36, 36, text="🎮", font=("Segoe UI Emoji", 14))
        return canvas

    def show(self) -> bool:
        """
        Displays the modal prompt dialog and waits for user confirmation.

        Returns:
            True if user clicked 'Install Game' or confirmed via Enter.
            False if cancelled, dismissed, timed out, or auto-rejected.
        """
        # 1. Explicit auto_confirm override (used in unit tests)
        if self.auto_confirm is not None:
            logger.info("InstallPromptDialog: Using auto_confirm override = %s", self.auto_confirm)
            self.result = bool(self.auto_confirm)
            return self.result

        # 2. Environment variable auto-confirm override
        env_override = get_auto_confirm_env()
        if env_override is not None:
            logger.info("InstallPromptDialog: Using GOG_MONITOR_AUTO_CONFIRM env override = %s", env_override)
            self.result = env_override
            return self.result

        # 3. Headless / missing GUI environment protection
        if not HAS_TKINTER or tk is None:
            logger.warning("Tkinter is not available in current Python runtime. Rejecting prompt.")
            self.result = False
            return False

        try:
            root = tk.Tk()
        except (tk.TclError, Exception) as ex:
            logger.warning("Failed to initialize Tkinter GUI display: %s. Defaulting to cancel.", ex)
            self.result = False
            return False

        try:
            title = self.raw_data.get("title", "GOG Game")
            publisher = self.raw_data.get("publisher", "GOG.com")
            version = self.raw_data.get("version", "1.0")
            size_mb = self.raw_data.get("estimated_size_mb")

            root.title(f"GOG Game Disk — {self.drive_letter}")
            root.resizable(False, False)
            root.attributes("-topmost", True)

            # Set window icon if .ico available
            if self.icon_path and os.path.isfile(self.icon_path) and sys.platform == "win32":
                try:
                    if self.icon_path.lower().endswith(".ico"):
                        root.iconbitmap(self.icon_path)
                except Exception:
                    pass

            # Modern ttk styling
            style = ttk.Style(root)
            try:
                style.theme_use("clam")
            except Exception:
                pass

            # Main container
            main_frame = ttk.Frame(root, padding=20)
            main_frame.grid(row=0, column=0, sticky="nsew")

            # Left Column: Custom Icon or Fallback Canvas
            icon_frame = ttk.Frame(main_frame, padding=(0, 0, 16, 0))
            icon_frame.grid(row=0, column=0, rowspan=5, sticky="n")

            tk_img = self._load_icon_image(root, self.icon_path)
            if tk_img:
                icon_label = ttk.Label(icon_frame, image=tk_img)
                icon_label.image = tk_img  # Retain reference to prevent garbage collection
                icon_label.pack()
            else:
                fallback_canvas = self._create_fallback_icon(icon_frame)
                fallback_canvas.pack()

            # Right Column: Metadata & Prompt
            title_label = ttk.Label(
                main_frame,
                text=title,
                font=("Segoe UI", 12, "bold"),
                wraplength=340,
            )
            title_label.grid(row=0, column=1, sticky="w", pady=(0, 4))

            # Publisher & Version
            meta_str = f"Publisher: {publisher}  •  Version: {version}"
            meta_label = ttk.Label(
                main_frame,
                text=meta_str,
                font=("Segoe UI", 9),
                foreground="#475569",
            )
            meta_label.grid(row=1, column=1, sticky="w", pady=(0, 4))

            # Drive & Media Info
            drive_str = f"Detected Media: {self.drive_letter} (GOG Game Disk)"
            if size_mb:
                drive_str += f"  •  Size: ~{size_mb:,} MB"
            drive_label = ttk.Label(
                main_frame,
                text=drive_str,
                font=("Segoe UI", 9, "italic"),
                foreground="#334155",
            )
            drive_label.grid(row=2, column=1, sticky="w", pady=(0, 10))

            # Description message
            desc_label = ttk.Label(
                main_frame,
                text="This game is not yet installed on this PC.\nWould you like to run the installer now?",
                font=("Segoe UI", 9),
                wraplength=340,
            )
            desc_label.grid(row=3, column=1, sticky="w", pady=(0, 16))

            # Bottom Button Bar
            btn_frame = ttk.Frame(main_frame)
            btn_frame.grid(row=4, column=0, columnspan=2, sticky="e", pady=(8, 0))

            def on_install(*args: Any) -> None:
                self.result = True
                try:
                    root.destroy()
                except Exception:
                    pass

            def on_cancel(*args: Any) -> None:
                self.result = False
                try:
                    root.destroy()
                except Exception:
                    pass

            install_btn = ttk.Button(
                btn_frame,
                text="Install Game",
                command=on_install,
                default="active",
            )
            install_btn.pack(side="right", padx=(8, 0))

            cancel_btn = ttk.Button(
                btn_frame,
                text="Cancel",
                command=on_cancel,
            )
            cancel_btn.pack(side="right")

            # Keyboard Shortcuts
            root.bind("<Return>", on_install)
            root.bind("<KP_Enter>", on_install)
            root.bind("<Escape>", on_cancel)
            root.protocol("WM_DELETE_WINDOW", on_cancel)

            # Auto-timeout callback if specified
            if self.timeout_seconds and self.timeout_seconds > 0:
                root.after(int(self.timeout_seconds * 1000), on_cancel)

            # Center Dialog Window on Primary Monitor
            root.update_idletasks()
            w = root.winfo_reqwidth()
            h = root.winfo_reqheight()
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 2)
            root.geometry(f"+{x}+{y}")

            # Focus and Mainloop
            root.lift()
            root.focus_force()
            install_btn.focus_set()
            root.mainloop()

        except Exception as ex:
            logger.error("Error during prompt dialog execution: %s", ex)
            self.result = False
        finally:
            try:
                root.destroy()
            except Exception:
                pass

        return self.result

    @classmethod
    def show_prompt(
        cls,
        config: Union[GOGDiskConfig, Dict[str, Any]],
        icon_path: Optional[str] = None,
        drive_letter: str = "",
        auto_confirm: Optional[bool] = None,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """
        Static convenience helper to instantiate and display an InstallPromptDialog.

        Args:
            config: Parsed GOGDiskConfig or dictionary of game metadata.
            icon_path: Optional path to disk icon file.
            drive_letter: Detected drive letter.
            auto_confirm: Optional test override.
            timeout_seconds: Optional timeout in seconds.

        Returns:
            True if installation confirmed, False otherwise.
        """
        dialog = cls(
            config=config,
            drive_letter=drive_letter,
            icon_path=icon_path,
            auto_confirm=auto_confirm,
            timeout_seconds=timeout_seconds,
        )
        return dialog.show()
