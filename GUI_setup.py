#!/usr/bin/env python3
"""
GUI_setup.py
~~~~~~~~~~~~

A graphical user interface and deployment engine built with PyQt / PySide for
creating, configuring, and deploying GOG game disk packages compatible with
GOG Game Disk Monitor.

Generates `gog_game.json` configuration descriptors, copies game setup binaries
and custom icons (.ico / .png) into target drives or directories, and validates
the output against the GOG Disk Monitor configuration schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import html
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import string
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

# ---------------------------------------------------------------------------
# Dynamic Qt Framework Binding (PyQt6 -> PySide6 -> PyQt5)
# ---------------------------------------------------------------------------
QT_BINDING: Optional[str] = None

try:
    from PyQt6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSpinBox,
        QSplitter,
        QStatusBar,
        QStyle,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )
    from PyQt6.QtCore import Qt, QSize, pyqtSignal as Signal
    from PyQt6.QtGui import QColor, QFont, QIcon, QPalette, QPixmap
    QT_BINDING = "PyQt6"
except ImportError:
    try:
        from PySide6.QtWidgets import (
            QApplication,
            QCheckBox,
            QComboBox,
            QFileDialog,
            QFrame,
            QGridLayout,
            QGroupBox,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QMainWindow,
            QMessageBox,
            QPlainTextEdit,
            QProgressBar,
            QPushButton,
            QScrollArea,
            QSizePolicy,
            QSpinBox,
            QSplitter,
            QStatusBar,
            QStyle,
            QTabWidget,
            QVBoxLayout,
            QWidget,
        )
        from PySide6.QtCore import Qt, QSize, Signal
        from PySide6.QtGui import QColor, QFont, QIcon, QPalette, QPixmap
        QT_BINDING = "PySide6"
    except ImportError:
        try:
            from PyQt5.QtWidgets import (
                QApplication,
                QCheckBox,
                QComboBox,
                QFileDialog,
                QFrame,
                QGridLayout,
                QGroupBox,
                QHBoxLayout,
                QLabel,
                QLineEdit,
                QMainWindow,
                QMessageBox,
                QPlainTextEdit,
                QProgressBar,
                QPushButton,
                QScrollArea,
                QSizePolicy,
                QSpinBox,
                QSplitter,
                QStatusBar,
                QStyle,
                QTabWidget,
                QVBoxLayout,
                QWidget,
            )
            from PyQt5.QtCore import Qt, QSize, pyqtSignal as Signal
            from PyQt5.QtGui import QColor, QFont, QIcon, QPalette, QPixmap
            QT_BINDING = "PyQt5"
        except ImportError:
            QT_BINDING = None

# GOG Disk Monitor config parser integration
try:
    from gog_disk_monitor.config import (
        CONFIG_FILENAMES,
        DEFAULT_ICON_FILENAMES,
        GOGDiskConfig,
        LauncherConfig,
        SetupConfig,
        find_disk_icon,
        normalize_disk_root,
        parse_and_sanitize_arguments,
        parse_disk_config,
        sanitize_argument,
    )
except ImportError:
    # Standalone execution fallback if package not installed in site-packages
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from gog_disk_monitor.config import (
        CONFIG_FILENAMES,
        DEFAULT_ICON_FILENAMES,
        GOGDiskConfig,
        LauncherConfig,
        SetupConfig,
        find_disk_icon,
        normalize_disk_root,
        parse_and_sanitize_arguments,
        parse_disk_config,
        sanitize_argument,
    )

logger = logging.getLogger("gog_disk_setup")


# ---------------------------------------------------------------------------
# Data Models & Core Deployment Engine
# ---------------------------------------------------------------------------

@dataclass
class DeploymentResult:
    """
    Result metadata from a GOG game disk deployment operation.

    Attributes:
        success: Whether the deployment and schema verification succeeded.
        target_dir: Target directory where files were deployed.
        config_path: Path to the written `gog_game.json` file.
        executable_path: Path to the deployed executable file.
        icon_path: Path to the deployed icon file (if provided).
        parsed_config: Validated GOGDiskConfig object if parsing succeeded.
        message: Human-readable status or error summary.
        errors: List of specific error messages if any occurred.
    """
    success: bool
    target_dir: str
    config_path: Optional[str] = None
    executable_path: Optional[str] = None
    icon_path: Optional[str] = None
    parsed_config: Optional[GOGDiskConfig] = None
    message: str = ""
    errors: List[str] = field(default_factory=list)


def slugify_game_id(text: Optional[str]) -> str:
    """
    Generate a safe, lowercase snake_case identifier from game title.

    Args:
        text: Game title or string (e.g. 'Cyberpunk 2077: Phantom Liberty').

    Returns:
        Slugified identifier (e.g. 'cyberpunk_2077_phantom_liberty').
    """
    if not text or not isinstance(text, str):
        return "game"
    # Replace non-alphanumeric characters with underscore
    slug = re.sub(r"[^\w\s-]", "", text.strip()).strip()
    slug = re.sub(r"[-\s]+", "_", slug).lower()
    slug = re.sub(r"_+", "_", slug)
    slug = slug.strip("_")
    return slug if slug else "game"


def parse_arguments_list(args_input: Union[str, Sequence[Any], Any, None]) -> List[str]:
    """
    Parse command-line argument string or list into a clean list of arguments.

    Handles standard spaces, quoted parameters, and embedded quotes such as:
    `/DIR="C:\\Games\\Witcher" /SILENT` -> `['/DIR="C:\\Games\\Witcher"', '/SILENT']`

    Args:
        args_input: String command line, sequence of arguments, or None.

    Returns:
        List of individual argument strings.
    """
    if args_input is None or isinstance(args_input, bool):
        return []
    if isinstance(args_input, (list, tuple, set)):
        return [str(a).strip() for a in args_input if str(a).strip()]
    if isinstance(args_input, (int, float)):
        return [str(args_input)]
    if isinstance(args_input, str):
        cleaned = args_input.strip()
        if not cleaned:
            return []
        pattern = r"""(?:[^\s"']|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')+"""
        matches = re.findall(pattern, cleaned)
        if matches:
            return matches
        return [cleaned]
    return []


def _sanitize_dest_relpath(relpath: Optional[str], default_name: str) -> str:
    """
    Sanitize destination relative filename, preventing path traversal and root escapes.

    Args:
        relpath: Raw relative subpath or filename (e.g. 'installer/setup.exe').
        default_name: Fallback filename if input is empty or invalid.

    Returns:
        Sanitized, normalized relative path with forward slashes.
    """
    if not relpath or not str(relpath).strip():
        return default_name
    cleaned = str(relpath).strip().replace("\\", "/")
    # Remove drive letters if present (e.g. C:)
    if len(cleaned) >= 2 and cleaned[1] == ":" and cleaned[0].isalpha():
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("/")
    norm = os.path.normpath(cleaned).replace("\\", "/")
    if norm.startswith("..") or norm in (".", "/"):
        norm = default_name
    return norm if norm else default_name


def get_available_drives() -> List[str]:
    """
    Enumerate available logical drive roots on Windows.

    Returns:
        List of drive root paths (e.g. ['C:\\', 'D:\\', 'E:\\']).
    """
    drives = []
    if sys.platform == "win32":
        try:
            import ctypes
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for letter in string.ascii_uppercase:
                if bitmask & 1:
                    drive_path = f"{letter}:\\"
                    drives.append(drive_path)
                bitmask >>= 1
        except Exception:
            for letter in string.ascii_uppercase:
                drive_path = f"{letter}:\\"
                if os.path.exists(drive_path):
                    drives.append(drive_path)
    return drives


def build_gog_config_dict(
    game_id: Optional[str] = None,
    title: str = "",
    version: str = "1.0.0",
    setup_executable: str = "setup.exe",
    setup_arguments: Optional[Union[str, List[str]]] = None,
    default_install_subdir: Optional[str] = None,
    estimated_size_mb: Optional[Union[int, float, str]] = None,
    silent_supported: bool = False,
    launcher_executable: Optional[str] = None,
    launcher_arguments: Optional[Union[str, List[str]]] = None,
    working_directory: Optional[str] = None,
    requires_admin: bool = False,
    icon_path: Optional[str] = None,
    publisher: Optional[str] = None,
    developer: Optional[str] = None,
    disk_number: Union[int, float, str] = 1,
    total_disks: Union[int, float, str] = 1,
    disk_label: Optional[str] = None,
    schema_version: str = "1.0",
) -> Dict[str, Any]:
    """
    Build a standard GOG game configuration dictionary matching the schema.

    Args:
        game_id: Unique string identifier for state tracking.
        title: Human-readable game title.
        version: Game release/patch version.
        setup_executable: Relative path to setup installer on disk.
        setup_arguments: Setup command-line arguments.
        default_install_subdir: Suggested destination folder name.
        estimated_size_mb: Estimated required disk space in megabytes.
        silent_supported: Whether unattended installation is supported.
        launcher_executable: Relative path to installed game binary.
        launcher_arguments: Command-line arguments for launching game.
        working_directory: Working directory relative to install folder.
        requires_admin: Whether UAC elevation is required.
        icon_path: Relative path to custom icon on disk.
        publisher: Publisher name.
        developer: Developer name.
        disk_number: Disc sequence index (1-based).
        total_disks: Total disc count in multi-disc set.
        disk_label: Volume label for disc identification.
        schema_version: Schema version string.

    Returns:
        Structured dictionary matching `gog_game.json` schema.
    """
    norm_title = str(title).strip() if title else ""
    norm_game_id = str(game_id).strip() if (game_id and str(game_id).strip()) else slugify_game_id(norm_title)
    norm_version = str(version).strip() if (version and str(version).strip()) else "1.0.0"

    # Setup configuration block
    setup_exe_rel = _sanitize_dest_relpath(setup_executable, "setup.exe")
    setup_args = parse_arguments_list(setup_arguments)

    setup_dict: Dict[str, Any] = {
        "executable": setup_exe_rel,
    }
    if setup_args:
        setup_dict["arguments"] = setup_args
    if default_install_subdir and str(default_install_subdir).strip():
        setup_dict["default_install_subdir"] = str(default_install_subdir).strip()
    if estimated_size_mb is not None:
        try:
            size_int = int(float(str(estimated_size_mb).strip()))
            if size_int > 0:
                setup_dict["estimated_size_mb"] = size_int
        except (ValueError, TypeError):
            pass
    if silent_supported:
        setup_dict["silent_supported"] = True

    # Launcher configuration block
    launcher_exe_rel = (
        _sanitize_dest_relpath(launcher_executable, setup_exe_rel)
        if (launcher_executable and str(launcher_executable).strip())
        else setup_exe_rel
    )
    launcher_args = parse_arguments_list(launcher_arguments)

    launcher_dict: Dict[str, Any] = {
        "executable": launcher_exe_rel,
    }
    if launcher_args:
        launcher_dict["arguments"] = launcher_args
    if working_directory and str(working_directory).strip():
        sanitized_workdir = _sanitize_dest_relpath(str(working_directory).strip(), "").replace("\\", "/")
        if sanitized_workdir:
            launcher_dict["working_directory"] = sanitized_workdir
    if requires_admin:
        launcher_dict["requires_admin"] = True

    # Master config dictionary
    config: Dict[str, Any] = {
        "schema_version": str(schema_version).strip() if schema_version else "1.0",
        "game_id": norm_game_id,
        "title": norm_title,
        "version": norm_version,
        "setup": setup_dict,
        "launcher": launcher_dict,
    }

    if icon_path and str(icon_path).strip():
        sanitized_icon = _sanitize_dest_relpath(str(icon_path).strip(), "icon.ico")
        if sanitized_icon:
            config["icon_path"] = sanitized_icon
    if publisher and str(publisher).strip():
        config["publisher"] = str(publisher).strip()
    if developer and str(developer).strip():
        config["developer"] = str(developer).strip()

    # Disk info
    try:
        d_num = int(float(str(disk_number).strip())) if disk_number is not None else 1
    except (ValueError, TypeError):
        d_num = 1
    try:
        t_disks = int(float(str(total_disks).strip())) if total_disks is not None else 1
    except (ValueError, TypeError):
        t_disks = 1
    if d_num < 1:
        d_num = 1
    if t_disks < 1:
        t_disks = 1
    if d_num > t_disks:
        t_disks = d_num

    disk_info: Dict[str, Any] = {
        "disk_number": d_num,
        "total_disks": t_disks,
    }
    if disk_label and str(disk_label).strip():
        disk_info["label"] = str(disk_label).strip()
    elif norm_title:
        disk_info["label"] = f"{slugify_game_id(norm_title).upper()}_DISC{d_num}"
    config["disk_info"] = disk_info

    return config


def deploy_game_disk(
    source_executable: str,
    target_dir: str,
    title: str,
    game_id: Optional[str] = None,
    version: str = "1.0.0",
    source_icon: Optional[str] = None,
    setup_arguments: Optional[Union[str, List[str]]] = None,
    default_install_subdir: Optional[str] = None,
    estimated_size_mb: Optional[int] = None,
    silent_supported: bool = False,
    launcher_executable: Optional[str] = None,
    launcher_arguments: Optional[Union[str, List[str]]] = None,
    working_directory: Optional[str] = None,
    requires_admin: bool = False,
    publisher: Optional[str] = None,
    developer: Optional[str] = None,
    disk_number: int = 1,
    total_disks: int = 1,
    disk_label: Optional[str] = None,
    config_filename: str = "gog_game.json",
    dest_setup_name: Optional[str] = None,
    dest_icon_name: Optional[str] = None,
    overwrite: bool = True,
) -> DeploymentResult:
    """
    Deploy game executable, custom icon, and JSON descriptor to target disk/folder.

    Performs full file copying, JSON configuration generation, and post-deployment
    schema verification using the existing `gog_disk_monitor.config` parser.

    Args:
        source_executable: Path to source game setup/installer binary.
        target_dir: Destination disk root or directory path.
        title: Game title.
        game_id: Optional unique game identifier (auto-generated if omitted).
        version: Version string.
        source_icon: Path to custom icon file (.ico / .png), optional.
        setup_arguments: Arguments passed to setup executable.
        default_install_subdir: Default install subfolder name.
        estimated_size_mb: Estimated size in MB.
        silent_supported: Unattended install support flag.
        launcher_executable: Relative path to game executable on local PC.
        launcher_arguments: Arguments for game executable.
        working_directory: Working directory relative to install folder.
        requires_admin: Administrator elevation flag.
        publisher: Publisher name.
        developer: Developer name.
        disk_number: Disk number index.
        total_disks: Total disks count.
        disk_label: Volume label for disk info.
        config_filename: Descriptor filename ('gog_game.json' or 'gog_disk.json').
        dest_setup_name: Custom filename/relpath for executable on destination.
        dest_icon_name: Custom filename/relpath for icon on destination.
        overwrite: Whether to overwrite existing files in destination.

    Returns:
        DeploymentResult containing status, output paths, and validated config.
    """
    errors: List[str] = []

    # 1. Validate mandatory inputs
    if not title or not str(title).strip():
        errors.append("Game title cannot be empty.")
    if not source_executable or not str(source_executable).strip():
        errors.append("Game executable path must be specified.")
    if not target_dir or not str(target_dir).strip():
        errors.append("Target directory must be specified.")

    if errors:
        return DeploymentResult(
            success=False,
            target_dir=target_dir or "",
            message="Validation failed.",
            errors=errors,
        )

    # 2. Check source file existence
    src_exe_path = Path(source_executable).resolve()
    if not src_exe_path.is_file():
        errors.append(f"Source executable does not exist: '{source_executable}'")

    src_icon_path: Optional[Path] = None
    if source_icon and str(source_icon).strip():
        src_icon_path = Path(source_icon).resolve()
        if not src_icon_path.is_file():
            errors.append(f"Source icon file does not exist: '{source_icon}'")

    if errors:
        return DeploymentResult(
            success=False,
            target_dir=target_dir,
            message="Source file verification failed.",
            errors=errors,
        )

    # 3. Prepare target directory (handling single drive letter normalization)
    norm_target = normalize_disk_root(target_dir) if target_dir else ""
    tgt_path = Path(norm_target if norm_target else target_dir).resolve()
    try:
        tgt_path.mkdir(parents=True, exist_ok=True)
    except OSError as ex:
        return DeploymentResult(
            success=False,
            target_dir=str(tgt_path),
            message=f"Failed to create target directory: {ex}",
            errors=[str(ex)],
        )

    # 4. Determine destination paths and sanitize relative names
    exe_target_relname = _sanitize_dest_relpath(dest_setup_name, src_exe_path.name)
    dst_exe_path = tgt_path / exe_target_relname

    dst_icon_path: Optional[Path] = None
    icon_rel_for_config: Optional[str] = None
    if src_icon_path:
        icon_target_relname = _sanitize_dest_relpath(dest_icon_name, src_icon_path.name)
        dst_icon_path = tgt_path / icon_target_relname
        icon_rel_for_config = icon_target_relname.replace("\\", "/")

    raw_config_name = Path(_sanitize_dest_relpath(config_filename, "gog_game.json")).name
    if raw_config_name.lower() in ("gog_game.json", "gog_disk.json"):
        clean_config_filename = raw_config_name
    else:
        clean_config_filename = "gog_game.json"
    dst_config_path = tgt_path / clean_config_filename

    # Check for destination path collisions among distinct source targets
    collision_errors = []
    if dst_exe_path.resolve() == dst_config_path.resolve():
        collision_errors.append(f"Executable target path collides with config file path: '{dst_exe_path}'")
    if dst_icon_path and dst_icon_path.resolve() == dst_config_path.resolve():
        collision_errors.append(f"Icon target path collides with config file path: '{dst_icon_path}'")
    if dst_icon_path and dst_exe_path.resolve() == dst_icon_path.resolve():
        collision_errors.append(f"Executable and icon target paths collide: '{dst_exe_path}'")

    if collision_errors:
        return DeploymentResult(
            success=False,
            target_dir=str(tgt_path),
            message="Destination path collision detected.",
            errors=collision_errors,
        )

    # Check if any destination path exists as a directory
    for path_obj, path_label in [
        (dst_exe_path, "Executable destination"),
        (dst_icon_path, "Icon destination"),
        (dst_config_path, "Configuration destination"),
    ]:
        if path_obj and path_obj.is_dir():
            return DeploymentResult(
                success=False,
                target_dir=str(tgt_path),
                message=f"{path_label} cannot be an existing directory: '{path_obj}'",
                errors=[f"Target path is an existing directory: '{path_obj}'"],
            )

    # Check overwrite constraint
    if not overwrite:
        existing_conflicts = []
        if dst_exe_path.exists() and src_exe_path.resolve() != dst_exe_path.resolve():
            existing_conflicts.append(str(dst_exe_path))
        if dst_icon_path and dst_icon_path.exists() and src_icon_path and src_icon_path.resolve() != dst_icon_path.resolve():
            existing_conflicts.append(str(dst_icon_path))
        if dst_config_path.exists():
            existing_conflicts.append(str(dst_config_path))
        if existing_conflicts:
            return DeploymentResult(
                success=False,
                target_dir=str(tgt_path),
                message="Target destination files already exist and overwrite is False.",
                errors=[f"File already exists: '{p}'" for p in existing_conflicts],
            )

    # Helper function to remove read-only attribute if replacing existing file
    def _prepare_write(p: Path) -> None:
        if p.exists():
            try:
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            except Exception:
                pass

    # 5. Copy Executable to Target
    try:
        dst_exe_path.parent.mkdir(parents=True, exist_ok=True)
        if src_exe_path.resolve() != dst_exe_path.resolve():
            _prepare_write(dst_exe_path)
            shutil.copy2(src_exe_path, dst_exe_path)
    except Exception as ex:
        return DeploymentResult(
            success=False,
            target_dir=str(tgt_path),
            message=f"Failed to copy executable to target: {ex}",
            errors=[str(ex)],
        )

    # 6. Copy Icon to Target (if provided)
    if src_icon_path and dst_icon_path:
        try:
            dst_icon_path.parent.mkdir(parents=True, exist_ok=True)
            if src_icon_path.resolve() != dst_icon_path.resolve():
                _prepare_write(dst_icon_path)
                shutil.copy2(src_icon_path, dst_icon_path)
        except Exception as ex:
            return DeploymentResult(
                success=False,
                target_dir=str(tgt_path),
                executable_path=str(dst_exe_path),
                message=f"Failed to copy icon to target: {ex}",
                errors=[str(ex)],
            )

    # 7. Build and Write gog_game.json Descriptor
    norm_game_id = game_id.strip() if (game_id and game_id.strip()) else slugify_game_id(title)
    config_dict = build_gog_config_dict(
        game_id=norm_game_id,
        title=title.strip(),
        version=version.strip() if (version and version.strip()) else "1.0.0",
        setup_executable=exe_target_relname.replace("\\", "/"),
        setup_arguments=setup_arguments,
        default_install_subdir=default_install_subdir.strip() if (default_install_subdir and default_install_subdir.strip()) else title.strip(),
        estimated_size_mb=estimated_size_mb,
        silent_supported=silent_supported,
        launcher_executable=launcher_executable or exe_target_relname.replace("\\", "/"),
        launcher_arguments=launcher_arguments,
        working_directory=working_directory,
        requires_admin=requires_admin,
        icon_path=icon_rel_for_config,
        publisher=publisher,
        developer=developer,
        disk_number=disk_number,
        total_disks=total_disks,
        disk_label=disk_label,
    )

    try:
        dst_config_path.parent.mkdir(parents=True, exist_ok=True)
        _prepare_write(dst_config_path)
        with open(dst_config_path, "w", encoding="utf-8") as f:
            json.dump(config_dict, f, indent=2, ensure_ascii=False)
    except Exception as ex:
        return DeploymentResult(
            success=False,
            target_dir=str(tgt_path),
            executable_path=str(dst_exe_path),
            icon_path=str(dst_icon_path) if dst_icon_path else None,
            message=f"Failed to write configuration file: {ex}",
            errors=[str(ex)],
        )

    # 8. Post-Deployment Verification via GOGDiskMonitor Config Parser
    parsed_config = parse_disk_config(str(tgt_path))
    if parsed_config is None:
        return DeploymentResult(
            success=False,
            target_dir=str(tgt_path),
            config_path=str(dst_config_path),
            executable_path=str(dst_exe_path),
            icon_path=str(dst_icon_path) if dst_icon_path else None,
            message="Deployment generated files, but config.py parser failed validation.",
            errors=["Config validation failed on generated descriptor."],
        )

    resolved_icon = find_disk_icon(str(tgt_path), parsed_config)

    return DeploymentResult(
        success=True,
        target_dir=str(tgt_path),
        config_path=str(dst_config_path),
        executable_path=str(dst_exe_path),
        icon_path=resolved_icon or (str(dst_icon_path) if dst_icon_path else None),
        parsed_config=parsed_config,
        message=f"Successfully deployed '{title}' to {tgt_path}.",
    )


# ---------------------------------------------------------------------------
# Qt Graphical User Interface
# ---------------------------------------------------------------------------

if QT_BINDING is not None:

    class DiskSetupWindow(QMainWindow):
        """
        PyQt/PySide Main Window for GOG Game Disk Generator & Setup Utility.
        """

        deployment_completed = Signal(object)

        def __init__(self, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self.setWindowTitle("GOG Game Disk Setup & Package Generator")
            self.resize(880, 740)
            self.setMinimumSize(720, 600)

            self._user_edited_id = False
            self._user_edited_subdir = False

            self._init_ui()
            self._apply_styling()
            self.update_json_preview()

        def _init_ui(self) -> None:
            """Construct layout, input groups, and widgets."""
            central_widget = QWidget(self)
            self.setCentralWidget(central_widget)

            main_layout = QVBoxLayout(central_widget)
            main_layout.setContentsMargins(16, 16, 16, 16)
            main_layout.setSpacing(12)

            # Header Banner
            header_box = QGroupBox(self)
            header_box.setObjectName("headerBox")
            header_layout = QHBoxLayout(header_box)
            header_layout.setContentsMargins(12, 10, 12, 10)

            title_label = QLabel("<b>GOG Game Disk Generator</b>", self)
            title_label.setObjectName("headerTitle")
            subtitle_label = QLabel(
                "Configure and deploy autorun-ready GOG game media (executable, icon, and JSON descriptor).",
                self,
            )
            subtitle_label.setObjectName("headerSubtitle")

            header_text_vbox = QVBoxLayout()
            header_text_vbox.addWidget(title_label)
            header_text_vbox.addWidget(subtitle_label)
            header_layout.addLayout(header_text_vbox)
            header_layout.addStretch()

            main_layout.addWidget(header_box)

            # Scroll Area for Form Fields
            scroll_area = QScrollArea(self)
            scroll_area.setWidgetResizable(True)
            scroll_area.setFrameShape(QFrame.Shape.NoFrame if hasattr(QFrame, "Shape") else QFrame.NoFrame)

            form_container = QWidget()
            form_layout = QVBoxLayout(form_container)
            form_layout.setContentsMargins(4, 4, 4, 4)
            form_layout.setSpacing(12)

            # -------------------------------------------------------------
            # Group 1: Game Metadata
            # -------------------------------------------------------------
            meta_group = QGroupBox("1. Game Metadata", form_container)
            meta_grid = QGridLayout(meta_group)
            meta_grid.setSpacing(8)

            meta_grid.addWidget(QLabel("Game Title: *", meta_group), 0, 0)
            self.title_input = QLineEdit(meta_group)
            self.title_input.setPlaceholderText("e.g. The Witcher 3: Wild Hunt")
            self.title_input.textChanged.connect(self._on_title_changed)
            meta_grid.addWidget(self.title_input, 0, 1, 1, 3)

            meta_grid.addWidget(QLabel("Game ID: *", meta_group), 1, 0)
            self.game_id_input = QLineEdit(meta_group)
            self.game_id_input.setPlaceholderText("e.g. witcher_3 (unique slug)")
            self.game_id_input.textChanged.connect(self._on_id_changed)
            meta_grid.addWidget(self.game_id_input, 1, 1)

            meta_grid.addWidget(QLabel("Version: *", meta_group), 1, 2)
            self.version_input = QLineEdit("1.0.0", meta_group)
            self.version_input.textChanged.connect(self.update_json_preview)
            meta_grid.addWidget(self.version_input, 1, 3)

            meta_grid.addWidget(QLabel("Publisher:", meta_group), 2, 0)
            self.publisher_input = QLineEdit(meta_group)
            self.publisher_input.setPlaceholderText("e.g. CD PROJEKT")
            self.publisher_input.textChanged.connect(self.update_json_preview)
            meta_grid.addWidget(self.publisher_input, 2, 1)

            meta_grid.addWidget(QLabel("Developer:", meta_group), 2, 2)
            self.developer_input = QLineEdit(meta_group)
            self.developer_input.setPlaceholderText("e.g. CD PROJEKT RED")
            self.developer_input.textChanged.connect(self.update_json_preview)
            meta_grid.addWidget(self.developer_input, 2, 3)

            form_layout.addWidget(meta_group)

            # -------------------------------------------------------------
            # Group 2: Files & Assets (Executable, Icon)
            # -------------------------------------------------------------
            files_group = QGroupBox("2. Files & Media Assets", form_container)
            files_grid = QGridLayout(files_group)
            files_grid.setSpacing(8)

            # Executable Selection
            files_grid.addWidget(QLabel("Game Executable: *", files_group), 0, 0)
            self.exe_path_input = QLineEdit(files_group)
            self.exe_path_input.setPlaceholderText("Select source setup.exe or game binary...")
            self.exe_path_input.textChanged.connect(self._on_exe_changed)
            files_grid.addWidget(self.exe_path_input, 0, 1)

            self.exe_browse_btn = QPushButton("Browse...", files_group)
            self.exe_browse_btn.clicked.connect(self._on_browse_exe)
            files_grid.addWidget(self.exe_browse_btn, 0, 2)

            # Icon Selection & Thumbnail
            files_grid.addWidget(QLabel("Game Icon (.ico / .png):", files_group), 1, 0)
            self.icon_path_input = QLineEdit(files_group)
            self.icon_path_input.setPlaceholderText("Select custom game icon (.ico or .png)...")
            self.icon_path_input.textChanged.connect(self._on_icon_changed)
            files_grid.addWidget(self.icon_path_input, 1, 1)

            self.icon_browse_btn = QPushButton("Browse...", files_group)
            self.icon_browse_btn.clicked.connect(self._on_browse_icon)
            files_grid.addWidget(self.icon_browse_btn, 1, 2)

            # Icon Preview Widget
            self.icon_preview_lbl = QLabel(files_group)
            self.icon_preview_lbl.setFixedSize(36, 36)
            self.icon_preview_lbl.setAlignment(
                Qt.AlignmentFlag.AlignCenter if hasattr(Qt, "AlignmentFlag") else Qt.AlignCenter
            )
            self.icon_preview_lbl.setStyleSheet("border: 1px dashed #666; border-radius: 4px; background: #222;")
            files_grid.addWidget(self.icon_preview_lbl, 1, 3)

            form_layout.addWidget(files_group)

            # -------------------------------------------------------------
            # Group 3: Setup & Installation Options
            # -------------------------------------------------------------
            setup_group = QGroupBox("3. Installation & Setup Options", form_container)
            setup_grid = QGridLayout(setup_group)
            setup_grid.setSpacing(8)

            setup_grid.addWidget(QLabel("Setup Arguments:", setup_group), 0, 0)
            self.setup_args_input = QLineEdit(setup_group)
            self.setup_args_input.setPlaceholderText('e.g. /SILENT /DIR="C:\\Games"')
            self.setup_args_input.textChanged.connect(self.update_json_preview)
            setup_grid.addWidget(self.setup_args_input, 0, 1)

            setup_grid.addWidget(QLabel("Default Install Subfolder:", setup_group), 0, 2)
            self.install_subdir_input = QLineEdit(setup_group)
            self.install_subdir_input.setPlaceholderText("e.g. Witcher 3")
            self.install_subdir_input.textChanged.connect(self._on_subdir_changed)
            setup_grid.addWidget(self.install_subdir_input, 0, 3)

            setup_grid.addWidget(QLabel("Estimated Size (MB):", setup_group), 1, 0)
            self.size_spin = QSpinBox(setup_group)
            self.size_spin.setRange(0, 5000000)
            self.size_spin.setSpecialValueText("Auto / None")
            self.size_spin.valueChanged.connect(self.update_json_preview)
            setup_grid.addWidget(self.size_spin, 1, 1)

            self.silent_chk = QCheckBox("Supports Silent / Unattended Installation", setup_group)
            self.silent_chk.stateChanged.connect(self.update_json_preview)
            setup_grid.addWidget(self.silent_chk, 1, 2, 1, 2)

            form_layout.addWidget(setup_group)

            # -------------------------------------------------------------
            # Group 4: Launcher & Execution Options
            # -------------------------------------------------------------
            launcher_group = QGroupBox("4. Installed Game Launcher Settings", form_container)
            launcher_grid = QGridLayout(launcher_group)
            launcher_grid.setSpacing(8)

            launcher_grid.addWidget(QLabel("Launcher Executable (Rel):", launcher_group), 0, 0)
            self.launcher_exe_input = QLineEdit(launcher_group)
            self.launcher_exe_input.setPlaceholderText("e.g. bin/x64/witcher3.exe or game.exe")
            self.launcher_exe_input.textChanged.connect(self.update_json_preview)
            launcher_grid.addWidget(self.launcher_exe_input, 0, 1)

            launcher_grid.addWidget(QLabel("Launcher Arguments:", launcher_group), 0, 2)
            self.launcher_args_input = QLineEdit(launcher_group)
            self.launcher_args_input.setPlaceholderText("e.g. -dx12 --fullscreen")
            self.launcher_args_input.textChanged.connect(self.update_json_preview)
            launcher_grid.addWidget(self.launcher_args_input, 0, 3)

            launcher_grid.addWidget(QLabel("Working Directory (Rel):", launcher_group), 1, 0)
            self.working_dir_input = QLineEdit(launcher_group)
            self.working_dir_input.setPlaceholderText("e.g. bin/x64 (optional)")
            self.working_dir_input.textChanged.connect(self.update_json_preview)
            launcher_grid.addWidget(self.working_dir_input, 1, 1)

            self.admin_chk = QCheckBox("Requires Administrator Privileges (UAC)", launcher_group)
            self.admin_chk.stateChanged.connect(self.update_json_preview)
            launcher_grid.addWidget(self.admin_chk, 1, 2, 1, 2)

            form_layout.addWidget(launcher_group)

            # -------------------------------------------------------------
            # Group 5: Target Output Media / Directory
            # -------------------------------------------------------------
            target_group = QGroupBox("5. Target Output Disk / Folder *", form_container)
            target_grid = QGridLayout(target_group)
            target_grid.setSpacing(8)

            target_grid.addWidget(QLabel("Target Directory / Drive: *", target_group), 0, 0)
            self.target_dir_input = QLineEdit(target_group)
            self.target_dir_input.setPlaceholderText("Select drive letter (e.g. D:\\) or output directory...")
            target_grid.addWidget(self.target_dir_input, 0, 1)

            self.target_browse_btn = QPushButton("Browse Folder...", target_group)
            self.target_browse_btn.clicked.connect(self._on_browse_target)
            target_grid.addWidget(self.target_browse_btn, 0, 2)

            # Quick Drive Dropdown
            self.drive_combo = QComboBox(target_group)
            self.drive_combo.addItem("Select Drive...")
            self._populate_drives()
            self.drive_combo.currentIndexChanged.connect(self._on_drive_selected)
            target_grid.addWidget(self.drive_combo, 0, 3)

            # Disk Info Sub-row
            target_grid.addWidget(QLabel("Disc Number:", target_group), 1, 0)
            self.disc_num_spin = QSpinBox(target_group)
            self.disc_num_spin.setRange(1, 99)
            self.disc_num_spin.setValue(1)
            self.disc_num_spin.valueChanged.connect(self.update_json_preview)
            target_grid.addWidget(self.disc_num_spin, 1, 1)

            target_grid.addWidget(QLabel("Total Discs:", target_group), 1, 2)
            self.total_discs_spin = QSpinBox(target_group)
            self.total_discs_spin.setRange(1, 99)
            self.total_discs_spin.setValue(1)
            self.total_discs_spin.valueChanged.connect(self.update_json_preview)
            target_grid.addWidget(self.total_discs_spin, 1, 3)

            form_layout.addWidget(target_group)

            # -------------------------------------------------------------
            # Group 6: Live JSON Preview Tab
            # -------------------------------------------------------------
            preview_group = QGroupBox("Configuration Preview (gog_game.json)", form_container)
            preview_layout = QVBoxLayout(preview_group)
            preview_layout.setContentsMargins(8, 8, 8, 8)

            self.json_preview = QPlainTextEdit(preview_group)
            self.json_preview.setReadOnly(True)
            self.json_preview.setMaximumHeight(160)
            self.json_preview.setFont(QFont("Consolas", 9) if QFont("Consolas").exactMatch() else QFont("Monospace", 9))
            preview_layout.addWidget(self.json_preview)

            form_layout.addWidget(preview_group)

            scroll_area.setWidget(form_container)
            main_layout.addWidget(scroll_area)

            # -------------------------------------------------------------
            # Action Buttons & Status Bar
            # -------------------------------------------------------------
            actions_hbox = QHBoxLayout()
            actions_hbox.setSpacing(10)

            self.reset_btn = QPushButton("Reset Form", self)
            self.reset_btn.clicked.connect(self.reset_form)
            actions_hbox.addWidget(self.reset_btn)

            actions_hbox.addStretch()

            self.deploy_btn = QPushButton("Deploy Game Package to Disk", self)
            self.deploy_btn.setObjectName("deployButton")
            self.deploy_btn.setFixedHeight(38)
            self.deploy_btn.clicked.connect(lambda: self.deploy(confirm_dialog=True))
            actions_hbox.addWidget(self.deploy_btn)

            main_layout.addLayout(actions_hbox)

            # Status bar
            self.statusBar = QStatusBar(self)
            self.setStatusBar(self.statusBar)
            self.statusBar.showMessage(f"Ready. Framework: {QT_BINDING}")

        def _apply_styling(self) -> None:
            """Apply clean, modern styling."""
            self.setStyleSheet("""
                QMainWindow {
                    background-color: #1e1e24;
                }
                QWidget {
                    color: #e0e0e0;
                    font-size: 10pt;
                }
                QGroupBox {
                    font-weight: bold;
                    border: 1px solid #3d3d4d;
                    border-radius: 6px;
                    margin-top: 12px;
                    padding-top: 14px;
                    background-color: #252530;
                }
                QGroupBox::title {
                    subcontrol-origin: margin;
                    subcontrol-position: top left;
                    left: 12px;
                    padding: 0 4px;
                    color: #7289da;
                }
                #headerBox {
                    background-color: #2b2b3a;
                    border: 1px solid #45455a;
                    border-radius: 6px;
                }
                #headerTitle {
                    font-size: 13pt;
                    color: #ffffff;
                }
                #headerSubtitle {
                    font-size: 9pt;
                    color: #a0a0b0;
                }
                QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {
                    background-color: #181820;
                    border: 1px solid #3d3d4d;
                    border-radius: 4px;
                    padding: 5px 8px;
                    color: #ffffff;
                }
                QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {
                    border: 1px solid #5865f2;
                }
                QPushButton {
                    background-color: #353545;
                    border: 1px solid #4a4a60;
                    border-radius: 4px;
                    padding: 6px 14px;
                    color: #ffffff;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #454558;
                    border-color: #686880;
                }
                QPushButton:pressed {
                    background-color: #282835;
                }
                #deployButton {
                    background-color: #2e7d32;
                    border: 1px solid #43a047;
                    font-size: 11pt;
                    padding: 6px 20px;
                }
                #deployButton:hover {
                    background-color: #388e3c;
                    border-color: #66bb6a;
                }
                #deployButton:pressed {
                    background-color: #1b5e20;
                }
                QScrollBar:vertical {
                    background: #181820;
                    width: 10px;
                    border-radius: 4px;
                }
                QScrollBar::handle:vertical {
                    background: #3d3d4d;
                    border-radius: 4px;
                }
                QStatusBar {
                    background: #181820;
                    color: #9e9ea8;
                }
            """)

        def _populate_drives(self) -> None:
            """Scan and populate system drives into dropdown."""
            self.drive_combo.blockSignals(True)
            self.drive_combo.clear()
            self.drive_combo.addItem("Select Drive...")
            drives = get_available_drives()
            for drv in drives:
                self.drive_combo.addItem(drv)
            self.drive_combo.blockSignals(False)

        def _on_drive_selected(self, index: int, *args: Any) -> None:
            if index > 0:
                selected_drive = self.drive_combo.itemText(index)
                self.target_dir_input.setText(selected_drive)

        def _on_title_changed(self, text: str = "", *args: Any) -> None:
            if not self._user_edited_id:
                slug = slugify_game_id(text) if text.strip() else ""
                self.game_id_input.blockSignals(True)
                self.game_id_input.setText(slug)
                self.game_id_input.blockSignals(False)

            if not self._user_edited_subdir:
                self.install_subdir_input.blockSignals(True)
                self.install_subdir_input.setText(text.strip())
                self.install_subdir_input.blockSignals(False)

            self.update_json_preview()

        def _on_id_changed(self, text: str = "", *args: Any) -> None:
            self._user_edited_id = bool(text.strip())
            self.update_json_preview()

        def _on_subdir_changed(self, text: str = "", *args: Any) -> None:
            self._user_edited_subdir = bool(text.strip())
            self.update_json_preview()

        def _on_exe_changed(self, text: str = "", *args: Any) -> None:
            if text and not self.launcher_exe_input.text().strip():
                self.launcher_exe_input.setText(Path(text).name)
            if text and not self.title_input.text().strip():
                stem = Path(text).stem
                self.title_input.setText(stem.replace("_", " ").title())
            self.update_json_preview()

        def _on_icon_changed(self, text: str = "", *args: Any) -> None:
            clean = text.strip()
            if clean and os.path.isfile(clean):
                pix = QPixmap(clean)
                if not pix.isNull():
                    scaled = pix.scaled(32, 32, Qt.AspectRatioMode.KeepAspectRatio if hasattr(Qt, "AspectRatioMode") else Qt.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation if hasattr(Qt, "TransformationMode") else Qt.SmoothTransformation)
                    self.icon_preview_lbl.setPixmap(scaled)
                else:
                    self.icon_preview_lbl.setText("?")
            else:
                self.icon_preview_lbl.clear()
            self.update_json_preview()

        def _on_browse_exe(self, *args: Any) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Select Game Executable or Setup Installer",
                "",
                "Executable Files (*.exe *.bat *.cmd *.msi);;All Files (*.*)",
            )
            if path:
                self.exe_path_input.setText(os.path.normpath(path))

        def _on_browse_icon(self, *args: Any) -> None:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Select Custom Game Icon",
                "",
                "Icon / Image Files (*.ico *.png);;ICO Files (*.ico);;PNG Files (*.png);;All Files (*.*)",
            )
            if path:
                self.icon_path_input.setText(os.path.normpath(path))

        def _on_browse_target(self, *args: Any) -> None:
            directory = QFileDialog.getExistingDirectory(
                self,
                "Select Target Disk Root or Output Directory",
                "",
            )
            if directory:
                self.target_dir_input.setText(os.path.normpath(directory))

        def collect_form_data(self) -> Dict[str, Any]:
            """Extract and return all current form values."""
            title = self.title_input.text().strip()
            game_id = self.game_id_input.text().strip() or slugify_game_id(title)
            version = self.version_input.text().strip() or "1.0.0"

            exe_path = self.exe_path_input.text().strip()
            icon_path = self.icon_path_input.text().strip()
            target_dir = self.target_dir_input.text().strip()

            setup_args = self.setup_args_input.text().strip()
            install_subdir = self.install_subdir_input.text().strip() or title
            size_mb = self.size_spin.value() if self.size_spin.value() > 0 else None
            silent_supported = self.silent_chk.isChecked()

            launcher_exe = self.launcher_exe_input.text().strip() or (Path(exe_path).name if exe_path else "game.exe")
            launcher_args = self.launcher_args_input.text().strip()
            working_dir = self.working_dir_input.text().strip()
            requires_admin = self.admin_chk.isChecked()

            publisher = self.publisher_input.text().strip()
            developer = self.developer_input.text().strip()

            disc_num = self.disc_num_spin.value()
            total_discs = self.total_discs_spin.value()

            return {
                "title": title,
                "game_id": game_id,
                "version": version,
                "source_executable": exe_path,
                "source_icon": icon_path if icon_path else None,
                "target_dir": target_dir,
                "setup_arguments": setup_args,
                "default_install_subdir": install_subdir,
                "estimated_size_mb": size_mb,
                "silent_supported": silent_supported,
                "launcher_executable": launcher_exe,
                "launcher_arguments": launcher_args,
                "working_directory": working_dir,
                "requires_admin": requires_admin,
                "publisher": publisher if publisher else None,
                "developer": developer if developer else None,
                "disk_number": disc_num,
                "total_disks": total_discs,
            }

        def update_json_preview(self, *args: Any) -> None:
            """Recompute and display formatted JSON preview."""
            try:
                data = self.collect_form_data()
                exe_name = Path(data["source_executable"]).name if data["source_executable"] else "setup.exe"
                icon_name = Path(data["source_icon"]).name if data["source_icon"] else None

                config_dict = build_gog_config_dict(
                    game_id=data["game_id"],
                    title=data["title"] or "Untitled Game",
                    version=data["version"],
                    setup_executable=exe_name,
                    setup_arguments=data["setup_arguments"],
                    default_install_subdir=data["default_install_subdir"],
                    estimated_size_mb=data["estimated_size_mb"],
                    silent_supported=data["silent_supported"],
                    launcher_executable=data["launcher_executable"],
                    launcher_arguments=data["launcher_arguments"],
                    working_directory=data["working_directory"],
                    requires_admin=data["requires_admin"],
                    icon_path=icon_name,
                    publisher=data["publisher"],
                    developer=data["developer"],
                    disk_number=data["disk_number"],
                    total_disks=data["total_disks"],
                )
                formatted = json.dumps(config_dict, indent=2, ensure_ascii=False)
                self.json_preview.setPlainText(formatted)
            except Exception as ex:
                self.json_preview.setPlainText(f"// Preview error: {ex}")

        def validate_form(self) -> Tuple[bool, List[str]]:
            """Validate required fields and files."""
            errors: List[str] = []
            data = self.collect_form_data()

            if not data["title"]:
                errors.append("Game Title is required.")
            if not data["source_executable"]:
                errors.append("Game Executable path is required.")
            elif not os.path.isfile(data["source_executable"]):
                errors.append(f"Game Executable file not found: '{data['source_executable']}'")

            if data["source_icon"] and not os.path.isfile(data["source_icon"]):
                errors.append(f"Custom Icon file not found: '{data['source_icon']}'")

            if not data["target_dir"]:
                errors.append("Target Directory / Drive is required.")
            elif os.path.isfile(data["target_dir"]):
                errors.append(f"Target Directory cannot be an existing file: '{data['target_dir']}'")

            return (len(errors) == 0, errors)

        def reset_form(self, *args: Any) -> None:
            """Clear inputs to defaults."""
            self.title_input.clear()
            self.game_id_input.clear()
            self.version_input.setText("1.0.0")
            self.publisher_input.clear()
            self.developer_input.clear()
            self.exe_path_input.clear()
            self.icon_path_input.clear()
            self.icon_preview_lbl.clear()
            self.setup_args_input.clear()
            self.install_subdir_input.clear()
            self.size_spin.setValue(0)
            self.silent_chk.setChecked(False)
            self.launcher_exe_input.clear()
            self.launcher_args_input.clear()
            self.working_dir_input.clear()
            self.admin_chk.setChecked(False)
            self.target_dir_input.clear()
            self.drive_combo.setCurrentIndex(0)
            self.disc_num_spin.setValue(1)
            self.total_discs_spin.setValue(1)
            self._user_edited_id = False
            self._user_edited_subdir = False
            self.update_json_preview()
            self.statusBar.showMessage("Form reset to default values.")

        def deploy(self, confirm_dialog: bool = True) -> DeploymentResult:
            """
            Execute disk deployment.

            Args:
                confirm_dialog: If True, presents modal confirmation dialog before proceeding.

            Returns:
                DeploymentResult instance.
            """
            valid, errors = self.validate_form()
            if not valid:
                error_msg = "\n".join(f"• {e}" for e in errors)
                if confirm_dialog:
                    QMessageBox.warning(
                        self,
                        "Validation Errors",
                        f"Please resolve the following issues before deploying:\n\n{error_msg}",
                    )
                self.statusBar.showMessage("Deployment halted: validation errors.")
                return DeploymentResult(
                    success=False,
                    target_dir=self.target_dir_input.text().strip(),
                    message="Validation failed.",
                    errors=errors,
                )

            data = self.collect_form_data()

            if confirm_dialog:
                esc_title = html.escape(str(data['title']))
                esc_id = html.escape(str(data['game_id']))
                esc_ver = html.escape(str(data['version']))
                esc_src_exe = html.escape(str(data['source_executable']))
                esc_src_ico = html.escape(str(data['source_icon'] or '(None)'))
                esc_tgt = html.escape(str(data['target_dir']))
                esc_exe_name = html.escape(Path(data['source_executable']).name)
                esc_ico_name = html.escape(Path(data['source_icon']).name) if data["source_icon"] else ""

                summary = (
                    f"<b>Ready to deploy GOG Game Package:</b><br><br>"
                    f"<b>Title:</b> {esc_title}<br>"
                    f"<b>Game ID:</b> {esc_id}<br>"
                    f"<b>Version:</b> {esc_ver}<br>"
                    f"<b>Source Executable:</b> {esc_src_exe}<br>"
                    f"<b>Source Icon:</b> {esc_src_ico}<br>"
                    f"<b>Target Destination:</b> {esc_tgt}<br><br>"
                    f"Files to write/copy:<br>"
                    f" • <code>gog_game.json</code><br>"
                    f" • <code>{esc_exe_name}</code><br>"
                    + (f" • <code>{esc_ico_name}</code><br>" if data["source_icon"] else "")
                    + "<br>Proceed with deployment?"
                )
                reply = QMessageBox.question(
                    self,
                    "Confirm Package Deployment",
                    summary,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    if hasattr(QMessageBox, "StandardButton")
                    else QMessageBox.Yes | QMessageBox.No,
                )
                yes_val = (
                    QMessageBox.StandardButton.Yes
                    if hasattr(QMessageBox, "StandardButton")
                    else QMessageBox.Yes
                )
                if reply != yes_val:
                    self.statusBar.showMessage("Deployment cancelled by user.")
                    return DeploymentResult(
                        success=False,
                        target_dir=data["target_dir"],
                        message="Cancelled by user.",
                    )

            self.statusBar.showMessage("Deploying files to target disk...")

            result = deploy_game_disk(
                source_executable=data["source_executable"],
                target_dir=data["target_dir"],
                title=data["title"],
                game_id=data["game_id"],
                version=data["version"],
                source_icon=data["source_icon"],
                setup_arguments=data["setup_arguments"],
                default_install_subdir=data["default_install_subdir"],
                estimated_size_mb=data["estimated_size_mb"],
                silent_supported=data["silent_supported"],
                launcher_executable=data["launcher_executable"],
                launcher_arguments=data["launcher_arguments"],
                working_directory=data["working_directory"],
                requires_admin=data["requires_admin"],
                publisher=data["publisher"],
                developer=data["developer"],
                disk_number=data["disk_number"],
                total_disks=data["total_disks"],
            )

            self.deployment_completed.emit(result)

            if result.success:
                self.statusBar.showMessage(f"✓ Deployed successfully: {result.target_dir}")
                if confirm_dialog:
                    esc_res_tgt = html.escape(str(result.target_dir))
                    esc_res_cfg = html.escape(Path(result.config_path).name if result.config_path else "gog_game.json")
                    esc_res_exe = html.escape(Path(result.executable_path).name if result.executable_path else "")
                    esc_res_ico = html.escape(Path(result.icon_path).name) if result.icon_path else ""
                    QMessageBox.information(
                        self,
                        "Deployment Complete",
                        f"<b>Package deployed successfully!</b><br><br>"
                        f"Target Directory: <code>{esc_res_tgt}</code><br>"
                        f"Descriptor: <code>{esc_res_cfg}</code><br>"
                        f"Executable: <code>{esc_res_exe}</code><br>"
                        + (f"Icon: <code>{esc_res_ico}</code><br>" if result.icon_path else "")
                        + "<br>The disk is now ready for GOG Game Disk Monitor auto-detection.",
                    )
            else:
                self.statusBar.showMessage("✗ Deployment failed.")
                if confirm_dialog:
                    QMessageBox.critical(
                        self,
                        "Deployment Failed",
                        f"An error occurred during deployment:\n\n{result.message}\n"
                        + "\n".join(result.errors),
                    )

            return result


# ---------------------------------------------------------------------------
# CLI / Main Entry Point
# ---------------------------------------------------------------------------

def main(args: Optional[Sequence[str]] = None) -> int:
    """
    Main entry point for GUI_setup application.

    Args:
        args: Command line argument sequence.

    Returns:
        Process exit code (0 for success).
    """
    if args is None:
        args = sys.argv[1:]

    # Help / Info flag handling
    if "--help" in args or "-h" in args:
        print("GOG Game Disk Generator & Setup Utility")
        print("Usage: python GUI_setup.py [OPTIONS]")
        print("")
        print("Options:")
        print("  -h, --help            Show this help message and exit.")
        print("  --offscreen           Run Qt in offscreen/headless mode.")
        print("  --version             Print version information.")
        return 0

    if "--version" in args:
        print("GOG Game Disk Generator 1.0.0 (Qt binding: %s)" % (QT_BINDING or "None"))
        return 0

    if QT_BINDING is None:
        sys.stderr.write(
            "Error: Neither PyQt6, PySide6, nor PyQt5 could be imported.\n"
            "Please install PyQt6 with: pip install PyQt6\n"
        )
        return 1

    # Offscreen flag support for headless environments
    qt_args = [sys.argv[0]]
    if "--offscreen" in args or os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        qt_args.extend(["-platform", "offscreen"])

    app = QApplication.instance()
    if app is None:
        app = QApplication(qt_args)

    window = DiskSetupWindow()
    window.show()

    # If offscreen flag was passed via args, close immediately after opening for test verification
    if "--offscreen" in args:
        window.close()
        return 0

    return app.exec() if hasattr(app, "exec") else app.exec_()


if __name__ == "__main__":
    sys.exit(main())
