# Launchers and command helpers

This folder contains small wrappers around the real project commands. The goal
is to keep the commands in one documented place and avoid hardcoded local paths
such as `C:\Users\...\Python313\python.exe`.

Run commands from the project root unless a section says otherwise.

## What is here

| File | Purpose |
| --- | --- |
| `mask_picker_launcher.pyw` | Starts Mask Picker without a console window and opens the browser. |
| `create_windows_shortcuts.ps1` | Creates Desktop shortcuts for Mask Picker and segmentation. |
| `bake_all.py` | Re-bakes selected cleanup/polygon results and optionally packs the final dataset zip. |
| `make_annotation_task.py` | Builds portable annotation task folders/zips for participants. |
| `import_annotation_export.py` | Imports a participant Export zip back into a workspace and normalizes local paths. |

## Python and paths

The launchers use the Python available as `python` or `py -3`. They should not
depend on a hardcoded local Python path. If Windows cannot find Python, install
Python 3.10+ from python.org and enable "Add python.exe to PATH".

Path assumptions:

- Project commands are relative to the Cryobiology project root.
- Annotation task packages are portable workspaces. Their generated
  `START_MASK_PICKER.bat` searches upward for `apps\mask_picker\app.py`.
- If a task folder is outside the project tree, set `CRYOBIOLOGY_ROOT` to the
  full project path before running `START_MASK_PICKER.bat`.

Example:

```bat
set CRYOBIOLOGY_ROOT=C:\Users\you\Projects\Cryobiology
START_MASK_PICKER.bat
```

## Install/check dependencies

For the full project:

```powershell
python -m pip install --user -r apps/mask_picker/requirements.txt
```

`apps/mask_picker/run.bat` and generated annotation launchers try to install
missing runtime packages automatically and load `shared/cellsegkit` through
`PYTHONPATH`. For development and tests, installing `cellsegkit` editable is
still fine, but participant launchers do not require it.

## Start Mask Picker

Default dataset from project config:

```powershell
.\run_mask_picker.bat
```

Specific workspace:

```powershell
python apps/mask_picker/app.py --workspace data/vesicles_good
```

Specific workspace on another port:

```powershell
python apps/mask_picker/app.py --workspace data/vesicles_good --port 5001
```

## Prepare annotation packages

Full current vesicles task:

```powershell
python tools/launchers/make_annotation_task.py --data-dir data/vesicles_good --all --name vesicles_good_annotation_full
```

Only specific photos:

```powershell
python tools/launchers/make_annotation_task.py --data-dir data/vesicles_good --stems db_img_0084,db_img_0169 --name marina_part01
```

Read photo stems from a text file:

```powershell
python tools/launchers/make_annotation_task.py --data-dir data/vesicles_good --list stems.txt --name part_from_list
```

Split all photos into N zip packages:

```powershell
python tools/launchers/make_annotation_task.py --data-dir data/vesicles_good --all --parts 4 --name vesicles_good
```

Output goes to `_send/annotation_tasks/` by default. Each task contains:

```text
START_MASK_PICKER.bat
README_FOR_ANNOTATOR.md
MANIFEST.json
images/
output/<model>/{overlay,npy,png,yolo}/
selected/
polygons/
skipped/
labels.json
selections.json
```

## Bake and final dataset zip

Re-bake all selected photos in a workspace:

```powershell
python tools/launchers/bake_all.py --data-dir data/vesicles_good
```

Re-bake and create `_archive/dataset_<workspace-name>.zip`:

```powershell
python tools/launchers/bake_all.py --data-dir data/vesicles_good --pack
```

Root shortcut:

```powershell
.\bake_all.bat --data-dir data/vesicles_good
```

## Import participant result

When a participant sends back the zip from the Mask Picker Export button:

```powershell
python tools/launchers/import_annotation_export.py _inbox/mask_picker_export_YYYYMMDD_HHMMSS.zip --data-dir data/vesicles_good
```

The importer copies `selected/`, `polygons/`, `skipped/`, merges
`selections.json`, and rewrites `copied_files` to workspace-relative paths like
`selected/instanseg/npy/db_img_0084.npy`. It creates a backup in `_backups/`
before writing.

## Desktop shortcuts

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\tools\launchers\create_windows_shortcuts.ps1
```

This creates shortcuts on the current user's Desktop. It does not build `.exe`
files and does not install PyInstaller.
