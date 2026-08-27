"""
gog_disk_monitor.config
~~~~~~~~~~~~~~~~~~~~~~~

Configuration models, parser, and icon resolution for GOG Game Disk Monitor.
Parses gog_game.json / gog_disk.json descriptors from removable/mounted media,
validates schema contracts, and resolves game icons with fallback chains.
"""

from dataclasses import dataclass, field
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Union

logger = logging.getLogger("gog_disk_monitor.config")


def _strip_quotes(s: str) -> str:
    """Helper to strip any matched outer quotes or escaped quotes repeatedly."""
    s = s.strip()
    while True:
        if len(s) >= 4 and (
            (s.startswith('\\"') and s.endswith('\\"'))
            or (s.startswith("\\'") and s.endswith("\\'"))
        ):
            s = s[2:-2].strip()
        elif len(s) >= 2 and (
            (s.startswith('"') and s.endswith('"'))
            or (s.startswith("'") and s.endswith("'"))
        ):
            s = s[1:-1].strip()
        else:
            break
    return s


def sanitize_argument(arg: Any) -> str:
    """
    Sanitizes a command-line argument by stripping extraneous quotes from
    enclosing strings and key=value options (e.g. /dir="C:\\Path" -> /dir=C:\\Path).

    On Windows, passing literal quotes inside arguments like '/dir="C:\\Path"'
    causes subprocess (via list2cmdline) to escape quotes as '\\"', which installer
    parsers (such as Inno Setup) interpret as literal invalid path characters,
    causing exit code 2 (invalid character in path).

    Args:
        arg: Raw argument string or value.

    Returns:
        Sanitized argument string.
    """
    if arg is None or isinstance(arg, bool):
        return ""
    if not isinstance(arg, str):
        arg = str(arg)

    arg = _strip_quotes(arg)
    if not arg:
        return ""

    # Check for key=value or key:value parameter (e.g. /dir="C:\Path", --prefix='C:\Games', -D="val")
    for sep in ("=", ":"):
        if sep in arg:
            key, s, val = arg.partition(sep)
            val = _strip_quotes(val)
            return f"{key}{s}{val}"

    return arg


def parse_and_sanitize_arguments(args_input: Union[str, Sequence[Any], Any, None]) -> List[str]:
    """
    Parses and sanitizes command-line arguments from a list, tuple, string, or None.

    If given a string (e.g. '/dir="C:\\Path" /SILENT'), splits it into individual
    tokens while respecting quoted sub-strings, and sanitizes each token.
    If given a list or sequence, sanitizes each argument in the sequence.

    Args:
        args_input: Raw arguments as list, sequence, string, or None.

    Returns:
        List of sanitized argument strings.
    """
    if args_input is None or isinstance(args_input, bool):
        return []

    if isinstance(args_input, (list, tuple, set)):
        result: List[str] = []
        for item in args_input:
            if item is None or isinstance(item, bool):
                continue
            sanitized = sanitize_argument(item)
            if sanitized:
                result.append(sanitized)
        return result

    if isinstance(args_input, (int, float)):
        return [str(args_input)]

    if isinstance(args_input, str):
        cleaned = args_input.strip()
        if not cleaned:
            return []
        pattern = r"""(?:[^\s"']|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')+"""
        matches = re.findall(pattern, cleaned)
        if matches:
            return [sanitize_argument(m) for m in matches if sanitize_argument(m)]
        sanitized = sanitize_argument(cleaned)
        return [sanitized] if sanitized else []

    return []

# Standard configuration descriptor filenames in order of preference
CONFIG_FILENAMES: Sequence[str] = (
    "gog_game.json",
    "gog_disk.json",
    "GOG_GAME.JSON",
    "GOG_DISK.JSON",
)

# Standard icon filenames in order of priority
DEFAULT_ICON_FILENAMES: Sequence[str] = (
    "autorun.ico",
    "icon.ico",
    "game.ico",
    "gog_icon.ico",
    "icon.png",
    "game.png",
    "gog_icon.png",
    "autorun.png",
)


