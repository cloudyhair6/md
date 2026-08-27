"""
gog_disk_monitor.cli
~~~~~~~~~~~~~~~~~~~~

Command-Line Interface for GOG Game Disk Monitor.
Supports starting the system tray daemon, headless background monitoring,
single-scan executions, state queries, and automated testing overrides.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import signal
import sys
from typing import Optional, Sequence

from . import __version__
from .app import GOGDiskMonitorApp
from .state import StateStore

logger = logging.getLogger("gog_disk_monitor.cli")


def build_parser() -> argparse.ArgumentParser:
    """Constructs the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="gog-disk-monitor",
        description="Windows System Tray GOG Game Disk Monitor & Auto-Launcher",
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show program version and exit.",
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="Run in headless mode without system tray icon or GUI prompt dialogs.",
    )

    parser.add_argument(
        "--scan-once",
        action="store_true",
        default=False,
        help="Perform a single synchronous scan of all logical drives, process any GOG game discs, and exit.",
    )

    parser.add_argument(
        "--state-file",
        metavar="PATH",
        type=str,
        default=None,
        help="Custom path to installed_games.json persistent state file.",
    )

    parser.add_argument(
        "--poll-interval",
        metavar="SECONDS",
        type=float,
        default=0.5,
        help="Polling interval in seconds between drive bitmask checks (default: 0.5s).",
    )

    parser.add_argument(
        "--auto-confirm",
        action="store_true",
        default=None,
        help="Automatically accept and install detected GOG game discs without prompting.",
    )

    parser.add_argument(
        "--auto-reject",
        action="store_true",
        default=False,
        help="Automatically reject detected GOG game installation prompts.",
    )

    parser.add_argument(
        "--scan-startup",
        action="store_true",
        default=False,
        help="Scan already mounted drives immediately on monitor startup.",
    )

    parser.add_argument(
        "--install-root",
        metavar="PATH",
        type=str,
        default=None,
        help="Base directory for installed game folders (default: C:\\GOG Games).",
    )

    parser.add_argument(
        "--list-installed",
        action="store_true",
        default=False,
        help="List all installed games recorded in the state store and exit.",
    )

    parser.add_argument(
        "--unmark",
        metavar="GAME_ID",
        type=str,
        default=None,
        help="Unmark / remove an installed game from the state store and exit.",
    )

    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Enable detailed debug logging output.",
    )

    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        default=False,
        help="Suppress all logs except warnings and errors.",
    )

    return parser


def setup_logging(verbose: bool = False, quiet: bool = False) -> None:
    """Configures application logging level and format."""
    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.WARNING
    else:
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
        datefmt="%H:%M:%S",
    )


def handle_list_installed(state_file: Optional[str]) -> int:
    """Prints all installed games from state store."""
    store = StateStore(state_file_path=state_file)
    games = store.get_all_installed()
    if not games:
        print("No installed GOG games found in state store.")
        return 0

    print(f"\nRecorded GOG Game Installations ({len(games)} found):")
    print("-" * 75)
    print(f"{'GAME ID':<20} {'TITLE':<30} {'VERSION':<10} {'STATUS'}")
    print("-" * 75)
    for gid, rec in sorted(games.items()):
        print(f"{rec.game_id:<20} {rec.title:<30} {rec.version:<10} {rec.status}")
        print(f"  Install Path:    {rec.install_path}")
        print(f"  Executable:      {rec.executable_path}")
        if rec.last_launched_at:
            print(f"  Last Launched:   {rec.last_launched_at}")
        print("-" * 75)
    return 0


def handle_unmark_game(state_file: Optional[str], game_id: str) -> int:
    """Removes a game record from state store."""
    store = StateStore(state_file_path=state_file)
    removed = store.unmark_installed(game_id)
    if removed:
        print(f"Successfully unmarked game '{game_id}' from state store.")
        return 0
    else:
        print(f"Game '{game_id}' was not found in state store.")
        return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    """
    Main CLI entrypoint for GOG Game Disk Monitor.

    Args:
        argv: Optional command-line argument list (defaults to sys.argv[1:]).

    Returns:
        Process exit code integer (0 for success).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_logging(verbose=args.verbose, quiet=args.quiet)

    # 1. State Store Query / Mutation Flags
    if args.list_installed:
        return handle_list_installed(args.state_file)

    if args.unmark:
        return handle_unmark_game(args.state_file, args.unmark)

    # 2. Determine Auto Confirm behavior
    auto_confirm: Optional[bool] = None
    if args.auto_confirm:
        auto_confirm = True
    elif args.auto_reject:
        auto_confirm = False

    # 3. Instantiate Coordinator Application
    app = GOGDiskMonitorApp(
        state_file_path=args.state_file,
        poll_interval=args.poll_interval,
        auto_confirm=auto_confirm,
        headless=args.headless,
        scan_on_startup=args.scan_startup,
        install_root=args.install_root,
    )

    # 4. Single-Scan Mode
    if args.scan_once:
        logger.info("Executing single scan of logical drives...")
        results = app.scan_now()
        logger.info("Scan completed with %d event result(s).", len(results))
        for res in results:
            action = res.get("action")
            title = res.get("title", res.get("game_id", "Unknown"))
            logger.info("Action: %s for %s", action, title)
        return 0

    # 5. Continuous Daemon Mode
    # Register graceful signal handlers
    def sig_handler(sig: int, frame: Any) -> None:
        logger.info("Received interrupt signal (%s). Initiating shutdown...", sig)
        app.stop()

    try:
        signal.signal(signal.SIGINT, sig_handler)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, sig_handler)  # type: ignore[attr-defined]
    except Exception:
        pass

    try:
        app.start(block=True)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt caught. Stopping application...")
        app.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
