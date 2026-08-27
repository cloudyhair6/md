"""
gog_disk_monitor.launcher
~~~~~~~~~~~~~~~~~~~~~~~~~

Process execution manager for GOG Game Disk Monitor.
Handles executing game installer setups (tracking execution and exit codes)
and launching installed game executables (detached processes with Windows
process group isolation).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Union

logger = logging.getLogger("gog_disk_monitor.launcher")

# Windows Process Creation Flags
# DETACHED_PROCESS (0x00000008): Process has no console window and is detached from parent.
# CREATE_NEW_PROCESS_GROUP (0x00000200): Process is the root of a new process group.
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


class ProcessExecutionError(Exception):
    """Raised when an executed process exits with a non-zero error status code."""

    def __init__(self, message: str, exit_code: int, cmd: Sequence[str]):
        super().__init__(message)
        self.exit_code = exit_code
        self.cmd = list(cmd)


class ProcessRunner:
    """
    Subprocess execution manager for game installers and launched game binaries.

    Provides:
      - `run_setup`: Synchronous execution of installer executable with exit code tracking.
      - `launch_game`: Asynchronous/detached execution of game binary independent of monitor lifecycle.
      - Robust cross-platform error handling (FileNotFoundError, PermissionError, timeout).
    """

    @staticmethod
    def _is_windows_batch_file(path: str) -> bool:
        """Determines if the given executable path is a Windows batch/cmd script."""
        ext = os.path.splitext(path)[1].lower()
        return ext in (".bat", ".cmd")

    @staticmethod
    def _resolve_executable_path(
        executable_path: str, cwd: Optional[str] = None
    ) -> str:
        """
        Resolves an executable path against cwd or disk root, returning normalized absolute path.

        Args:
            executable_path: Path to executable (relative or absolute).
            cwd: Optional working directory base.

        Returns:
            Resolved absolute path string.

        Raises:
            ValueError: If executable_path is empty.
            FileNotFoundError: If the target file does not exist.
        """
        if not executable_path or not str(executable_path).strip():
            raise ValueError("Executable path must not be empty.")

        raw_path = str(executable_path).strip()

        # If relative and cwd is provided, check relative to cwd first
        if not os.path.isabs(raw_path) and cwd:
            combined = os.path.join(cwd, raw_path)
            if os.path.exists(combined):
                return os.path.abspath(combined)

        # Direct path check
        abs_path = os.path.abspath(raw_path)
        if os.path.exists(abs_path):
            return abs_path

        # If not found directly, check if cwd combined exists
        if cwd:
            combined = os.path.abspath(os.path.join(cwd, raw_path))
            if os.path.exists(combined):
                return combined

        raise FileNotFoundError(f"Executable not found at: {raw_path}")

    @staticmethod
    def is_executable_valid(executable_path: str, cwd: Optional[str] = None) -> bool:
        """
        Checks whether an executable file exists and is a valid file.

        Args:
            executable_path: Path to executable.
            cwd: Optional working directory for relative resolution.

        Returns:
            True if file exists and is a file, False otherwise.
        """
        try:
            resolved = ProcessRunner._resolve_executable_path(executable_path, cwd=cwd)
            return os.path.isfile(resolved)
        except (ValueError, FileNotFoundError, OSError):
            return False

    @classmethod
    def sanitize_argument(cls, arg: Any) -> str:
        """
        Sanitizes an individual argument string, stripping extraneous quotes from
        enclosing strings and key=value switches (e.g. /dir="C:\\Path" -> /dir=C:\\Path)
        so that Windows subprocess execution passes clean paths without literal quotes.
        """
        from .config import sanitize_argument
        return sanitize_argument(arg)

    @classmethod
    def run_setup(
        cls,
        setup_exe_path: str,
        args: Optional[Sequence[str]] = None,
        cwd: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        env: Optional[Dict[str, str]] = None,
        shell: Optional[bool] = None,
        check: bool = False,
    ) -> int:
        """
        Launches an installer/setup executable, tracks its execution, and returns the exit code.

        Args:
            setup_exe_path: Path to setup installer (e.g. 'D:\\setup.exe' or 'setup.bat').
            args: Optional command-line arguments list to pass to setup.
            cwd: Working directory (defaults to directory containing setup_exe_path).
            timeout_seconds: Maximum duration in seconds to wait for setup to finish.
            env: Optional environment variables dictionary.
            shell: Explicit shell execution override (auto-detected if None).
            check: If True and exit code != 0, raises ProcessExecutionError.

        Returns:
            Process exit code integer (0 indicates success).

        Raises:
            ValueError: If setup_exe_path is empty.
            FileNotFoundError: If setup executable does not exist.
            PermissionError: If execution permissions are denied.
            subprocess.TimeoutExpired: If setup execution exceeds timeout_seconds.
            ProcessExecutionError: If check=True and process returns non-zero code.
        """
        resolved_path = cls._resolve_executable_path(setup_exe_path, cwd=cwd)
        if os.path.isdir(resolved_path):
            raise FileNotFoundError(
                f"Setup path points to a directory, not an executable file: {resolved_path}"
            )

        effective_cwd = cwd or os.path.dirname(resolved_path)
        if not effective_cwd or not os.path.isdir(effective_cwd):
            effective_cwd = os.path.dirname(resolved_path)

        cmd_args = [cls.sanitize_argument(a) for a in (args or []) if a is not None]

        # Windows batch script (.bat/.cmd) execution handling
        is_batch = cls._is_windows_batch_file(resolved_path)
        use_shell = shell if shell is not None else False

        if sys.platform == "win32":
            if is_batch and not use_shell:
                # Execute batch script via cmd.exe /c to avoid shell quoting vulnerabilities
                comspec = os.environ.get("COMSPEC", "cmd.exe")
                full_cmd = [comspec, "/c", resolved_path] + cmd_args
            else:
                full_cmd = [resolved_path] + cmd_args
            creationflags = CREATE_NEW_PROCESS_GROUP
        else:
            full_cmd = [resolved_path] + cmd_args
            creationflags = 0

        logger.info(
            "Running setup executable: %s (cwd: %s, args: %s, timeout: %s)",
            resolved_path,
            effective_cwd,
            cmd_args,
            timeout_seconds,
        )

        try:
            proc = subprocess.Popen(
                full_cmd,
                cwd=effective_cwd,
                creationflags=creationflags,
                env=env,
                shell=use_shell,
            )

            exit_code = proc.wait(timeout=timeout_seconds)
            logger.info(
                "Setup executable '%s' completed with exit code %d.",
                resolved_path,
                exit_code,
            )

            if check and exit_code != 0:
                raise ProcessExecutionError(
                    f"Setup process failed with exit code {exit_code}: {resolved_path}",
                    exit_code=exit_code,
                    cmd=full_cmd,
                )

            return exit_code

        except subprocess.TimeoutExpired:
            logger.error(
                "Setup executable '%s' timed out after %s seconds. Terminating process...",
                resolved_path,
                timeout_seconds,
            )
            cls.terminate_process(proc, timeout=3.0)
            raise

        except PermissionError as ex:
            logger.error("Permission denied when launching setup '%s': %s", resolved_path, ex)
            raise

        except FileNotFoundError as ex:
            logger.error("File not found when launching setup '%s': %s", resolved_path, ex)
            raise

        except Exception as ex:
            logger.error("Unexpected error executing setup '%s': %s", resolved_path, ex)
            raise

    @classmethod
    def launch_game(
        cls,
        game_exe_path: str,
        args: Optional[Sequence[str]] = None,
        cwd: Optional[str] = None,
        detached: bool = True,
        env: Optional[Dict[str, str]] = None,
        shell: Optional[bool] = None,
    ) -> subprocess.Popen:
        """
        Launches an installed game binary detached from the monitor process lifecycle.

        On Windows with detached=True, uses DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        with closed standard file descriptors, so the game continues running even if
        the disk monitor is restarted or exited.

        Args:
            game_exe_path: Full or relative path to game executable binary.
            args: Optional command-line arguments to pass when launching.
            cwd: Working directory (defaults to folder containing game_exe_path).
            detached: Whether to spawn process detached from monitor process.
            env: Optional environment variables dictionary.
            shell: Explicit shell execution override (auto-detected if None).

        Returns:
            subprocess.Popen instance for the launched process.

        Raises:
            ValueError: If game_exe_path is empty.
            FileNotFoundError: If game binary does not exist.
            PermissionError: If execution permissions are denied.
            OSError: If process creation fails.
        """
        resolved_path = cls._resolve_executable_path(game_exe_path, cwd=cwd)
        if os.path.isdir(resolved_path):
            raise FileNotFoundError(
                f"Game executable path points to a directory: {resolved_path}"
            )

        effective_cwd = cwd or os.path.dirname(resolved_path)
        if not effective_cwd or not os.path.isdir(effective_cwd):
            effective_cwd = os.path.dirname(resolved_path)

        cmd_args = [cls.sanitize_argument(a) for a in (args or []) if a is not None]
        is_batch = cls._is_windows_batch_file(resolved_path)
        use_shell = shell if shell is not None else False

        creationflags = 0
        popen_kwargs: Dict[str, Any] = {
            "cwd": effective_cwd,
            "env": env,
            "shell": use_shell,
        }

        if sys.platform == "win32":
            if is_batch and not use_shell:
                comspec = os.environ.get("COMSPEC", "cmd.exe")
                full_cmd = [comspec, "/c", resolved_path] + cmd_args
            else:
                full_cmd = [resolved_path] + cmd_args

            if detached:
                creationflags |= (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
                popen_kwargs["close_fds"] = True
                popen_kwargs["stdin"] = subprocess.DEVNULL
                popen_kwargs["stdout"] = subprocess.DEVNULL
                popen_kwargs["stderr"] = subprocess.DEVNULL
            else:
                creationflags |= CREATE_NEW_PROCESS_GROUP

            popen_kwargs["creationflags"] = creationflags
        else:
            full_cmd = [resolved_path] + cmd_args
            if detached:
                popen_kwargs["start_new_session"] = True
                popen_kwargs["stdin"] = subprocess.DEVNULL
                popen_kwargs["stdout"] = subprocess.DEVNULL
                popen_kwargs["stderr"] = subprocess.DEVNULL

        logger.info(
            "Launching game executable: %s (cwd: %s, args: %s, detached: %s)",
            resolved_path,
            effective_cwd,
            cmd_args,
            detached,
        )

        try:
            proc = subprocess.Popen(full_cmd, **popen_kwargs)
            logger.info(
                "Game process launched successfully (PID: %d, detached: %s).",
                proc.pid,
                detached,
            )
            return proc

        except PermissionError as ex:
            logger.error("Permission denied when launching game '%s': %s", resolved_path, ex)
            raise

        except FileNotFoundError as ex:
            logger.error("File not found when launching game '%s': %s", resolved_path, ex)
            raise

        except Exception as ex:
            logger.error("Unexpected error launching game '%s': %s", resolved_path, ex)
            raise

    @staticmethod
    def terminate_process(proc: subprocess.Popen, timeout: float = 3.0) -> bool:
        """
        Gracefully terminates a running subprocess, falling back to kill if it does not exit.

        Args:
            proc: subprocess.Popen instance.
            timeout: Maximum seconds to wait after terminate() before calling kill().

        Returns:
            True if process exited cleanly, False if forced kill or error.
        """
        if proc.poll() is not None:
            return True

        try:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
                return True
            except subprocess.TimeoutExpired:
                logger.warning("Process PID %d did not terminate in time. Forcing kill...", proc.pid)
                proc.kill()
                proc.wait(timeout=2.0)
                return True
        except Exception as ex:
            logger.debug("Error while terminating process PID %d: %s", getattr(proc, "pid", 0), ex)
            return False