@dataclass
class SetupConfig:
    """
    Configuration for installing a game from disk.

    Attributes:
        executable: Relative path from disk root to setup installer.
        arguments: Command-line arguments passed to setup executable.
        default_install_subdir: Suggested destination folder name under install root.
        estimated_size_mb: Estimated required disk space in megabytes.
        silent_supported: Whether unattended installation is supported.
    """
    executable: str
    arguments: List[str] = field(default_factory=list)
    default_install_subdir: Optional[str] = None
    estimated_size_mb: Optional[int] = None
    silent_supported: bool = False


@dataclass
class LauncherConfig:
    """
    Configuration for launching an installed game.

    Attributes:
        executable: Relative path from install directory to game executable.
        arguments: Command-line arguments passed when launching game.
        working_directory: Working directory relative to install folder.
        requires_admin: Whether UAC elevation is required.
    """
    executable: str
    arguments: List[str] = field(default_factory=list)
    working_directory: Optional[str] = None
    requires_admin: bool = False


@dataclass
class GOGDiskConfig:
    """
    Descriptor metadata parsed from a GOG game disk.

    Attributes:
        game_id: Unique identifier for state store indexing.
        title: Human-readable game title.
        version: Game release/patch version.
        setup: Nested SetupConfig instance.
        launcher: Nested LauncherConfig instance.
        icon_path: Disk-relative path to custom icon, if specified.
        publisher: Game publisher name.
        developer: Game developer name.
        disk_info: Multi-disc or volume metadata.
        schema_version: Schema specification version.
        raw_data: Original unparsed dictionary for forward compatibility.
    """
    game_id: str
    title: str
    version: str
    setup: SetupConfig
    launcher: LauncherConfig
    icon_path: Optional[str] = None
    publisher: Optional[str] = None
    developer: Optional[str] = None
    disk_info: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = "1.0"
    raw_data: Dict[str, Any] = field(default_factory=dict, repr=False)


def normalize_disk_root(disk_root: str) -> str:
    """
    Normalize a disk root path across Windows drive letters and directory paths.

    Args:
        disk_root: Raw drive string (e.g. 'X:', 'X:\\', 'D:/', '/media/cdrom').

    Returns:
        Normalized absolute path with proper directory separator.
    """
    if not disk_root or not isinstance(disk_root, str):
        return ""
    root = disk_root.strip()
    if not root:
        return ""

    # Handle single drive letters like 'X:' or 'x:'
    if len(root) == 2 and root[1] == ":" and root[0].isalpha():
        return root.upper() + os.sep

    # If drive letter with slashes like 'X:\' or 'X:/'
    if len(root) >= 3 and root[1] == ":" and root[0].isalpha():
        drive = root[:2].upper()
        rest = root[2:].lstrip("/\\")
        if not rest:
            return drive + os.sep
        return os.path.abspath(drive + os.sep + rest)

    return os.path.abspath(root)


def _find_case_insensitive_entry(parent_dir: str, target_name: str) -> Optional[str]:
    """
    Finds a file or directory inside parent_dir matching target_name case-insensitively.

    Args:
        parent_dir: Directory to scan.
        target_name: Name of file or subdirectory to find.

    Returns:
        Exact case-matched entry name if found, otherwise None.
    """
    if not os.path.isdir(parent_dir):
        return None

    target_lower = target_name.lower()
    try:
        entries = os.listdir(parent_dir)
    except OSError:
        return None

    for entry in entries:
        if entry.lower() == target_lower:
            return entry
    return None


def _resolve_case_insensitive_path(base_dir: str, rel_path: str) -> Optional[str]:
    """
    Resolves a relative (or absolute) path case-insensitively to its exact on-disk casing.

    Args:
        base_dir: Base directory path.
        rel_path: Relative or absolute file path.

    Returns:
        Absolute resolved path with exact on-disk casing if it exists as a file, otherwise None.
    """
    if not rel_path or not isinstance(rel_path, str):
        return None

    if os.path.isabs(rel_path):
        drive, rest = os.path.splitdrive(rel_path)
        if drive:
            current_dir = drive + os.sep
        else:
            current_dir = os.sep
        parts = os.path.normpath(rest).replace("\\", "/").split("/")
    else:
        if not base_dir or not os.path.isdir(base_dir):
            return None
        current_dir = base_dir
        parts = os.path.normpath(rel_path).replace("\\", "/").split("/")

    for part in parts:
        if not part or part == ".":
            continue
        if part == "..":
            current_dir = os.path.dirname(current_dir)
            continue
        matched_entry = _find_case_insensitive_entry(current_dir, part)
        if not matched_entry:
            return None
        current_dir = os.path.join(current_dir, matched_entry)

    if os.path.isfile(current_dir):
        return os.path.abspath(current_dir)
    return None


