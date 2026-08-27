"""
tests.test_launcher
~~~~~~~~~~~~~~~~~~~

Comprehensive unit tests for gog_disk_monitor.launcher.ProcessRunner.
Tests executable resolution, synchronous setup execution, timeout handling,
non-zero exit codes, detached game launching, and process lifecycle termination.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from gog_disk_monitor.launcher import (
    ProcessExecutionError,
    ProcessRunner,
)


class TestProcessRunnerPathResolution(unittest.TestCase):
    """Tests for ProcessRunner._resolve_executable_path and is_executable_valid."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.sample_file = self.base_path / "sample_tool.exe"
        self.sample_file.write_text("mock executable content", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_resolve_absolute_existing_path(self):
        """Absolute existing path resolves to normalized absolute path."""
        resolved = ProcessRunner._resolve_executable_path(str(self.sample_file))
        self.assertEqual(resolved, str(self.sample_file.resolve()))

    def test_resolve_relative_path_with_cwd(self):
        """Relative path resolves against cwd directory."""
        resolved = ProcessRunner._resolve_executable_path(
            "sample_tool.exe", cwd=str(self.base_path)
        )
        self.assertEqual(resolved, str(self.sample_file.resolve()))

    def test_resolve_empty_path_raises_value_error(self):
        """Empty string or whitespace-only path raises ValueError."""
        with self.assertRaises(ValueError):
            ProcessRunner._resolve_executable_path("")
        with self.assertRaises(ValueError):
            ProcessRunner._resolve_executable_path("   ")

    def test_resolve_nonexistent_path_raises_file_not_found(self):
        """Non-existent executable raises FileNotFoundError."""
        with self.assertRaises(FileNotFoundError):
            ProcessRunner._resolve_executable_path("definitely_nonexistent_binary_xyz.exe")

    def test_is_executable_valid(self):
        """is_executable_valid returns True for real files and False for missing/directories."""
        self.assertTrue(ProcessRunner.is_executable_valid(str(self.sample_file)))
        self.assertTrue(
            ProcessRunner.is_executable_valid("sample_tool.exe", cwd=str(self.base_path))
        )
        self.assertFalse(ProcessRunner.is_executable_valid(str(self.base_path)))  # directory
        self.assertFalse(ProcessRunner.is_executable_valid("missing_file.exe"))


class TestProcessRunnerSetupExecution(unittest.TestCase):
    """Tests for ProcessRunner.run_setup."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_mock_script(self, filename: str, exit_code: int = 0, marker_name: str = "") -> Path:
        """Helper to create cross-platform mock executable script."""
        script_path = self.base_path / filename
        if sys.platform == "win32" and filename.endswith((".bat", ".cmd")):
            lines = ["@echo off\n"]
            if marker_name:
                lines.append(f"echo DONE > \"{self.base_path / marker_name}\"\n")
            lines.append(f"exit /b {exit_code}\n")
            script_path.write_text("".join(lines), encoding="utf-8")
        else:
            # Python script wrapper runnable via python executable
            lines = [
                "# mock script\n",
                "import sys\n",
            ]
            if marker_name:
                lines.append(f"with open(r'{self.base_path / marker_name}', 'w') as f: f.write('DONE')\n")
            lines.append(f"sys.exit({exit_code})\n")
            script_path.write_text("".join(lines), encoding="utf-8")
        return script_path

    def test_run_setup_success_batch(self):
        """Executes Windows batch setup installer, verifies exit code 0 and side effects."""
        if sys.platform == "win32":
            script = self._create_mock_script("setup.bat", exit_code=0, marker_name=".installed")
            code = ProcessRunner.run_setup(str(script), cwd=str(self.base_path))
            self.assertEqual(code, 0)
            self.assertTrue((self.base_path / ".installed").is_file())
        else:
            script = self._create_mock_script("setup.py", exit_code=0, marker_name=".installed")
            code = ProcessRunner.run_setup(sys.executable, args=[str(script)], cwd=str(self.base_path))
            self.assertEqual(code, 0)
            self.assertTrue((self.base_path / ".installed").is_file())

    def test_run_setup_non_zero_exit_code(self):
        """Returns non-zero exit code and raises ProcessExecutionError when check=True."""
        if sys.platform == "win32":
            script = self._create_mock_script("setup_fail.bat", exit_code=42)
            code = ProcessRunner.run_setup(str(script), cwd=str(self.base_path), check=False)
            self.assertEqual(code, 42)

            with self.assertRaises(ProcessExecutionError) as ctx:
                ProcessRunner.run_setup(str(script), cwd=str(self.base_path), check=True)
            self.assertEqual(ctx.exception.exit_code, 42)
            self.assertIn("42", str(ctx.exception))
        else:
            script = self._create_mock_script("setup_fail.py", exit_code=42)
            code = ProcessRunner.run_setup(sys.executable, args=[str(script)], cwd=str(self.base_path), check=False)
            self.assertEqual(code, 42)

            with self.assertRaises(ProcessExecutionError) as ctx:
                ProcessRunner.run_setup(sys.executable, args=[str(script)], cwd=str(self.base_path), check=True)
            self.assertEqual(ctx.exception.exit_code, 42)
            self.assertIn("42", str(ctx.exception))

    def test_run_setup_with_arguments_and_env(self):
        """Passes command-line arguments and custom environment variables to setup executable."""
        arg_script = self.base_path / "check_args.py"
        arg_script.write_text(
            "import sys, os\n"
            "assert sys.argv[1] == '--silent'\n"
            "assert sys.argv[2] == r'/DIR=C:\\Games'\n"
            "assert os.environ.get('CUSTOM_GOG_VAR') == 'ACTIVE'\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        custom_env = os.environ.copy()
        custom_env["CUSTOM_GOG_VAR"] = "ACTIVE"

        code = ProcessRunner.run_setup(
            sys.executable,
            args=[str(arg_script), "--silent", r"/DIR=C:\Games"],
            cwd=str(self.base_path),
            env=custom_env,
        )
        self.assertEqual(code, 0)

    def test_run_setup_with_quoted_dir_argument_strips_quotes_for_subprocess(self):
        """
        Verify that a config containing quoted arguments (like /dir=\"C:\\Path\" or /dir=\"C:\\GOG Games\\W3\")
        is parsed and passed to a subprocess correctly, without the literal quotes becoming part of the path.
        """
        arg_script = self.base_path / "check_quoted_args.py"
        arg_script.write_text(
            "import sys\n"
            "# Assert exact received argument without internal literal quotes\n"
            "assert sys.argv[1] == r'/dir=C:\\Path', f'Expected /dir=C:\\\\Path, got {sys.argv[1]!r}'\n"
            "assert sys.argv[2] == r'/DIR=C:\\GOG Games\\Witcher', f'Expected /DIR=C:\\\\GOG Games\\\\Witcher, got {sys.argv[2]!r}'\n"
            "# Verify no literal double-quote character is present inside the path\n"
            "assert '\"' not in sys.argv[1], f'Literal quote found in {sys.argv[1]}'\n"
            "assert '\"' not in sys.argv[2], f'Literal quote found in {sys.argv[2]}'\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )

        code = ProcessRunner.run_setup(
            sys.executable,
            args=[
                str(arg_script),
                r'/dir="C:\Path"',
                r'/DIR="C:\GOG Games\Witcher"',
            ],
            cwd=str(self.base_path),
        )
        self.assertEqual(code, 0)

    def test_run_setup_timeout_handling(self):
        """Raises subprocess.TimeoutExpired and terminates child process if execution times out."""
        slow_script = self.base_path / "slow_setup.py"
        slow_script.write_text(
            "import time\ntime.sleep(10.0)\n",
            encoding="utf-8",
        )
        start = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            ProcessRunner.run_setup(
                sys.executable,
                args=[str(slow_script)],
                cwd=str(self.base_path),
                timeout_seconds=0.3,
            )
        elapsed = time.time() - start
        self.assertLess(elapsed, 4.0, "Timeout did not terminate child within reasonable threshold.")

    def test_run_setup_missing_file_raises_file_not_found(self):
        """Raises FileNotFoundError when attempting to run non-existent setup file."""
        with self.assertRaises(FileNotFoundError):
            ProcessRunner.run_setup(str(self.base_path / "ghost_setup.exe"))

    def test_run_setup_directory_raises_file_not_found(self):
        """Raises FileNotFoundError when given path is a directory."""
        with self.assertRaises(FileNotFoundError):
            ProcessRunner.run_setup(str(self.base_path))


class TestProcessRunnerGameLaunch(unittest.TestCase):
    """Tests for ProcessRunner.launch_game."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.procs_to_cleanup: list[subprocess.Popen] = []

    def tearDown(self):
        for p in self.procs_to_cleanup:
            try:
                ProcessRunner.terminate_process(p, timeout=1.0)
            except Exception:
                pass
        self.temp_dir.cleanup()

    def test_launch_game_detached_windows(self):
        """Launches game process detached and verifies it is running with valid PID."""
        game_script = self.base_path / "game_loop.py"
        game_script.write_text(
            "import time\ntime.sleep(2.0)\n",
            encoding="utf-8",
        )
        proc = ProcessRunner.launch_game(
            sys.executable,
            args=[str(game_script)],
            cwd=str(self.base_path),
            detached=True,
        )
        self.procs_to_cleanup.append(proc)
        self.assertIsNotNone(proc.pid)
        self.assertGreater(proc.pid, 0)
        self.assertIsNone(proc.poll(), "Process should still be active immediately after launch.")

    def test_launch_game_batch_file(self):
        """Launches a .bat script game executable on Windows."""
        if sys.platform == "win32":
            batch_game = self.base_path / "game.bat"
            marker = self.base_path / "game_ran.txt"
            batch_game.write_text(
                f"@echo off\necho PLAYING > \"{marker}\"\nexit /b 0\n",
                encoding="utf-8",
            )
            proc = ProcessRunner.launch_game(
                str(batch_game),
                cwd=str(self.base_path),
                detached=False,
            )
            self.procs_to_cleanup.append(proc)
            proc.wait(timeout=3.0)
            self.assertEqual(proc.returncode, 0)
            self.assertTrue(marker.is_file())

    def test_launch_game_non_detached(self):
        """Launches game with detached=False."""
        game_script = self.base_path / "quick_game.py"
        game_script.write_text(
            "import sys\nsys.exit(0)\n",
            encoding="utf-8",
        )
        proc = ProcessRunner.launch_game(
            sys.executable,
            args=[str(game_script)],
            cwd=str(self.base_path),
            detached=False,
        )
        self.procs_to_cleanup.append(proc)
        proc.wait(timeout=3.0)
        self.assertEqual(proc.returncode, 0)

    def test_launch_game_missing_file_raises_file_not_found(self):
        """Raises FileNotFoundError when game executable does not exist."""
        with self.assertRaises(FileNotFoundError):
            ProcessRunner.launch_game(str(self.base_path / "nonexistent_game.exe"))

    def test_launch_game_directory_raises_file_not_found(self):
        """Raises FileNotFoundError when path is a directory."""
        with self.assertRaises(FileNotFoundError):
            ProcessRunner.launch_game(str(self.base_path))

    def test_terminate_process_utility(self):
        """Tests terminate_process cleanly stops running process and handles exited process."""
        long_script = self.base_path / "long_running.py"
        long_script.write_text(
            "import time\ntime.sleep(15.0)\n",
            encoding="utf-8",
        )
        proc = subprocess.Popen([sys.executable, str(long_script)])
        self.assertIsNone(proc.poll())
        res = ProcessRunner.terminate_process(proc, timeout=0.5)
        self.assertTrue(res)
        self.assertIsNotNone(proc.poll())

        # Calling again on already terminated process returns True
        self.assertTrue(ProcessRunner.terminate_process(proc))


if __name__ == "__main__":
    unittest.main()
