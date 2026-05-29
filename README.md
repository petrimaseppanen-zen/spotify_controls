# Spotify Mini Controller

A compact Windows desktop controller for Spotify with a live audio spectrum visualizer.

## Features

- Frameless always-on-top mini window
- Track info from Spotify window
- Media controls: previous, play/pause, next
- Real-time spectrum analyzer
- Adjustable analyzer tuning (speed, falloff, accuracy)
- Toggle for centered spectrum rendering
- Audio source selection (loopback or microphone fallback)
- Build scripts for standalone EXE and installer

## Requirements

- Windows 10/11
- Python 3.12
- Spotify desktop app (recommended for track metadata)

## Quick Start

1. Open terminal in this folder.
2. Run:

```bat
start_spot.bat
```

This script creates `.venv`, installs dependencies, and launches the app.

## Manual Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe spot.py
```

## Build EXE

```bat
build_exe.bat
```

Output:

- `dist/SpotifyMiniController.exe`

## Build Installer (Inno Setup)

1. Install Inno Setup 6.
2. Compile `installer.iss`:

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
```

Output:

- `dist/SpotifyMiniController-Setup.exe`

## Versioning

- Runtime version is read from `VERSION.txt`.
- Update version quickly:

```bat
set_version.bat 1.0.3
```

## Project Files

- `spot.py` - main application
- `start_spot.bat` - local run helper
- `build_exe.bat` - PyInstaller build helper
- `installer.iss` - Inno Setup script
- `VERSION.txt` - current app version
- `requirements.txt` - Python dependencies
- `GIT_UPLOAD.md` - step-by-step git upload instructions

## Notes

- If loopback is unavailable, app may fall back to microphone input.
- Audio hardware and Windows routing can affect analyzer behavior.
