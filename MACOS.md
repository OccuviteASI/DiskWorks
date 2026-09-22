# DiskWorks on macOS

DiskWorks 0.2.0 carries a complete macOS backend (`dw_mac.py`): the Disks view through
`diskutil`, Unlock through the standard administrator prompt, operations through
`diskutil partitionDisk / addPartition / eraseVolume / resizeVolume / apfs resizeContainer /
rename / verifyVolume`, imaging on `/dev/rdiskN`, the Access ladder for NTFS and ext
partitions, the Speed and Space tabs.

**Status:** the macOS code was written and unit-checked on Windows against recorded
`diskutil` output (`tools/mac_unit.py`, fixtures in `tools/fixtures/mac/`). It has **not yet
been run on a Mac**. The first real run is the checklist at the end of this file; anything
that fails there is a bug to report back.

## 1. Prerequisites on the Mac

| What | Why | How |
|---|---|---|
| macOS 12 Monterey or newer, Apple Silicon (an Intel Mac builds `mac-x86_64` the same way) | WKWebView + PyObjC baseline | – |
| Xcode Command Line Tools | compilers for a few Python wheels, `codesign`, `ditto` | `xcode-select --install` |
| Python 3.12 or newer | the app is Python | `brew install python@3.12` or the installer from python.org. `python3 --version` must say 3.12+ |
| Homebrew (optional) | for the optional tools below | https://brew.sh |

Optional tools DiskWorks uses when they are present (it never installs them):

| Tool | Gives | Install |
|---|---|---|
| 7-Zip | the read-only file browser for partitions macOS cannot mount (ext4, btrfs, xfs, NTFS without a driver) | `brew install 7zip` |
| macFUSE + ntfs-3g | NTFS **read-write** (macOS reads NTFS by itself, read-only) | `brew install --cask macfuse` then `brew install gromgit/fuse/ntfs-3g-mac`. macFUSE 5.1+ works through the FSKit backend without a kernel extension; older versions need "Reduced Security" on Apple Silicon |
| ExtendFS (App Store, macOS 15.6+) | ext2/3/4 read-only, mounts automatically | App Store |

## 2. Get the source onto the Mac

On the Windows machine:

```bash
python tools/pack_source.py
```

writes `dist/DiskWorks-src-<version>.zip` (code, UI, docs, build scripts; no binaries).
Copy it to the Mac (AirDrop, a USB stick, a share), then in Terminal:

```bash
cd ~/Downloads
unzip DiskWorks-src-0.2.0.zip
cd DiskWorks
```

## 3. Build

```bash
./build-mac.sh
```

The script checks for the Command Line Tools, creates `.venv`, installs the Python
dependencies (pywebview pulls the PyObjC frameworks on macOS; PyInstaller, pycdlib, pyfatfs)
and runs `python build.py`. Output:

```
dist/mac-arm64/DiskWorks.app                 the app (a one-folder PyInstaller bundle)
dist/mac-arm64/DiskWorks-0.2.0-mac-arm64.zip the same app zipped with ditto, for sharing
dist/mac-arm64/VERSION.txt, THIRD-PARTY-LICENSES.txt
```

The build takes two to five minutes the first time (pip downloads), under a minute after.
`./build-mac.sh --console` builds a plain command-line binary instead of an `.app`, handy
for reading tracebacks.

## 4. First start

1. **Gatekeeper.** The app is ad-hoc signed, not notarized. The first time, right-click
   `DiskWorks.app` → **Open** → **Open** (or run `xattr -dr com.apple.quarantine
   dist/mac-arm64/DiskWorks.app` once). After that it opens normally.
2. **Removable volumes.** The first time DiskWorks looks at an external disk, macOS asks
   *"DiskWorks would like to access files on a removable volume"*. Allow it. This is the
   Transparency, Consent and Control (TCC) prompt; the app triggers it from the window
   process on purpose, because a root helper never gets its own prompt.
3. **Internal disks.** Reading the raw device of an internal disk needs **Full Disk Access**
   (System Settings → Privacy & Security → Full Disk Access → add DiskWorks). The app shows
   the message and a button that opens that pane when it hits the permission error.
   Viewing works without it; only raw reads (signatures, imaging of internal disks) need it.
4. **Unlock.** Pressing Unlock shows the standard *"DiskWorks wants to make changes"*
   administrator prompt (osascript `with administrator privileges`). The helper it starts
   runs as root for the rest of the session; the window never does.

Ad-hoc signatures change with every rebuild, and macOS ties TCC grants to the signature:
after a rebuild expect the prompts again and re-add Full Disk Access if you had granted it.

State, settings and the session log live in `~/Library/Application Support/DiskWorks`.

## 5. Running from source (development)

