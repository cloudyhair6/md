"""
gog_disk_monitor
~~~~~~~~~~~~~~~~

Windows System Tray GOG Game Disk Monitor.
Monitors logical drives for GOG game configuration descriptors,
manages local installation state, prompts user for setup, and automatically
launches installed games upon disk insertion.
"""

__version__ = "1.0.0"

from .config import (
    SetupConfig,
    LauncherConfig,
    GOGDiskConfig,
    parse_disk_config,
    find_disk_icon,
    normalize_disk_root,
    sanitize_argument,
    parse_and_sanitize_arguments,
)
from .state import (
    InstalledGameRecord,
    StateStore,
)
from .drive_monitor import (
    DriveInfo,
    DriveMonitor,
    DriveSimulator,
)
from .launcher import (
    ProcessExecutionError,
    ProcessRunner,
)
from .prompt_dialog import (
    InstallPromptDialog,
    enable_windows_dpi_awareness,
    get_auto_confirm_env,
)
from .tray import (
    TrayManager,
    create_default_icon_image,
    load_tray_icon,
)
from .app import (
    GOGDiskMonitorApp,
)
from .cli import (
    main,
    build_parser,
)

__all__ = [
    "__version__",
    "SetupConfig",
    "LauncherConfig",
    "GOGDiskConfig",
    "parse_disk_config",
    "find_disk_icon",
    "normalize_disk_root",
    "sanitize_argument",
    "parse_and_sanitize_arguments",
    "InstalledGameRecord",
    "StateStore",
    "DriveInfo",
    "DriveMonitor",
    "DriveSimulator",
    "ProcessExecutionError",
    "ProcessRunner",
    "InstallPromptDialog",
    "enable_windows_dpi_awareness",
    "get_auto_confirm_env",
    "TrayManager",
    "create_default_icon_image",
    "load_tray_icon",
    "GOGDiskMonitorApp",
    "main",
    "build_parser",
]

