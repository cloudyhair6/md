# Original User Request

## Initial Request — 2026-08-27T09:57:36Z

> Requested team: Small, focused team

This is a single self-contained feature addition. Keep it small and focused. 

A Python GUI application (`GUI_setup.py`) built with PyQt/PySide that helps users easily generate the necessary configuration files and automatically copy the game executable, icon, and config to a selected target disk or folder for the GOG Disk Monitor application.

Working directory: ~/teamwork_projects/gog_disk_monitor
Integrity mode: development

## Requirements

### R1. Graphical User Interface
The application must be implemented in `GUI_setup.py` using PyQt or PySide. It must provide inputs for the user to select a game executable, an icon file, specify any game configuration details, and select a target output directory/drive.

### R2. Deployment Logic
Upon user confirmation, the application must generate the correct JSON configuration file (matching the schema expected by the existing GOG Disk Monitor) and copy the configuration file, the selected game executable, and the custom icon into the target directory.

## Verification Resources
The team should write a verification script that tests the underlying deployment logic (generating the config and copying files) to a temporary directory to ensure it behaves correctly without requiring manual UI clicks.

## Acceptance Criteria

### Execution & Setup
- [ ] The `GUI_setup.py` application can be executed and the GUI window opens successfully.
- [ ] Any required PyQt/PySide dependencies are added to the project's requirements.

### End-to-End File Deployment
- [ ] A programmatic test verifies that providing a mock game executable, a mock icon, and a target folder results in all three files being correctly copied to the target folder.
- [ ] The generated configuration file is verified to be completely valid and readable by the existing `config.py` parser.