```bash
source .venv/bin/activate
python diskworks.py                 # opens the window
python diskworks.py --no-open --port 8766   # server only, open http://127.0.0.1:8766 in Safari
python diskworks.py --fixture tools/fixtures/mac/usb-ntfs.json   # render a recorded inventory
sudo DISKWORKS_NO_ELEVATE=1 python diskworks.py --no-open --port 8790   # helper without the prompt (tests)
```

## 6. Smoke checklist (the first run on real hardware)

Use a USB stick you can erase. Tick each line; anything that fails is worth a note.

1. App opens; the version pill says 0.2.0; clicking it shows the release notes.
2. **Disks**: the internal disk is hidden until "Show system disks"; the stick shows as a
   GPT/MBR disk with its partitions; free space is shown after Unlock (the exact gaps come
   from `gpt -r show`, which needs root).
3. **Unlock**: the administrator prompt appears; the pill turns green with the open lock.
   Cancelling the prompt gives "The administrator prompt was cancelled." and the pill stays
   locked.
4. **Operations** on the stick: New partition table (GPT) → Apply; New partition, exFAT,
   named TEST → Apply; the Finder mounts it. Then Label, Delete, Format (MS-DOS FAT32),
   Check. Right-click menus, the Actions strip and "Show commands" (every step is a
   `diskutil …` line).
5. **Drag-resize**: shrink an HFS+ (Mac OS Extended) partition by dragging its right edge;
   the pending list shows "Shrink … to …"; Apply runs `diskutil resizeVolume`.
6. **Image**: write a small ISO to the stick with verify on; then back the stick up to a
   `.img.zst`; verify the backup; restore it.
7. **Access**: an NTFS stick → rung "Built into macOS (read-only)" → Open mounts it; with
   macFUSE + ntfs-3g installed the read-write rung appears. An ext4 stick → ExtendFS /
   ext4fuse rungs, and the 7-Zip browser if `7zz` is installed (`brew install 7zip`).
8. **Speed**: run one pass on the stick; write and read numbers appear; the temp file is
   gone afterwards (`ls -la /Volumes/TEST`).
9. **Space**: scan the home folder; click into rings; move a throwaway file to the Trash;
   it appears in the Finder's Trash.
10. Quit; `~/Library/Application Support/DiskWorks/diskworks.log` has the session; no
    `DiskWorks` helper process is left (`pgrep -fl DiskWorks`).

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `xcode-select: note: no developer tools were found` | run `xcode-select --install`, finish the dialog, re-run `./build-mac.sh` |
| `python3: command not found` or version < 3.12 | install Python 3.12+, or run `PYTHON=/opt/homebrew/bin/python3.12 ./build-mac.sh` |
| pip fails building `pyobjc-framework-…` | the Command Line Tools are missing or old; `softwareupdate --all --install --force` then retry |
| The app bounces once and quits | run the console build (`./build-mac.sh --console`, then `dist/mac-arm64/DiskWorks`) and read the traceback; usually a missing hidden import — add it to `build.py`'s mac branch |
| "DiskWorks is damaged and can't be opened" | Gatekeeper on an unsigned download: `xattr -dr com.apple.quarantine DiskWorks.app` |
| Unlock: prompt appears, pill stays "Asking for permission…" then fails | the helper could not connect back: check `~/Library/Application Support/DiskWorks/diskworks.log`; the socket lives under `$TMPDIR`; try `sudo DISKWORKS_NO_ELEVATE=1 python diskworks.py` to see the helper's own error |
| `Operation not permitted` reading `/dev/rdiskN` | TCC: allow Removable Volumes, or add Full Disk Access for internal disks |
| Free space not shown / partition starts marked approximate | `gpt -r show` needs root: Unlock once, the inventory refines itself |
| `diskutil` says "Could not unmount disk" | something has files open on it (Spotlight, Finder). `sudo mdutil -i off /Volumes/X`, close windows, retry; DiskWorks already uses `unmountDisk force` |
| NTFS mounts read-only | that is macOS's built-in driver; install macFUSE + ntfs-3g for write access (see §1) |

## 8. What is different on a Mac

- Formats DiskWorks can create on macOS: APFS, Mac OS Extended (HFS+, journaled), exFAT,
  FAT32. NTFS, ext2/3/4, btrfs, xfs, f2fs and Linux swap cannot be created on a Mac; the
  dialog says so and names a Windows or Linux machine for that.
- Sizes go to `diskutil` with explicit units; partitions are 1 MiB aligned like everywhere.
- Synthesized APFS disks are not shown as disks; the container shows as one segment on the
  physical disk with its volumes in the detail pane, like Disk Utility.
- Test disks for the smoke scripts are `hdiutil attach -nomount` raw images instead of
  Windows VHDs / Linux loop devices (`/api/dev/vhd` handles it).
