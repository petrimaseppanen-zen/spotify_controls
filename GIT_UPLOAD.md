# Git Upload Guide

Use these commands in `spotify-mini-controller` folder.

## 1) Initialize repo

```powershell
git init
git branch -M main
```

## 2) Add files and first commit

```powershell
git add .
git commit -m "Initial release v1.0.2"
```

## 3) Create GitHub repository

Create an empty repo in GitHub UI (without README/license/gitignore), then connect it:

```powershell
git remote add origin https://github.com/<your-user>/<your-repo>.git
```

## 4) Push

```powershell
git push -u origin main
```

## 5) Tag release (optional)

```powershell
git tag v1.0.2
git push origin v1.0.2
```

## 6) Next version workflow

```powershell
set_version.bat 1.0.3
git add VERSION.txt installer.iss spot.py
git commit -m "Bump version to 1.0.3"
```

## Recommended

- Keep `dist/` out of git (already ignored).
- Publish binaries via GitHub Releases instead of committing build outputs.
