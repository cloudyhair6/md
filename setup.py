#!/usr/bin/env python3
"""
setup.py
~~~~~~~~

Packaging specification for GOG Game Disk Monitor.
"""

from pathlib import Path
from setuptools import find_packages, setup

# Read version from package
version = "1.0.0"
init_path = Path(__file__).parent / "gog_disk_monitor" / "__init__.py"
if init_path.exists():
    for line in init_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            delim = '"' if '"' in line else "'"
            version = line.split(delim)[1]
            break

# Read long description from README.md
readme_path = Path(__file__).parent / "README.md"
long_description = (
    readme_path.read_text(encoding="utf-8")
    if readme_path.exists()
    else "Windows System Tray GOG Game Disk Monitor & Auto-Launcher"
)

setup(
    name="gog-disk-monitor",
    version=version,
    description="Windows System Tray GOG Game Disk Monitor & Auto-Launcher",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="GOG Game Disk Monitor Contributors",
    author_email="noreply@example.com",
    url="https://github.com/example/gog-disk-monitor",
    packages=find_packages(exclude=["tests*", "docs*"]),
    py_modules=["GUI_setup", "verify_gui_setup", "verify_simulation"],
    python_requires=">=3.8",
    install_requires=[
        "pystray>=0.19.5",
        "Pillow>=10.0.0",
        "PyQt6>=6.0.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "flake8>=6.0.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "gog-disk-monitor=gog_disk_monitor.cli:main",
            "gog-disk-setup=GUI_setup:main",
            "gog-disk-verify=verify_simulation:main",
        ],
    },
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Environment :: Win32 (MS Windows)",
        "Intended Audience :: End Users/Desktop",
        "License :: OSI Approved :: MIT License",
        "Operating System :: Microsoft :: Windows",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Games/Entertainment",
        "Topic :: System :: Hardware",
        "Topic :: Utilities",
    ],
    keywords="gog disk monitor autorun windows tray system-tray subst optical-disc",
)