def parse_disk_config(disk_root: str) -> Optional[GOGDiskConfig]:
    """
    Search for and parse a GOG game configuration descriptor from a disk root.

    Args:
        disk_root: Root path of the disk/drive (e.g. 'D:\\' or '/tmp/mock_disk').

    Returns:
        GOGDiskConfig instance if a valid config is found and validated, else None.
    """
    try:
        norm_root = normalize_disk_root(disk_root)
        if not norm_root or not os.path.isdir(norm_root):
            logger.debug("Disk root is not an accessible directory: %s", disk_root)
            return None

        # 1. Search for configuration descriptor file
        config_path: Optional[str] = None
        for filename in CONFIG_FILENAMES:
            resolved = _resolve_case_insensitive_path(norm_root, filename)
            if resolved and os.path.isfile(resolved):
                config_path = resolved
                break

        if not config_path:
            logger.debug("No GOG game descriptor found in root: %s", norm_root)
            return None

        # 2. Read and parse JSON content (handling UTF-8 BOM transparently)
        try:
            with open(config_path, "r", encoding="utf-8-sig") as f:
                content = f.read().strip()
            if not content:
                logger.debug("Config file is empty: %s", config_path)
                return None
            data = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            logger.debug("Failed to read/decode JSON config at %s: %s", config_path, e)
            return None

        if not isinstance(data, dict):
            logger.debug("Invalid config at %s: root must be a JSON object", config_path)
            return None

        # 3. Validate mandatory field: game_id
        game_id_raw = data.get("game_id")
        if not isinstance(game_id_raw, str) or not game_id_raw.strip():
            logger.debug("Config missing or invalid 'game_id' at %s", config_path)
            return None
        game_id = game_id_raw.strip()

        # 4. Validate mandatory field: title
        title_raw = data.get("title")
        if not isinstance(title_raw, str) or not title_raw.strip():
            logger.debug("Config missing or invalid 'title' at %s", config_path)
            return None
        title = title_raw.strip()

        # 5. Validate mandatory field: version
        version_raw = data.get("version")
        if not isinstance(version_raw, str) or not version_raw.strip():
            logger.debug("Config missing or invalid 'version' at %s", config_path)
            return None
        version = version_raw.strip()

        # 6. Validate mandatory section: setup
        setup_data = data.get("setup")
        if not isinstance(setup_data, dict):
            logger.debug("Config missing or invalid 'setup' section at %s", config_path)
            return None

        setup_exe_raw = setup_data.get("executable")
        if not isinstance(setup_exe_raw, str) or not setup_exe_raw.strip():
            logger.debug("Setup missing or invalid 'executable' at %s", config_path)
            return None
        setup_exe = os.path.normpath(setup_exe_raw.strip())

        # Setup arguments (support 'arguments' or 'args')
        setup_args_raw = setup_data.get("arguments", setup_data.get("args"))
        if setup_args_raw is None:
            setup_args = []
        elif isinstance(setup_args_raw, (list, str)):
            setup_args = parse_and_sanitize_arguments(setup_args_raw)
        else:
            logger.debug("Setup 'arguments' has invalid type at %s", config_path)
            return None

        setup_subdir = setup_data.get("default_install_subdir")
        if setup_subdir is not None:
            setup_subdir = str(setup_subdir).strip()

        setup_size_raw = setup_data.get("estimated_size_mb")
        setup_size: Optional[int] = None
        if setup_size_raw is not None:
            try:
                setup_size = int(setup_size_raw)
            except (ValueError, TypeError):
                logger.debug("Setup 'estimated_size_mb' invalid value at %s", config_path)
                return None

        silent_supported = bool(setup_data.get("silent_supported", False))

        setup_config = SetupConfig(
            executable=setup_exe,
            arguments=setup_args,
            default_install_subdir=setup_subdir,
            estimated_size_mb=setup_size,
            silent_supported=silent_supported,
        )

        # 7. Validate mandatory section: launcher
        launcher_data = data.get("launcher")
        if not isinstance(launcher_data, dict):
            logger.debug("Config missing or invalid 'launcher' section at %s", config_path)
            return None

        launcher_exe_raw = launcher_data.get("executable")
        if not isinstance(launcher_exe_raw, str) or not launcher_exe_raw.strip():
            logger.debug("Launcher missing or invalid 'executable' at %s", config_path)
            return None
        launcher_exe = os.path.normpath(launcher_exe_raw.strip())

        # Launcher arguments (support 'arguments' or 'args')
        launcher_args_raw = launcher_data.get("arguments", launcher_data.get("args"))
        if launcher_args_raw is None:
            launcher_args = []
        elif isinstance(launcher_args_raw, (list, str)):
            launcher_args = parse_and_sanitize_arguments(launcher_args_raw)
        else:
            logger.debug("Launcher 'arguments' has invalid type at %s", config_path)
            return None

        launcher_workdir_raw = launcher_data.get("working_directory", launcher_data.get("working_dir"))
        launcher_workdir: Optional[str] = None
        if launcher_workdir_raw is not None:
            launcher_workdir = os.path.normpath(str(launcher_workdir_raw).strip())

        requires_admin = bool(launcher_data.get("requires_admin", False))

        launcher_config = LauncherConfig(
            executable=launcher_exe,
            arguments=launcher_args,
            working_directory=launcher_workdir,
            requires_admin=requires_admin,
        )

        # 8. Parse optional top-level metadata
        icon_path_raw = data.get("icon_path")
        icon_path: Optional[str] = None
        if icon_path_raw is not None and isinstance(icon_path_raw, str) and icon_path_raw.strip():
            icon_path = os.path.normpath(icon_path_raw.strip())

        publisher = str(data["publisher"]).strip() if data.get("publisher") is not None else None
        developer = str(data["developer"]).strip() if data.get("developer") is not None else None

        disk_info_raw = data.get("disk_info")
        disk_info = dict(disk_info_raw) if isinstance(disk_info_raw, dict) else {}

        schema_version = str(data.get("schema_version", "1.0")).strip()

        return GOGDiskConfig(
            game_id=game_id,
            title=title,
            version=version,
            setup=setup_config,
            launcher=launcher_config,
            icon_path=icon_path,
            publisher=publisher,
            developer=developer,
            disk_info=disk_info,
            schema_version=schema_version,
            raw_data=data,
        )

    except Exception as e:
        logger.debug("Unexpected error while parsing config from %s: %s", disk_root, e)
        return None


def find_disk_icon(disk_root: str, config: Optional[GOGDiskConfig] = None) -> Optional[str]:
    """
    Locate an icon file on the disk volume using priority-based resolution.

    Priority hierarchy:
      1. Configured icon_path (if config provided and file exists on disk)
      2. Root default icons: autorun.ico, icon.ico, game.ico, gog_icon.ico, icon.png, etc.
      3. Setup folder icons (adjacent to setup executable)

    Args:
        disk_root: Root path of the disk/drive.
        config: Optional parsed GOGDiskConfig instance.

    Returns:
        Absolute path to resolved icon file if found, otherwise None.
    """
    try:
        norm_root = normalize_disk_root(disk_root)
        if not norm_root or not os.path.isdir(norm_root):
            return None

        # 1. Configured icon path
        if config and config.icon_path:
            resolved = _resolve_case_insensitive_path(norm_root, config.icon_path)
            if resolved and os.path.isfile(resolved):
                return resolved

        # 2. Standard root filenames in priority order
        for icon_name in DEFAULT_ICON_FILENAMES:
            resolved = _resolve_case_insensitive_path(norm_root, icon_name)
            if resolved and os.path.isfile(resolved):
                return resolved

        # 3. Setup-adjacent icon
        if config and config.setup and config.setup.executable:
            setup_rel_dir = os.path.dirname(config.setup.executable)
            if setup_rel_dir:
                setup_abs_dir = os.path.join(norm_root, setup_rel_dir)
                if os.path.isdir(setup_abs_dir):
                    for icon_name in DEFAULT_ICON_FILENAMES:
                        resolved = _resolve_case_insensitive_path(setup_abs_dir, icon_name)
                        if resolved and os.path.isfile(resolved):
                            return resolved

        return None
    except Exception as e:
        logger.debug("Error while resolving icon for %s: %s", disk_root, e)
        return None
