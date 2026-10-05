# DiskWorks architecture

DiskWorks is a self-contained disk and partition manager for Windows and Linux: a
Disk-Management-style view with GParted-grade operations, a "make this partition
readable from the OS" ladder for foreign filesystems, and Rufus-style imaging (write
an image or bootable ISO, back up a drive, restore it). It is a Python 3.14 program
that serves a small web UI to itself and shows it in a native window. Nothing is
downloaded at run time. The window never runs elevated; one privileged helper per
session does the work. This document describes how it is built and why; the
requirements ledger is `REQUIREMENTS.md`, releases are in `CHANGELOG.md`.

Status 2026-09-11: version 0.1.0, the first runnable build, exists for both platforms
and passed its end-to-end tests (§12). Sections describe the build as it is; what is
still planned is marked in `REQUIREMENTS.md`. Open questions live in §13.

## 1. Process model

```
 DiskWorks.exe / DiskWorks  (PyInstaller one-file; unpacks to _MEI*/ then runs)
 |
 |-- main thread ........ pywebview window (WebView2 on Windows, Qt on Linux, WKWebView on macOS)
 |                        webview.start() returns when the LAST window closes
 |                        -> that is the shutdown signal
 |-- HTTP thread ........ ThreadingHTTPServer on 127.0.0.1:<free port>
 |                        one handler thread per request; serves ui/ and the JSON API
 |-- Inventory .......... poll thread: unprivileged snapshot of disks/partitions/volumes
 |                        every 3 s (or on a device-change signal); diffed into events
 |-- Jobs ............... one Job at a time (apply queue, image write/backup/restore,
 |                        access ladder); the Job talks to the helper and appends
 |                        progress to an EventLog the UI polls
 `-- Helper ............. `DiskWorks --helper <address> [--token-file <path>]`
                          started ONCE per session via ShellExecute "runas" (Windows),
                          pkexec (Linux) or osascript "with administrator privileges"
                          (macOS); headless; admin/root; connects BACK to the
                          window process over multiprocessing.connection and runs
                          allow-listed verbs; exits when the connection drops
```

**Why two processes.** Microsoft's WebView2 guidance says not to host WebView2 in an
elevated process (user-data-folder and drag-and-drop breakage, worse under Windows 11
Administrator Protection). On Linux QtWebEngine refuses to run as root unless its
sandbox is disabled, and `pkexec` strips `DISPLAY`. So the UI stays unprivileged and
every privileged action goes through a separate helper — the same pattern LinkTest
uses for packet capture (`--capture-helper`), extended into a long-lived session
helper.

**Helper handshake.**
1. The window process creates a `multiprocessing.connection.Listener` with a random
   32-byte `authkey` (`secrets.token_bytes`). Address: Windows `\\.\pipe\DiskWorks-<uuid>`
   (`AF_PIPE`); Linux `$XDG_RUNTIME_DIR/diskworks-<uuid>/sock` in a 0700 directory
   (`AF_UNIX`).
2. Windows: the key is written to `%LOCALAPPDATA%\DiskWorks\helper\<uuid>.key` (the
   directory is user-private by default ACL) and the helper is started with
   `ShellExecuteExW(lpVerb="runas")` — the only supported way to elevate — with the
   address and key path on its command line. Linux: `pkexec <sys.executable> --helper
   <address>`; `pkexec` keeps stdin, so the key is written to the helper's stdin as one
   hex line and stdin is closed. macOS: the key goes to a 0600 file in the state dir and
   the helper is launched detached through `osascript -e 'do shell script "<argv>
   >/dev/null 2>&1 &" with administrator privileges'` (`dw_ipc.launch_mac`); osascript
   returns once the prompt is answered and the root helper connects back over the
   `AF_UNIX` listener (socket under `tempfile.gettempdir()`); AppleScript error -128 →
   "The administrator prompt was cancelled."; the script text is built once per session
   (the 5-minute authorization cache is keyed on it) and `DYLD_*` / `PATH` are scrubbed
   from the environment before it is built. Nothing secret is ever on a command line.
3. The helper connects with `Client(address, authkey=key)` (mutual HMAC challenge in
   both directions), sends `{"hello": {"version", "pid", "uid"}}`; the window checks
   the version matches its own and marks the helper **ready**. From then on the UI's
   Unlock state is "on"; no further prompts this session.
4. Frames are JSON via `send_bytes` / `recv_bytes`. Requests `{"id", "verb", "args"}`;
   the helper answers with any number of `{"id", "event": "progress"|"log"|"done"|
   "error", …}` frames. One job runs at a time; `cancel` and `ping` are accepted while
   a job runs (the helper reads on its own thread).
5. The helper exits on `EOFError` from the connection, on `quit`, or when the window
   pid it was told about is gone. A cancelled UAC / polkit prompt is detected by the
   window (RunAs raises with "cancel" in the error; pkexec exits 126/127) and reported
   as "The administrator prompt was cancelled." Silence for 20 s after launch reports
   "The helper did not start" with the log path.

**Frozen-binary notes.** The elevated helper is a second copy of the one-file binary;
it extracts its own `_MEI` directory (as admin/root — a root helper must never reuse a
user-writable extraction, and `pkexec` clears `TMPDIR` anyway). Cost: one extra
extraction per session (seconds on Linux for the Qt-laden binary; the helper imports
none of the UI). `--runtime-tmpdir` is never fixed. `helper_command()` returns
`[sys.executable]` when frozen, `[sys.executable, __file__]` from source, like
LinkTest's `pcaptool.helper_command()`. On Linux the helper sets `LD_LIBRARY_PATH` to
its `_MEIPASS` for every bundled tool it spawns (§10).

**Second launch** finds `<state>/instance.json`, validates it with `GET /api/ping`,
and asks the running instance for another window (`POST /api/window`). `--no-open`
serves without a window (dev server for the browser pane); `--browser` opens the
default browser (dev only).

**Intent journal.** Before the helper's first write of a job it appends
`<state>/journal/<jobId>.json` (`{job, steps[], started, device ids, layout hash}`)
and updates it after each step. On the next start, an unfinished journal is shown in
the Log tab as "This operation was interrupted after step N of M" with the steps that
did and did not run.

## 2. Modules

| File | Role |
|---|---|
| `diskworks.py` | Entry point. `App` (settings, inventory, jobs, helper handle), `Handler` (routes), `QuietServer`, native window helpers, `main()`, `--helper` dispatch before argparse. **Single source of `VERSION`.** |
| `dw_inventory.py` | Data model `Disk`, `Partition`, `Volume`, `Gap`, `Inventory` (dataclasses → JSON); protection flags (§4); gap computation and alignment; layout hash; the unprivileged backends (`win_inventory()`, `linux_inventory()`) and the privileged refinement the helper returns (`inventory` verb). |
| `dw_ipc.py` | Launching the helper (RunAs / pkexec), key hand-over, `Listener` / `Client` wrappers, frame encoding, request ids, cancellation, liveness. |
| `dw_helper.py` | The privileged side: connects back, serves verbs one job at a time (`ping` / `cancel` / `quit` answered immediately), refines the inventory (signatures, BitLocker, WSL state), watches the parent pid. Verb tables from `dw_ops_exec`, `dw_image`, `dw_access` are merged in. |
| `dw_ops_exec.py` | Helper-side execution of planned steps (`ps`, `sfdisk`, `tool`, `unmount`, `raw_zero`), the minimum-size `probe`, the `vhd` test-disk verb, and the PowerShell → sentence error mapping. |
| `dw_jobs.py` | Window-side job manager: `/api/ops/*` plan / apply / events, `/api/fs/probe`, `/api/dev/vhd`; runs the step list through the helper on a thread and streams progress into an `EventLog`. Owns the imaging (`dw_image.ImageJobs`) and access (`dw_access.Access`) route handlers. |
| `dw_ops.py` | Pending operations (`CreateOp`, `DeleteOp`, `FormatOp`, `ResizeOp`, `MoveOp`, `LabelOp`, `FlagsOp`, `LetterOp`, `TableOp`, `WipeOp`, `CheckOp`), the planner / validator, expansion of each op into ordered platform steps with a command preview, the layout preview. |
| `dw_fs.py` | Filesystem capability matrix (§7) as data; per-filesystem adapters: create / grow / shrink / check / label / uuid / min-size probe, each returning the command line to run and a parser for its output and progress. |
| `dw_win.py` | Windows backend: one-shot `Get-CimInstance` batch over `root\Microsoft\Windows\Storage`, Storage cmdlet wrappers, `diskpart /s` script runner with percent parsing, ctypes (`CreateFileW`, `DeviceIoControl` for `FSCTL_LOCK_VOLUME` / `FSCTL_DISMOUNT_VOLUME` / `IOCTL_STORAGE_QUERY_PROPERTY` / `IOCTL_STORAGE_GET_DEVICE_NUMBER` / `IOCTL_DISK_UPDATE_PROPERTIES` / `FSCTL_SET_SPARSE`), the FAT32 formatter, `virtdisk` calls, `wsl.exe` wrappers (UTF-16 decoding), driver install via `pnputil`, `manage-bde` / `-FVE-FS-` BitLocker detection. |
| `dw_mac.py` | macOS backend (0.2.0): unprivileged inventory from `diskutil list -plist` + `diskutil info -plist` per device (every key read with defaults; synthesized APFS disks folded into the physical store's partition as `apfsContainer` / `apfsVolumes`; starts estimated and flagged `approx` until the root helper refines them with `gpt -r show` / `fdisk -d`), `RawDevice` on `/dev/rdiskN` (sector-aligned pread/pwrite, `DKIOCGETBLOCKSIZE/COUNT`), `diskutil mount/unmount/unmountDisk force/mountDisk`, `listFilesystems -plist`, `hdiutil attach -nomount` test disks, the TCC probe + Full Disk Access deep link, tool detection (macFUSE, its FSKit backend, ExtendFS, 7-Zip), `clean_env()` (drops `DYLD_*`, `LD_*`), absolute tool paths. |
| `dw_speed.py` | Speed tab (0.2.0), window process: `UnbufferedFile` (Windows `FILE_FLAG_NO_BUFFERING|WRITE_THROUGH` via ctypes, Linux `O_DIRECT`, macOS `F_NOCACHE` + `F_FULLFSYNC`), `SpeedTest` (targets = mounted volumes with free space, write-then-read loop over a `.diskworks-speedtest.tmp` file placed by `writable_folder_on()` in the first writable folder on that volume (Temp, home, root, `DiskWorks-speedtest`; the root of the Windows system drive refuses normal users, D-034), 1 MiB-aligned `mmap` blocks, `tick` / `phase` / `run` / `done` events, MB = 10^6). |
| `dw_space.py` | Space tab (0.2.0, treemap data 0.3.0), window process: `Node` tree from `os.scandir` (no symlinks / reparse points, top 40 files per folder, the rest aggregated, progress events), per-extension totals and a 1000-entry largest-files heap over the whole scan, `SpaceScan` (targets = mounted volumes + home; `/api/space/tree` with `depth`, `min` share pruning for the treemap; `/api/space/types`, `/api/space/largest`; delete to Recycle Bin / Trash via `Microsoft.VisualBasic.FileIO` / Finder / `gio trash` or the freedesktop spec, permanent delete with the critical-path guard; reveal in Explorer / Finder / xdg-open). |
| `dw_smart.py` | Drive health (0.3.0), both processes: helper verbs `smart` / `smart_selftest` run `smartctl -j` (bundled or system; `-d sat` retries for USB) and fall back to Windows storage reliability counters + the WMI failure-prediction ATA block (`parse_ata_block`) or macOS `diskutil` SMARTStatus; `normalize_smartctl` + `assess` turn any source into one report (verdict, reasons, tiles, attributes, NVMe log, self-tests); window-side `Smart` caches a report per disk and serves `/api/smart`, `/api/smart/all`, `/api/smart/selftest` (D-037). |
| `dw_linux.py` | Linux backend: `lsblk -J -b -O` + `/sys` parsing, `sfdisk` / `sgdisk` script building, `udisksctl` / `mount` / `umount`, `findmnt` / holders / `/proc/swaps` busy checks, `/proc/filesystems` + `modprobe -n` ladder, `BLKRRPART` ioctl (fallback `blockdev --rereadpt`) / `udevadm settle`, then a kernel-vs-table partition-count check with `partx -u` / `partprobe` when they differ (D-035), live-boot detection. |
| `dw_image.py` | Imaging, both sides: source inspection (hybrid MBR/GPT, compression by magic, ISO / Windows media via pycdlib, fixed VHD footer, DiskWorks manifest), decompressors (`gzip`, `lzma`, `bz2`, `compression.zstd`), the raw copy loops (1 MiB `mmap` buffers, write-through, per-block blake2b for read-back verify, zero-block skipping → sparse or zstd output, JSON manifest), `ImageJobs` routes. Windows ISO mode is not in 0.1.0. |
| `dw_access.py` | The three ladders (§8) evaluated without side effects, WSL attach / detach, installed-driver detection (service key + `GetVolumeInformationW` names), drive-letter assignment, Linux mount / unmount (udisks2 first when the helper is not up), macOS `diskutil` / FUSE mounts, the commercial-driver `info` rungs (name + website, never installed), the **7-Zip read-only browser** (`browse` / `copyout` helper verbs, partition device or temp-image source, `parse_slt`), bookkeeping of what DiskWorks opened. Driver install offers are still 'later'. |
| `ui/index.html` | One page; tab bar + one `<section class="tab">` per tool; dialogs (operation dialogs, confirm-by-typing, release notes, About). |
| `ui/app.js` | Shell helpers (`api`, `toast`, `copyText`, `esc`, `fmtBytes`, seq pollers), version pill + release notes, Unlock state, Log tab. |
| `ui/disks.js` | Disks tab: volume table, disk bars (flex segments with a minimum width), selection, detail pane (Actions strip under the title, facts in tiles, protection reasons), the right-click context menu (`opsFor(sel)` decides what is offered and why something is locked; the same list feeds the Actions strip), the system-disk filter (`visibleDisks()`, hidden by default), drag-resize (`resizeInfo()` decides which segments get a grip; pointer events map pixels over partition + gap to 1 MiB-snapped bytes, clamp to the probed minimum and the free space, and queue `{op: resize}` on release). Drive-health dialog (verdict banner, tiles, facts, attribute / NVMe / Windows-counter tables, self-tests, Copy report) and the S.M.A.R.T. chips on the disk bars (0.3.0). |
| `ui/ops.js` | Operation dialogs (create with size slider, format, delete, label, letter, check, resize with the probed minimum, table, wipe), the pending queue → `/api/ops/plan` → preview, 'Show commands', Apply with typed confirmation and live step progress. |
| `ui/image.js` | Image tab: source picker and inspection card, target picker with guardrails, write / backup / restore / verify flows. |
| `ui/access.js` | Access tab: per-partition ladder card, rung results (`ok` / `done` / `no` / `later` / `info` with links), WSL attach state, the file browser panel (breadcrumb, folders first, multi-select, Copy out… through the native folder dialog with progress). |
| `ui/speed.js` | Speed tab: target / size / block / repeat controls, two SVG dial gauges with auto-scaling, run history with averages, the "Will it work?" table. |
| `ui/space.js` | Space tab: scan control and progress, Rings / Treemap switch (remembered in settings), SVG sunburst (two rings, free space at the root, tooltip, click to descend), Show-free-space checkbox (off by default, `spaceShowFree` setting, 0.3.1), canvas treemap (squarified layout, nested folder frames four levels deep, files coloured by type group with a legend, hover tooltip, click strip / double-click to open, click file to tick), list with share bars, By-file-type and Largest-files sections, breadcrumb, selection shared across list / map / largest, Move to Trash / Delete permanently (type-to-confirm), reveal. |
| `ui/style.css` | Design tokens (light / dark via `prefers-color-scheme`), components; same token names as LinkTest. |
| `fetch-helpers.py` | Downloads or copies and SHA-256-pins every bundled binary and blob into `bin/<tag>/` (§10). Never used at run time. |
| `build.py` | PyInstaller build for the current platform: one-file on Windows and Linux, `--windowed --onedir` `.app` on macOS (`--osx-bundle-identifier com.asirobots.diskworks`, `--collect-all webview`, PyObjC hidden imports, Qt excluded, `finish_mac_bundle()` adds the Info.plist keys and ad-hoc signs, `ditto` zip); `--wsl` runs the Linux build inside WSL; collects the bundled tools and their libraries; writes `VERSION.txt` and `THIRD-PARTY-LICENSES.txt`. `build-mac.sh` wraps it on a Mac (Command Line Tools gate, `.venv`, deps). |
| `tools/` | Dev aids: `inv_summary.py` (one-screen inventory dump), `helper_smoke.py` (channel + refined inventory), `ops_smoke.py`, `image_smoke.py`, `access_smoke.py`, `browse_smoke.py` (end-to-end on a throwaway VHD / loop disk; `--attach <port>` drives a running instance such as the frozen exe), `mac_unit.py` (+ `fixtures/mac/`: the macOS backend against recorded diskutil output), `pack_source.py` (source zip for the Mac). |

Platform tags are `win64`, `linux-x86_64`, `mac-arm64` and `mac-x86_64`, as in LinkTest.

## 3. UI ↔ backend contract

The UI never touches the OS; it calls the local JSON API. Two patterns, unchanged
from LinkTest:

- **Request / response**: `GET /api/inventory`, `POST /api/ops/plan {ops}` …
  Errors come back as `{"error": "sentence for the user"}` with HTTP 400;
  `ValueError` / `RuntimeError` raised in the backend map to that automatically.
- **Seq polling** for anything long-running: the backend appends events to an
  `EventLog` (`seq`, `ts`, `type`, payload); the UI polls `GET …/events?since=<seq>`
  every ~0.4 s and applies each event. A page reload replays from `since=0` or a
  `state` route, so the UI always rehydrates. Progress from the helper is copied into
  the job's `EventLog` as it arrives.

### Routes

| Route | Method | Purpose |
|---|---|---|
| `/`, `/ui/*` | GET | Static UI |
| `/api/ping` | GET | Instance identity (`{app, version}`) |
| `/api/status` | GET | App info (version, platform, windowed), helper state (`absent` / `starting` / `ready` / `failed` + reason), bundle info (tools present), WSL state on Windows |
| `/api/inventory[?refresh=1]` | GET | Current snapshot: disks with partitions, gaps, volumes, protection flags, layout hash |
| `/api/inventory/events?since` | GET | `changed` events (added / removed / modified device ids) |
| `/api/helper/start` | POST | Start the helper now (the Unlock button); returns `{status, message}` |
| `/api/ops/plan {ops, snapshot}` | POST | Validate a pending queue: returns the previewed layout, per-op steps with command previews, warnings, and the unmounts it will perform |
| `/api/ops/apply {ops, snapshot, confirm}` | POST | Run the queue (needs the helper); `confirm` carries the typed model for locked targets |
| `/api/ops/cancel` | POST | Cancel between steps (a running tool is terminated only when it is safe: never mid-copy of a move) |
| `/api/ops/events?since`, `/api/ops/state` | GET | Job events (`step`, `progress`, `log`, `done`, `error`) and rehydrate |
| `/api/fs/probe {device}` | POST | Filesystem details and the true minimum size for a resize dialog |
| `/api/fs/check {device, repair}` | POST | Check / repair (job) |
| `/api/access/ladder {device}` | POST | Evaluate the ladder without acting: rungs with `possible` / `reason` |
| `/api/access/open {device, rung}` | POST | Run one rung (job); returns the path or letter |
| `/api/access/close {device}` | POST | Detach / unmount what the app mounted |
| `/api/access/browse {part, path}` | POST | Read-only listing of one folder level through 7-Zip in the helper (`{path, entries[{name, path, size, dir, mtime}], total, source}`) |
| `/api/access/copyout {part, paths, dest}` | POST | Copy files / folders out (job; progress on `/api/access/events`) |
| `/api/access/pickdir` | POST | Native folder dialog for the copy-out destination |
| `/api/speed/targets`, `/api/speed/start {root, sizeMB, block, loop}`, `/api/speed/stop`, `/api/speed/events?since`, `/api/speed/state` | GET / POST | Speed test (window process, no helper) |
| `/api/space/targets`, `/api/space/scan {root}`, `/api/space/stop`, `/api/space/events?since`, `/api/space/tree?path&depth[&min]`, `/api/space/types`, `/api/space/largest?n`, `/api/space/delete {paths, mode}`, `/api/space/reveal {path}` | GET / POST | Space scan and deletion (window process, no helper); `min` = share below which the treemap's children are summed instead of listed |
| `/api/smart?disk[&refresh=1]`, `/api/smart/all`, `/api/smart/selftest {disk, kind}` | GET / POST | Drive health report for one disk (`{locked: true}` before Unlock; cached 2 min), the cached summaries for the chips, start a short / long self-test (D-037) |
| `/api/access/driver {name}` | POST | Install a bundled driver after the explicit yes (job) — not in 0.1.0 |
| `/api/image/inspect {path}` | POST | Source inspection: kind, size, hybrid table, Windows media, compression |
| `/api/image/targets` | GET | Disks eligible for writing (removable first; `all` includes fixed non-boot disks) |
| `/api/image/write {path, disk, mode, verify}` | POST | Write image or ISO mode (job) |
| `/api/image/backup {source, dest, compress}` | POST | Back up disk or partition (job) |
| `/api/image/restore` | — | Restore is the write route with the manifest read from beside the file |
| `/api/image/verify {path, disk}` | POST | Compare a device with a file (job) |
| `/api/image/cancel`, `/api/image/events?since`, `/api/image/state` | POST / GET | Job control and stream |
| `/api/settings` | GET / POST | Persisted settings (POST `{patch}`) |
| `/api/log?since`, `/api/log/export` | GET / POST | Session command log; export via native Save dialog |
| `/api/fs` | GET | Filesystem knowledge for the UI: capability matrix, format choices per platform, type names |
| `/api/image/pick {kind}` | POST | Native Open / Save dialog for image files (returns `manual: true` without a window) |
| `/api/access/state` | GET | What DiskWorks itself has opened (mount points, WSL attachments, letters) |
| `/api/dev/vhd {action, path, sizeMB}` | POST | Test disks: create + attach a VHD (Windows) or loop device (Linux), or detach (D-021) |
| `/api/changelog` | GET | `CHANGELOG.md` text for the release-notes dialog |
| `/api/window` | POST | Open another native window |
| `/api/export {name, text}` | POST | Native Save dialog + write |
| `/api/pick {kind}` | POST | Native Open / Save-folder dialog (image source, backup destination) |
| `/api/quit` | POST | Shut down (helper first) |

### Helper verbs (JSON over the pipe)

| Verb | Arguments | Does |
|---|---|---|
| `ping` | — | Liveness |
| `inventory` | — | Privileged inventory refinement: raw signatures of RAW partitions, BitLocker status, free-space confirmation, WSL attach state |
| `raw_read` | device, offset, length (≤ 16 MiB) | Read sectors (signatures, browser, hybrid detection on a device) |
| `identify` | disk, start | Filesystem signature of one partition |
| `wsl_state` | — | `wsl.exe --status / --version / -l -v` decoded from UTF-16 |
| `step` | step dict from the planner (+ layout hash for the first step) | Rebuilds the command from the step's structured args (`dw_ops.build_*`) and runs it: PowerShell (`ps`), `sfdisk`, a bundled tool, `unmount`, `raw_zero`; checks the partition still starts where the plan expects |
| `probe` | disk, start | Minimum / maximum size for the resize dialog (`Get-PartitionSupportedSize`, `resize2fs -P`, `ntfsresize --info`, `btrfs filesystem usage`) |
| `vhd` | action, path, sizeMB | Test disk create / detach (D-021) |
| `mount` / `unmount` | device, fstype | Linux mount under `/mnt/<name>` (or `/run/media/diskworks`) with uid/gid options for FAT-family and NTFS; unmount + swapoff |
| `letter` | disk, start | Assign the next free drive letter (Windows) |
| `wsl_mount` / `wsl_unmount` | disk, number, type | `wsl.exe --mount \\.\PHYSICALDRIVEn --partition k --type t --name dw<n>p<k>` / `--unmount`; boot and removable disks refused |
| `image_write` / `image_read` / `image_verify` | path, disk, part, dest, compress, verify | The raw copy loops (§9); volumes locked and dismounted (Windows), unmounted (Linux) or `diskutil unmountDisk force` (macOS) first |
| `browse` / `copyout` | disk, start, path / paths, dest | 7-Zip `l -ba -slt -r-` on the partition device (or a temporary image when the device is refused, D-030) filtered to one folder level; `x -y -bsp1` with percent progress |
| `mac_mount` / `mac_unmount` / `fuse_mount` | device, readonly / fs | `diskutil mount [readOnly]` / `diskutil unmount`; `ntfs-3g` / `ext4fuse` mounts under `/Volumes` |
| `smart` / `smart_selftest` | disk / disk, kind | `smartctl -j -a` (bundled or system, `-d sat` retries), else Windows reliability counters + WMI failure-prediction block, else `diskutil` SMARTStatus → normalised report with verdict; `smartctl -t short|long` (D-037) |
| `cancel` | job id | Cooperative cancel |
| `quit` | — | Exit |

Every verb validates its device arguments against the inventory the helper itself
just read (never trusting the UI's strings), refuses anything on the protection set
without the `override` token the window issues after a typed confirmation, and never
builds shell strings — tools are spawned with argument lists.

## 4. Operation engine

```
 snapshot (Inventory + layout hash)
   -> pending ops (UI edits; each op validated against the snapshot as it is added)
   -> planner: alignment (1 MiB), overlaps, table limits (MBR 4 primaries / extended),
      filesystem limits (§7, probed minimums), protection set, mount state,
      ordering rules, unmount plan
   -> preview layout (what the bars show) + per-op steps with command previews
   -> Apply: for each step { journal -> re-verify layout hash -> run in helper ->
      re-read inventory -> next }; stop on first failure with the state explained
```

**Ordering rules (GParted's, documented in its manual):** shrink the filesystem
before shrinking the partition; grow the partition before growing the filesystem;
a move is shrink-if-needed, relocate (with data copy), then grow; anything offline
unmounts first and remounts after; NTFS resized from Linux gets the "boot Windows
twice" note.

**Sizes and alignment.** Partitions begin and end on 1 MiB boundaries. The usable end
of a disk (`dw_ops.usable_end`) is the 1 MiB boundary below the GPT backup header —
the same position Windows reports as its largest free extent and sfdisk aligns to —
and unallocated regions are reported with that usable size, so the create and resize
dialogs can offer their maximum without the plan being refused later.

**Protection set** (`Partition.locked` with a reason): the whole disk behind the
running OS (`MSFT_Disk.IsSystem`/`IsBoot`; Linux: the disks behind `/`, `/boot`,
`/boot/efi`, `/usr`, active swap), EFI System (`c12a7328-…`), Microsoft Reserved
(`e3c9e316-…`), Recovery (`de94bba4-…`), LDM (`5808c8aa-…`, `af9b60a0-…`), BitLocker
(`manage-bde -status` via the helper; unprivileged heuristic `-FVE-FS-` at byte 3 of
the volume), LUKS, members with `/sys/class/block/X/holders/` non-empty or `TYPE` in
lvm / raid / crypt / dm / mpath, volumes holding a pagefile (`Win32_PageFileUsage`) or
in `/proc/swaps`, devices with `/sys/block/X/ro = 1` or `MSFT_Disk.IsReadOnly`.
Override is offered only for non-boot disks and requires typing the disk model; it is
never offered for the boot disk or the ESP.

**Step expansion examples**

| Op | Windows steps | Linux steps |
|---|---|---|
| Shrink NTFS partition by 20 GiB | `Get-PartitionSupportedSize` (bound) → `Resize-Partition -Size` (resizes NTFS too) → `Update-Disk` | `umount` → `ntfsresize --info` → `ntfsresize --size` (offline) → `sfdisk -N n` with the new size → re-read table (`BLKRRPART`) |
| Shrink ext4 partition | not applicable natively (see Access §8: attach to WSL and run the Linux steps inside the distro when the disk qualifies) | `umount` → `e2fsck -f` → `resize2fs -p <dev> <size>` → `sfdisk -N n` → re-read table (`BLKRRPART`) |
| Create + format exFAT in a gap | `New-Partition -Offset -Size -GptType` → `Format-Volume -FileSystem exFAT -NewFileSystemLabel` → letter | `sfdisk --append` (start, size, type) → re-read table (`BLKRRPART`) → `mkfs.exfat -L` |
| Format FAT32 on a 128 GB partition | own formatter: lock + dismount the volume (`FSCTL_LOCK_VOLUME`, `FSCTL_DISMOUNT_VOLUME`), write BPB / FSInfo / FATs / root cluster through the volume handle, unlock, `Update-Disk` | `mkfs.fat -F 32 -n` |
| Move partition start | deferred (D-001) | `umount` → `sfdisk --move-data -N n` script with the new start → re-read table (`BLKRRPART`) |
| Convert data disk MBR → GPT | destructive: `Clear-Disk` → `Initialize-Disk -PartitionStyle GPT` (typed confirmation) | `sgdisk -g` (in place) |
| Full format NTFS with progress | `diskpart /s` (`format fs=ntfs label=…` without `quick`), parse `N percent completed` on `\r` | `mkntfs` without `-Q` (prints percent) |

## 5. State on disk

`%LOCALAPPDATA%\DiskWorks` (Windows) / `~/.config/diskworks` (Linux):

| Path | Content |
|---|---|
| `settings.json` | `lastTab`, `window {width, height, x, y, maximized}`, `showAllDisks`, `imageDir` (last backup folder), `compress` (zstd level), `verifyAfterWrite`, `accessPrefs` (per-rung opt-outs, e.g. never offer drivers) |
| `instance.json` | `{port, pid}` of the running instance |
| `helper/<uuid>.key` | Windows only: the session authkey, deleted once the helper has connected |
| `journal/<jobId>.json` | Intent journal (§1); finished journals are moved to `journal/done/` and pruned after 30 days |
| `logs/<YYYY-MM-DD>.log` | Session command log (command, exit code, duration, output) |
| `tables/<disk-id>-<ts>.sfdisk` / `.gpt` | Partition-table backups made before every table write (R-019) |
| `webview/` | WebView2 / Qt profile data |

Backups (`.img`, `.img.zst`, `.json` manifests) live wherever the user chooses; the
app remembers only the folder. On Linux the root helper writes journal and log
entries into the user's state dir passed on its command line and `chown`s them to
`PKEXEC_UID`.

## 6. Platform backends

| Need | Windows | Linux | Fallback / note |
|---|---|---|---|
| Inventory (unprivileged) | One PowerShell batch: `Get-CimInstance` on `MSFT_Disk`, `MSFT_Partition`, `MSFT_Volume`, `MSFT_PhysicalDisk` (namespace `root\Microsoft\Windows\Storage`) → `ConvertTo-Json -Compress`; keyed on DiskNumber + Offset (partition numbers renumber) | `lsblk -J -b -O` (udev db supplies FSTYPE / LABEL / UUID / PARTTYPE without root) + `/sys/block/X/{size}`, `/sys/block/X/Xn/{start,size}` for gaps | Windows: `Win32_DiskDrive` / `Win32_DiskPartition` / `Win32_LogicalDisk` + ctypes `IOCTL_STORAGE_QUERY_PROPERTY` (bus, removable, serial with access 0), `FindFirstVolumeW` / `GetVolumeInformationW` / `GetDiskFreeSpaceExW` when `MSFT_Disk` is access-denied (§13). Linux: `udevadm info` when a column is missing; `blkid` only in the helper (`-p`), never unprivileged (stale cache) |
| Unallocated regions | computed: sort by Offset, GPT reserve 17 408 B head and 16 KiB + 1 sector tail; cross-check `MSFT_Disk.LargestFreeExtent` | computed from `/sys` (512-byte units); GPT first/last usable read from the header in the helper | MBR: LBA 0 reserved; logicals inside the extended partition handled separately |
| Filesystem identity of "Unknown" partitions | helper `raw_read` of the first sectors → signature table (NTFS, exFAT, FAT, ext superblock at +1024, xfs `XFSB`, btrfs `_BHRfS_M` at +65536, f2fs, LUKS, swap, ISO9660, HFS+, APFS, `-FVE-FS-`) | `lsblk FSTYPE`, confirmed by `blkid -p` in the helper | signature table is shared code |
| Partition table edits | Storage cmdlets (`New/Remove/Resize/Set-Partition`, `Initialize-Disk`, `Clear-Disk`, `Set-Disk -PartitionStyle`); `diskpart /s` for `clean` / `convert`; `Update-Disk` / `IOCTL_DISK_UPDATE_PROPERTIES` after | `sfdisk` scripts (`--append`, `-N n`, `--part-type/-label/-uuid/-attrs`, `--move-data`, `--backup`, `--dump`); `sgdisk -g` / `--backup`; `BLKRRPART` ioctl (fallback `blockdev --rereadpt`) + `udevadm settle` after; `partx -u` / `partprobe` when the kernel's partition count still differs from the table's (D-035) | VDS COM rejected (deprecated) |
| Filesystem tools | built in: `Format-Volume`, `Resize-Partition` (NTFS/ReFS), `Repair-Volume` / `chkdsk`, `Set-Volume`; own FAT32 formatter for > 32 GB; `mbr2gpt` for the system disk | bundled: e2fsprogs, xfsprogs, btrfs-progs, ntfs-3g (`mkntfs`, `ntfsresize`, `ntfsfix`, `ntfslabel`), dosfstools, exfatprogs, f2fs-tools, util-linux (`mkswap`, `swaplabel`), `fatresize` (libparted fs-resize) for FAT | Linux filesystems from Windows: `wsl --mount --bare` + the tools inside the distro, or WinBtrfs's `mkbtrfs.exe` |
| Mount / unmount | drive letters via `Set-Partition -NewDriveLetter` / `Remove-PartitionAccessPath`; lock + dismount via `FSCTL_LOCK_VOLUME` / `FSCTL_DISMOUNT_VOLUME` (fails on the system volume or a pagefile volume — reported) | `udisksctl mount -b` / `udisksctl unmount -b` (polkit: removable silent, fixed disks prompt; `--no-user-interaction` to probe) else `mount` / `umount` in the helper; busy = `findmnt -J`, `/proc/swaps`, `holders/`, optional `fuser -vm` | `Set-Disk -IsOffline` as the whole-disk alternative to per-volume locks |
| Foreign filesystems | §8 Windows ladder | §8 Linux ladder | in-app read-only browser on both |
| Raw device I/O | ctypes `CreateFileW` on `\\.\PhysicalDriveN` / `\\.\Volume{GUID}` with `FILE_FLAG_NO_BUFFERING` + `FILE_FLAG_WRITE_THROUGH`; sector-aligned `mmap` buffers; `FlushFileBuffers` at the end | `os.open` with `O_RDWR` + `O_DIRECT` + `O_EXCL`; page-aligned `mmap` buffers; `fdatasync` every 64 MiB and at the end; `BLKRRPART` | Python `open(buffering=0)` is not enough on Windows (no share flags, no `NO_BUFFERING`) |
| Elevation | `ShellExecuteExW("runas")` once per session; helper connects back over `AF_PIPE` | `pkexec` once per session; helper connects back over `AF_UNIX`; no pkexec or no display → show the `sudo` line to run the helper by hand | LinkTest's short-command RunAs-with-temp-file pattern kept for one-off admin reads before the helper exists (e.g. BitLocker status) |
| Device change | poll every 3 s (`Get-CimInstance` batch is cheap) | poll every 3 s; `udevadm monitor` subprocess when present to trigger an early poll | manual Refresh |
| BitLocker / LUKS | `manage-bde -status` (helper) or `Win32_EncryptableVolume`; `-FVE-FS-` signature unprivileged | `lsblk FSTYPE crypto_LUKS`; `cryptsetup luksDump` (bundled) | detect only; LUKS open / close via `udisksctl unlock` |
| WSL | `wsl.exe --status`, `-l -v`, `--mount`, `--unmount` with UTF-16LE decoding of stdout / stderr; exit code non-zero from `-l -v` = no distro | — | absence of WSL = rung skipped with the reason |
| Drivers | detect: drive letters whose `GetVolumeInformationW` filesystem name is `Btrfs` / `EXT4`; install: `pnputil /add-driver <inf> /install` from the bundled package (helper) | kernel modules cannot be bundled; `modprobe` only | Secure Boot note for WinBtrfs (`UpgradedSystem` policy) shown when install fails |

**macOS column (0.2.0, `dw_mac.py`).** Inventory: `diskutil list -plist` +
`diskutil info -plist <dev>` per device (unprivileged; Apple documents no schema, so
every key is read with a default); partition starts are unknown to diskutil and are
estimated from cumulative sizes (`approx: true`) until the root helper runs `gpt -r
show /dev/diskN` (exact starts, index-less rows = free space) or `fdisk -d` for MBR.
Synthesized APFS disks (`APFSVolumes` without `Partitions`) are folded into the
physical store's partition, Disk-Utility style. Table edits, formats, labels, resizes
and checks all go through `diskutil` (`partitionDisk`, `addPartition`, `eraseVolume`
with `"Free Space" %noformat%` as delete, `rename`, `resizeVolume` / `apfs
resizeContainer` after their `limits -plist`, `verifyVolume` / `repairVolume`);
formats come from `diskutil listFilesystems -plist` (fallback APFS, JHFS+, ExFAT,
MS-DOS FAT32). Raw I/O on `/dev/rdiskN` (sector-aligned only). Test disks:
`hdiutil attach -nomount -imagekey diskimage-class=CRawDiskImage`. Elevation §1. TCC
prompts D-029. Protection: the physical store behind `/`, EFI, `OSInternal` volumes,
internal disks locked like the Windows system disk.

## 7. Filesystem capability matrix

Tool named per cell; **on** = works on a mounted filesystem, **off** = must be
unmounted / dismounted, — = not possible with any bundled or built-in tool (the UI
greys the action and says so). Windows-side cells refer to what Windows itself can do;
for Linux filesystems on Windows the Access ladder (WSL attach) is the route.

| Filesystem | Create | Grow | Shrink | Move | Check | Label | UUID / serial |
|---|---|---|---|---|---|---|---|
| NTFS | Win `Format-Volume`; Linux `mkntfs` | Win `Resize-Partition` (on); Linux `ntfsresize` (off) | Win `Resize-Partition` (on, bound by `SizeMin`); Linux `ntfsresize` (off) | Linux `sfdisk --move-data` then hidden-sectors fix; Win — (D-001) | Win `chkdsk` / `Repair-Volume`; Linux `ntfsfix` | Win `Set-Volume`; Linux `ntfslabel` | Linux `ntfslabel --new-serial`; Win — |
| exFAT | Win `Format-Volume`; Linux `mkfs.exfat` | — | — | Linux move only | Win `chkdsk`; Linux `fsck.exfat` | Win `Set-Volume`; Linux `exfatlabel` | Linux `tune.exfat -I` |
| FAT32 / FAT16 | Win `Format-Volume` ≤ 32 GB, own formatter above; Linux `mkfs.fat` | Linux `fatresize` (libparted fs-resize; off, ≥ 256 MB); Win — | same as grow | Linux move + BPB hidden-sectors fix | Win `chkdsk`; Linux `fsck.fat` | Win `Set-Volume`; Linux `fatlabel` | Linux `fatlabel -i` |
| ReFS | Win `Format-Volume` where the edition allows (probe) | Win `Resize-Partition` (on) | — | — | Win `Repair-Volume` | Win `Set-Volume` | — |
| ext2 / ext3 / ext4 | Linux `mkfs.ext4` (conservative features, N-007) | Linux `resize2fs` (on for ext3/4, off for ext2) | Linux `resize2fs` (off, after `e2fsck -f`) | Linux move (no fix-up needed) | Linux `e2fsck -f` (off) | Linux `e2label` / `tune2fs -L` | Linux `tune2fs -U` (off) |
| xfs | Linux `mkfs.xfs` | Linux `xfs_growfs` (on, must be mounted) | — (xfs has no shrink) | Linux move | Linux `xfs_repair` (off) | Linux `xfs_admin -L` (off) | Linux `xfs_admin -U` (off) |
| btrfs | Linux `mkfs.btrfs`; Win `mkbtrfs.exe` when WinBtrfs is installed | Linux `btrfs filesystem resize` (on) | Linux `btrfs filesystem resize` (on; slow) | Linux move | Linux `btrfs check` (off) | Linux `btrfs filesystem label` | Linux `btrfstune -u` (off) |
| f2fs | Linux `mkfs.f2fs` | Linux `resize.f2fs` (off) | — | Linux move | Linux `fsck.f2fs` (off) | Linux `f2fslabel` | — |
| Linux swap | Linux `mkswap` | recreate | recreate | Linux move | — | Linux `swaplabel -L` | Linux `swaplabel -U` |
| LUKS | — (D-008) | — | — | Linux move | `cryptsetup luksDump` (detect) | — | — |
| HFS+ (Mac OS Extended) | mac `diskutil eraseVolume JHFS+`; Linux `mkfs.hfsplus` (bundled, 0.2.0) | mac `diskutil resizeVolume` | mac `diskutil resizeVolume` (bound by `limits`) | Linux move | mac `diskutil verifyVolume` / `repairVolume`; Linux `fsck.hfsplus` | mac `diskutil rename` | — |
| APFS (container) | mac `diskutil eraseVolume APFS` | mac `diskutil apfs resizeContainer` | mac `diskutil apfs resizeContainer` (bound by `limits`) | — | mac `diskutil verifyVolume` | mac `diskutil rename` | — |
| ISO9660 / UDF, ReFS on Linux / macOS, HFS+ / APFS on Windows | — | — | — | — | — | — | detect only; readable through the 7-Zip browser |

Cited basis: the GParted features page and manual (ordering rules), `resize2fs(8)`,
`xfs_growfs(8)`, the btrfs `filesystem resize` documentation, exfatprogs 1.4 release
notes (no resize), the `Resize-Partition` documentation ("resizes a partition and the
underlying file system"), and the parted 3.1 `libparted-fs-resize` split (FAT ≥ 256 MB).

## 8. Foreign-filesystem ladders

**Windows — "Open in Windows" for a partition Windows shows as RAW / Unknown**

```
identify filesystem (signature via helper)  -> ext*/btrfs/xfs/f2fs/…
 ├─ rung 1  native?  NTFS/exFAT/FAT/ReFS -> assign a letter (Set-Partition)          [v0.1]
 ├─ rung 2  driver already installed?  a letter reporting Btrfs / EXT4 exists -> use it [v0.1]
 ├─ rung 3  wsl --mount                                                              [v0.1]
 │      needs: WSL 2 (wsl --status), a distro (wsl -l -v exit 0), admin (helper)
 │      refuses: the Windows boot disk; removable flash / SD (RemovableMedia=TRUE) —
 │               "Windows cannot hand USB flash media to WSL" (WSL issue 6011)
 │      does:    wsl --mount \\.\PHYSICALDRIVEn --partition k --type <fs> [--name]
 │               path \\wsl.localhost\<distro>\mnt\wsl\<name>; optional net use X:
 │               Detach = wsl --unmount \\.\PHYSICALDRIVEn (always run on quit)
 │      note:    attaches the WHOLE disk (its other partitions leave Windows meanwhile)
 ├─ rung 4  offer a bundled open-source driver (explicit yes; pnputil in the helper)  [v0.1 btrfs]
 │      btrfs -> WinBtrfs 1.10 (signed; full read/write; drive letter)
 │      ext4  -> Ext4Fsd (D-009: read-only on orphan_file/bigalloc, refuses
 │               inline_data/casefold/encrypt) — offered only after evaluation
 │      xfs   -> none exists; say so
 └─ rung 5  built-in read-only browser + copy-out (D-010 engine)                    [v0.1]
 later:  usbipd-win hand-over of the USB device to WSL (D-011); lwext4 write (D-012)
```

**Linux — "Open in Linux" for NTFS / exFAT / anything not auto-mounted**

```
identify filesystem (lsblk FSTYPE; blkid -p in the helper)
 ├─ rung 1  kernel driver present?  grep -w <fs> /proc/filesystems || modprobe -n <fs>
 │      NTFS: prefer ntfs3 on kernels ≥ 6.12 (mount options windows_names, uid/gid/umask);
 │            older kernels or dirty/hibernated volumes -> rung 2
 │      exFAT: in-kernel exfat (≥ 5.7); vfat; ext4; xfs; btrfs; f2fs
 ├─ rung 2  bundled FUSE helper  (mount.ntfs-3g); legacy exfat-fuse not bundled
 ├─ rung 3  mount: udisksctl mount -b <dev> (polkit) else mount -t <fs> in the helper;
 │      errno tells the story: ENODEV no driver, EINVAL bad superblock / dirty,
 │      EROFS hibernated or unclean NTFS -> explain, offer ntfsfix
 └─ rung 4  built-in read-only browser + copy-out
 a missing kernel module (e.g. ntfs3 absent, as in the WSL kernel) is reported as such:
 the app cannot bring kernel modules; it names the distro package that would
```

**macOS — "Open on this Mac" (0.2.0, `evaluate_mac`)**

```
identify filesystem (diskutil FilesystemType; signature in the helper for RAW)
 ├─ APFS / HFS+ / FAT / exFAT   native read-write  -> diskutil mount                     [ok]
 ├─ NTFS   rung 1  built into macOS, read-only     -> diskutil mount readOnly           [ok]
 │         rung 2  ntfs-3g through macFUSE         -> helper: ntfs-3g [-o backend=fskit] [ok when both installed]
 │                 detect /Library/Filesystems/macfuse.fs + ntfs-3g in /opt/homebrew/bin or /usr/local/bin;
 │                 macFUSE >= 5.1 uses the FSKit backend (no kernel extension); older = Reduced Security note
 │         rung 3  instructions (brew commands)                                          [later]
 │         rung 4  commercial: Paragon NTFS for Mac, Tuxera NTFS (name + website)        [info]
 ├─ ext2/3/4  ExtendFS (FSKit, macOS 15.6+, read-only, auto-mounts) or ext4fuse if present [ok]
 │            else instructions + Paragon extFS for Mac                                  [later / info]
 ├─ btrfs / xfs  read-only browser only (7-Zip via brew; nothing at any price otherwise) [ok / later]
 └─ LUKS / LVM / RAID  no                                                                 [no]
```

**Mac filesystems on Windows and Linux (0.2.0).** Windows: no WSL rung (the WSL kernel
has neither `hfsplus` nor `apfs`, D-031); the 7-Zip browser is the open-source route
(HFS+ and APFS are both readable), and the commercial drivers are named — Paragon HFS+
for Windows (read-write), Paragon APFS for Windows (write experimental), OWC MacDrive.
Linux: HFS+ = kernel `hfsplus` mounted **read-only** (`-o ro`; writing needs the journal
off, explained in the rung); APFS = kernel `apfs` when `apfs-dkms` is installed (Ubuntu ≥
22.04 / Debian; read-only by design) → `mount -t apfs -o ro`, else the browser + the
package hint (`apt install apfs-dkms apfsprogs`) + Paragon APFS for Linux named.

**Read-only browser engine (D-010, D-030).** 7-Zip: `7z.exe` (Windows, bundled), `7zz`
(Linux, bundled), `7zz` from Homebrew on a Mac. Source = the partition device
(`\\.\HarddiskNPartitionM`, `/dev/sdXn`, `/dev/rdiskNsM`); when 7-Zip refuses it the
helper dumps the partition to a temporary image (≤ 8 GiB) and opens that. Listing is one
folder level per call and cached per partition for the session; copy-out streams
7-Zip's percent output as progress. Rung "Read-only browser" is `ok` wherever 7-Zip is
present; "install driver" rungs stay `later`; commercial mentions are `info` rungs with
links and no button.

Every rung result is kept per partition for the session and shown on the Access card
("Opened through WSL at \\wsl.localhost\Ubuntu\mnt\wsl\sda3 — Detach").

## 9. Imaging pipeline

```
 source file --inspect--> kind (raw | hybrid ISO | Windows install ISO | plain ISO |
                         fixed VHD | compressed wrapper) + size + notes
   -> target picker (removable first; guardrails R-044)
   -> confirm by typing the model
   -> helper job:
        unmount / lock every volume on the disk (hold the handles)   [Windows lock+dismount,
                                                                       Linux udisksctl / umount]
        write: 1 MiB blocks from mmap-aligned buffers, write-through; SHA-256 while writing
        flush; re-read partition table (IOCTL_DISK_UPDATE_PROPERTIES / BLKRRPART ioctl)
        verify: re-read the written extent, SHA-256, range-scoped mismatch report
   -> summary: "2.1 GiB of 115 GiB used — the rest is unallocated" + [Create data partition]
```

- **Hybrid detection**: bytes 510–511 = `55 AA` and at least one MBR entry with a
  non-zero type and size (or a protective GPT). Plain (non-hybrid) ISOs are refused for
  dd mode with the explanation that they will not boot from USB; Windows install media
  (`/sources/install.wim|esd|swm` + `/efi/boot/boot*.efi` in the ISO tree) is routed to
  ISO mode.
- **Windows install ISO mode** (R-041): lock → `Clear-Disk` / `sfdisk` new GPT → NTFS
  partition (disk size − 1 MiB − 8 MiB) formatted and labelled from the ISO's volume
  id → extract with pycdlib (UDF first, Joliet fallback; long paths enabled) →
  trailing 8 MiB FAT partition (`EFI System` type) written from the bundled
  `uefi-ntfs.img` → flush → summary "boots on UEFI firmware only". Fallback when the
  user asks for it: FAT32 partition + `wimlib-imagex split install.wim 3800`.
- **Cancel**: between blocks; the first MiB of the target is zeroed so a half-written
  stick is not mistaken for bootable.
- **Backup** (R-042): read the disk or partition in 1 MiB blocks; an all-zero block is
  skipped (seek forward — Linux makes the file sparse by itself; on Windows the output
  gets `FSCTL_SET_SPARSE` first and holes via `FSCTL_SET_ZERO_DATA`, NTFS / ReFS
  targets only, otherwise zeros are written); optional zstd through
  `compression.zstd.ZstdFile` with `nb_workers` = CPU count and the frame checksum on;
  SHA-256 of the raw stream; manifest beside the file:

```json
{ "diskworks": "0.1.0", "created": "2026-09-11T12:00:00Z",
  "source": {"model": "SanDisk Ultra USB 3.0", "serial": "…", "bus": "USB",
             "size": 123010547712, "logicalSector": 512, "physicalSector": 512,
             "table": "gpt", "partition": null},
  "image": {"file": "sandisk-2026-09-11.img.zst", "compression": "zstd", "level": 3,
            "rawSize": 123010547712, "sha256": "…", "zeroBlocks": [[1048576, 5242880], "…"]},
  "layout": { "…": "the Inventory snapshot of the source at backup time" } }
```

- **Restore** (R-043): the write path with the manifest's `rawSize` ≤ target size,
  equal logical sector size, and a warning when model / serial differ.
- **VHDX** (D-003, later): `virtdisk.CreateVirtualDisk` with
  `CREATE_VIRTUAL_DISK_FLAG_CREATE_BACKING_STORAGE` and the physical drive as source;
  restore by `Mount-DiskImage` and imaging the mounted disk.

## 10. Bundled assets and build pipeline

```
fetch-helpers.py  --downloads / copies + SHA-256 pins-->
   bin/win64/
     7zip/7z.exe, 7z.dll, License.txt                       (7-Zip 25.01 x64, LGPL; fetched via 7zr.exe, both SHA-256-pinned)  [0.2.0]
     smartmontools/smartctl.exe, COPYING.txt               (smartmontools 7.5 x64, GPL-2.0-or-later; unpacked from the SHA-256-pinned NSIS installer with 7z.exe)  [0.3.0]
     wimlib/wimlib-imagex.exe, libwim-15.dll, COPYING*      (wimlib 1.14.x; LGPLv3 lib / GPLv3 cli)
     uefi-ntfs.img                                         (pbatard/uefi-ntfs, GPL-2.0, signed binaries)
     drivers/winbtrfs/ (btrfs.inf, .sys, .cat, mkbtrfs.exe, LICENSE)   (WinBtrfs 1.10, GPLv3)
     drivers/ext4fsd/  (installer; only once D-009 is settled)
     xorriso.exe (optional; system-area report)            (GPLv3)
   bin/linux-x86_64/tools/
     sfdisk sgdisk blockdev blkid findmnt mkswap swaplabel wipefs      (util-linux, gdisk)
     mkfs.ext4 resize2fs e2fsck e2label tune2fs e2image               (e2fsprogs)
     mkfs.xfs xfs_growfs xfs_repair xfs_admin                          (xfsprogs)
     mkfs.btrfs btrfs btrfstune                                        (btrfs-progs)
     mkntfs ntfsresize ntfsfix ntfslabel ntfsclone mount.ntfs-3g       (ntfs-3g)
     mkfs.fat fsck.fat fatlabel                                        (dosfstools)
     mkfs.exfat fsck.exfat exfatlabel tune.exfat                       (exfatprogs)
     mkfs.f2fs fsck.f2fs resize.f2fs f2fslabel                         (f2fs-tools)
     cryptsetup                                                        (cryptsetup)
     wimlib-imagex, xorriso                                            (wimlib, xorriso)
     fatresize                                                         (fatresize; wraps libparted-fs-resize for FAT)
     partx, smartctl                                                   (util-linux, smartmontools)  [0.2.2, 0.3.0]
     7zz (+ 7z)                                                        (7zip 26.x; the read-only browser)            [0.2.0]
     mkfs.hfsplus fsck.hfsplus                                          (hfsprogs)                                    [0.2.0]
     + LICENSES/ (one file per package)
tools/make_icon.py -----------------------> assets/diskworks.{ico,png}   (committed)
build.py  (per platform; --wsl for Linux from Windows)
   PyInstaller --onefile --add-data ui --add-data assets --add-data CHANGELOG.md
               --add-binary bin/<tag>/**   (PyInstaller collects each tool's .so deps)
   -> dist/win64/DiskWorks.exe   dist/linux-x86_64/DiskWorks
   + VERSION.txt + THIRD-PARTY-LICENSES.txt (aggregated from bin/**/LICENSES)
build-mac.sh  (on the Mac: xcode-select gate -> .venv -> pip -r requirements.txt -> build.py)
   PyInstaller --windowed --onedir --osx-bundle-identifier com.asirobots.diskworks
               --collect-all webview + PyObjC hidden imports, Qt excluded
   -> dist/mac-arm64/DiskWorks.app (Info.plist: version keys, LSMinimumSystemVersion 12.0,
      NSHighResolutionCapable, NSRemovableVolumesUsageDescription; codesign --force --deep -s -)
   -> dist/mac-arm64/DiskWorks-<ver>-mac-arm64.zip (ditto -c -k --keepParent)
tools/pack_source.py -> dist/DiskWorks-src-<ver>.zip (the tree without dist/, bin/, caches)
```

- **Windows binaries.** 0.2.0 bundles 7-Zip (`python fetch-helpers.py win64`: the console
  `7zr.exe` extracts `7z.exe` + `7z.dll` + `License.txt` from the pinned x64 installer;
  `build.py` adds them with `--add-binary`). wimlib, `uefi-ntfs.img` and the driver
  packages arrive together with ISO mode and the driver offers; `fetch-helpers.py` carries
  their (commented) download entries. Everything else comes from Windows itself
  (PowerShell, `diskpart`, `manage-bde`, `wsl.exe`).
- **Linux tool bundling** (D-020): `fetch-helpers.py linux-tools`, run inside the WSL
  build host, installs the packages (`apt-get install util-linux gdisk e2fsprogs
  xfsprogs btrfs-progs ntfs-3g dosfstools fatresize exfatprogs f2fs-tools cryptsetup-bin wimlib
  xorriso`), copies the listed executables into `bin/linux-x86_64/tools/`, records
  their package versions and SHA-256 in `bin/linux-x86_64/MANIFEST.json`, and copies
  the licence files. PyInstaller's binary analysis collects the shared libraries
  (`libblkid`, `libfdisk`, `libsmartcols`, `libext2fs`, `libntfs-3g`, `libbtrfs`,
  `libwim`, `libgcrypt`…) next to the tools; glibc is excluded, so the glibc floor is
  the build host's — the same floor the Qt runtime already imposes. The helper spawns
  tools with `LD_LIBRARY_PATH=<_MEIPASS>` and `LC_ALL=C`. `tools/check_bundle.py`
  runs every tool with `--version` from a frozen build. An optional static `sfdisk`
  (`util-linux --enable-static-programs=sfdisk`) is a hardening step, not a
  requirement.
- **Python dependencies** (`requirements.txt`): `pywebview` (+ `pythonnet` on Windows,
  `qtpy` + `PySide6` on Linux, the `pyobjc-*` frameworks pulled by pywebview on macOS),
  `pyinstaller`, `pycdlib` (LGPL-2.1), `pyfatfs` (MIT). The read-only browser is the
  bundled 7-Zip executable (D-010), so no Python filesystem parsers.
- **Windows build host**: this PC (Python 3.14.5). **Linux build host**: WSL Ubuntu
  26.04, venv `/opt/diskworksbuild` (same recipe as LinkTest's `/opt/linktestbuild`),
  plus the tool packages above (`7zip`, `hfsprogs` added in 0.2.0). `build.py --wsl` rsyncs
  the tree to `/tmp/diskworks-src` excluding `dist`, `build`, `__pycache__`, `.git`.
  **macOS build host**: Kenton's Apple Silicon Mac, per `MACOS.md` (Command Line Tools,
  Python 3.12+, `./build-mac.sh`); the source travels as `tools/pack_source.py`'s zip.

## 11. Conventions

- **Version** in `diskworks.py` only; every user-visible change bumps it and adds a
  CHANGELOG entry; `REQUIREMENTS.md` statuses follow in the same pass. The header pill
  shows it; clicking opens the release notes of the running version (R-052).
- **Plain language**: labels say what the user wants ("Make this partition smaller",
  "Open in Windows"); technical terms explained inline or in About; tool and OS errors
  mapped to sentences with the raw output one click away.
- **Offline**: no code path opens a network connection. Driver installers ship inside
  the binary.
- **Stdlib + the listed pure-Python packages** for logic; ctypes over subprocess where
  a Windows API exists; subprocess for the bundled tools, PowerShell (one batch per
  refresh, never one process per cmdlet), `diskpart`, `wsl.exe`, `udisksctl`, `pkexec`.
- **Command preview everywhere**: every step that changes a disk can be shown as the
  command or API call it will run, copyable, before Apply (R-020).
- **Helper discipline**: allow-listed verbs, arguments validated against the helper's
  own inventory, protection set enforced in the helper (not only in the UI), argument
  lists never shell strings, one job at a time, journal before the first write,
  layout hash re-verified before every table write.
- **Threads are daemon**; shutdown cancels jobs between blocks, detaches WSL mounts the
  app made, tells the helper to quit, then exits.

## 12. Testing recipes

- **Browser-pane dev server**: `.claude/launch.json` entry `diskworks`
  (`python diskworks.py --no-open --port 8766`) once code exists; same HTML/JS as the
  window. Stop it before testing the exe (the exe would attach via `instance.json`).
- **Fixtures**: `python diskworks.py --fixture <inventory.json>` shows a saved
  inventory (dump one with `python dw_inventory.py > file.json`) for UI work without
  hardware.
- **Test disks**: `POST /api/dev/vhd` (D-021) creates and attaches a VHD (diskpart) on
  Windows or a loop device on Linux; the smoke scripts do this for you:
  `python tools/ops_smoke.py <port> '<ops json>' --vhd <file> <MB>`,
  `python tools/image_smoke.py <port> --vhd <file> <MB>`,
  `python tools/access_smoke.py <port> --vhd <file> <MB>`. Each starts a headless
  instance, unlocks (one UAC / pkexec prompt; `DISKWORKS_NO_ELEVATE=1` skips it when
  the shell is already root / administrator, D-026), runs, detaches, quits. Add
  `--attach` to drive an instance that is already running — the way the frozen
  binaries are tested (`DiskWorks.exe --no-open --port 8799`, then the scripts with
  `--attach`). Placeholders `DISK=VHD`, `GAP0=VHD`, `PART=<disk>:<n>`, `new:<k>` in
  the ops JSON resolve against the live inventory / the queue. The scripts need Python
  3.14 for the `.zst` steps (`compression.zstd`); they also run as root in a udev-less
  container (loop disks; partition nodes come from the D-035 fallback, `apt install fdisk
  gdisk dosfstools ntfs-3g exfatprogs p7zip-full parted` supplies the tools).
- **This PC's SanDisk Ultra USB 3.0** (Disk 1, removable, Linux partitions): the
  foreign-filesystem target — expected results: WSL rung refused (removable flash),
  Ext4Fsd evaluation (D-009), read-only browser lists the Linux root.
- **Imaging**: write a small hybrid ISO (SystemRescue or Alpine) to a spare stick,
  verify, then "Reclaim this drive"; write a Windows 11 ISO in ISO mode and boot a
  laptop from it; back up a 1 GB VHD to `.img.zst`, restore to a second VHD, compare
  SHA-256 of both raw devices.
- **Elevation paths**: cancel the UAC prompt (message, no helper); cancel pkexec
  (126/127); kill the window while the helper idles (helper exits); kill the window mid
  full-format (journal shows the interrupted step on the next start).
- **API driving**: `curl.exe --data-binary @file.json` (PowerShell strips JSON quotes;
  `Invoke-RestMethod` may resend POSTs). One-file exe = bootloader pid + child pid;
  windows belong to the child.
- **Linux**: run `dist/linux-x86_64/DiskWorks` under WSLg; `chmod +x` after copying
  through Windows. Note the WSL kernel has no `ntfs3` / `exfat` / `hfsplus` / `apfs` — the
  Linux ladder's "missing kernel module" message is exercised there.
- **Read-only browser**: `python tools/browse_smoke.py <port> --vhd <file> <MB> [--fs
  ntfs|ext4|hfsplus|exfat] [--copy <dest>]` creates the test disk with one partition,
  drops files on it through the ladder (skipped when this OS cannot mount the
  filesystem), then lists and copies through `/api/access/browse` / `copyout`. Verified in
  WSL as root for ext4 (device source, copy-out) and HFS+ (recognised); on Windows it
  needs one UAC click (D-030 pending).
- **Speed / Space**: Speed on the SanDisk stick with 100 MB, once (the temp file must be
  gone afterwards); Space on the scratchpad folder with throwaway files, trash one, check
  the Recycle Bin.
- **macOS without a Mac**: `python tools/mac_unit.py` (recorded diskutil plists → inventory,
  `gpt -r show` refinement, planner commands, build argv) and `python diskworks.py
  --fixture tools/fixtures/mac/sample-inventory.json` to render the APFS container / HFS+
  stick in the browser pane. On the Mac: the checklist in `MACOS.md` §6.

## 13. Open questions to verify on hardware

| # | Question | Why it matters | How to settle |
|---|---|---|---|
| 1 | Does `Get-CimInstance MSFT_Disk` work for a true standard (non-admin) user? On this PC it works unelevated for an admin account's filtered token; reports say standard users get access denied | Decides whether the CIMV2 / ctypes fallback is needed in practice | Create a standard local user and run the inventory batch |
| 2 | Does stable Windows 11 (25H2) `format` / `Format-Volume` still refuse FAT32 above 32 GB? Insider builds lifted it to 2 TB in 2026 | Whether the own FAT32 formatter is needed at all | Try `Format-Volume -FileSystem FAT32` on a 64 GB VHD |
| 3 | Ext4Fsd 0.71 on the SanDisk stick's ext4 (Ubuntu-created, likely `orphan_file`): read-only? refuses? stable? | Whether rung 4 for ext4 can be offered (D-009) | Install on a test machine, mount, copy in and out, `e2fsck` afterwards on Linux |
| 4 | ~~Does PyInstaller collect the shared libraries of `--add-binary` executables?~~ **Settled 2026-09-11 for the build host:** the frozen Linux binary ran `sfdisk`, `mkntfs`, `mkfs.btrfs`, `mkfs.exfat`, `mkfs.ext4`, `losetup` from `_MEIPASS` with `LD_LIBRARY_PATH` set (16-step queue on a loop disk). Still open: the same on Fedora / Debian stable (glibc floor = build host's) | D-020 | Run `tools/ops_smoke.py` against the frozen binary in a Fedora and a Debian container |
| 5 | Second one-file extraction as root via `pkexec`: how long on the 244 MB binary on a real desktop, and does `TMPDIR` clearing land it in `/tmp` reliably? In WSL (no pkexec, helper started directly) the helper was ready in about 3 s | Session start latency after Unlock; whether a separate `--onedir` helper is worth it | Time it on a real Ubuntu desktop with pkexec |
| 6 | `udisksctl` absent on minimal desktops (it is absent in WSL): how common on the target Linux machines? | Whether rung 3 must default to `mount` in the helper | Check the fleet's distro images |
| 7 | ~~`AF_PIPE` from an elevated helper to the unelevated window's pipe?~~ **Settled 2026-09-11:** works; the UAC-elevated helper (source and frozen) connected and authenticated within 0.5 s | — | — |
| 8 | Which `--type` values does this WSL kernel accept (`ext4`, `btrfs`, `xfs` compiled in; `exfat`, `ntfs3` absent)? | Rung 3 scope and the "format Linux filesystem from Windows" path | `wsl --mount --bare` a VHD and try each `mkfs` + mount inside the distro |
| 9 | ~~`dissect.*` AGPL-3.0 acceptable for an internal tool? Otherwise 7-Zip CLI~~ **Settled 2026-09-11:** 7-Zip (D-010); Kenton chose open-source read-only where it exists, and Mac filesystems made 7-Zip the better fit | — | — |
| 10 | Does 7-Zip on Windows open `\\.\HarddiskNPartitionM` directly, or does the temporary-image fallback kick in? | D-030; browsing a 100 GB partition through a temp image is slow and capped at 8 GiB | `python tools/browse_smoke.py <port> --vhd <file> 512 --fs ntfs --copy <dir>` on Windows with one UAC click; read `source` in the output |
| 11 | macOS: does the detached `osascript` helper connect back within the accept window, and does the Removable-Volumes prompt appear on the first `open("/dev/rdiskN")` from the window process? | Unlock and imaging on a Mac | `MACOS.md` §6 steps 2–3 |
| 12 | macOS: `diskutil addPartition` / `resizeVolume` behaviour on a stick partitioned by Windows (no Apple boot / recovery partitions) | Create / resize on foreign sticks | `MACOS.md` §6 step 4 with a Windows-formatted stick |

## Sources consulted (2026-09-11)

Windows: `wsl --mount` documentation and WSL issue 6011 (removable media);
WSL2-Linux-Kernel `config-wsl`; `MSFT_Disk` / `MSFT_Partition` class references;
`Get-Disk`, `Resize-Partition`, `Get-PartitionSupportedSize`, `Format-Volume`,
`Mount-DiskImage` cmdlet docs; `IOCTL_DISK_GET_DRIVE_LAYOUT_EX`,
`IOCTL_STORAGE_QUERY_PROPERTY`, `FSCTL_LOCK_VOLUME`, `FSCTL_ALLOW_EXTENDED_DASD_IO`,
`FSCTL_SET_SPARSE`, `IOCTL_STORAGE_GET_HOTPLUG_INFO`; "Restricted Direct Disk Access
and Volume Access in Windows" (dn653576); `MBR2GPT`, `chkdsk`, `manage-bde` command
docs; "VDS is transitioning to Windows Storage Management API"; WebView2 security
guidance and WebView2Feedback issues 4672 / 932 / 3128; "Elevate through
ShellExecute"; FAT32 32 GB → 2 TB coverage (BleepingComputer, Windows Latest, Apr 2026);
Dev Drive / ReFS docs; WinBtrfs (maharmstone/btrfs) 1.10; Ext4Fsd (bobranten);
Paragon LFS for Windows; dissect.extfs / dissect.xfs; python `ext4` (read-only);
Ext4Windows (WinFsp + lwext4); e2fsprogs_win32 (abandoned); Cygwin e2fsprogs.

Linux: `lsblk(8)`, `sfdisk(8)`, `resize2fs(8)`, `xfs_growfs(8)`, `blkid(8)`,
`pkexec(1)`, `udisksctl(1)`; btrfs `filesystem resize` docs; GParted features page,
manual, "moving space between partitions", pkexec announcement, FAT < 256 MB issue 245;
KPMcore 4.0 (libparted → sfdisk); parted 3.5 release; pyparted releases; gptfdisk
revisions; util-linux `--enable-static-programs`, static-toolbox; `sfdisk --move-data`
issues 1176 / 848; udisks2 polkit actions; ArchWiki Udisks; UAPI Discoverable
Partitions Specification; ntfs3 kernel docs, Phoronix NTFS3 in Linux 7.0, syzbot ntfs3
report Apr 2026, NTFS-3G 2026.2.25 / 2026.7.7, "NTFS Remake" coverage; exfatprogs
releases, exFAT in Linux 5.7; xfs.org shrinking support; refsprogs; QtWebEngine
platform notes, QTBUG-79710; PyInstaller operating mode, issues 8711 / 6842; pyfatfs;
lwext4; libguestfs FAQ; Debian live-boot(7), Fedora LiveOS.

Imaging: Rufus FAQ, ChangeLog, `src/iso.c`, `src/drive.c`, `src/vhd.c`, issues 843 /
964; pbatard/uefi-ntfs; pycdlib 1.20.0 and issue 65; wimlib 1.14.5; etcher-sdk and
blockmap; usbimager; Ventoy licence and issue 3224; partclone; Sysinternals EULA;
FFU deployment docs; UEFI 2.11 §13 Media Access; xorrisofs manual; Python 3.14
`compression.zstd` and What's New; CPython issue 128111 / bpo-25639 (device reads on
Windows); Red Hat I/O alignment article.

macOS and 0.2.0 (2026-09-11): `diskutil(8)`, `gpt(8)`, `hdiutil(1)`, `fdisk(8)` man pages;
Technical Note TN2065 (`do shell script`, administrator privileges, the 5-minute cache);
Apple Developer Forums / DTS on TCC and helper tools; "Accessing files on removable volumes";
PyInstaller manual (macOS bundles, `--windowed`, onedir, `--osx-bundle-identifier`, codesign);
macFUSE 5.1 release notes (FSKit backend); ntfs-3g-mac tap; ExtendFS (App Store, macOS 15.6);
ext4fuse; apfs-linux / apfs-dkms and apfsprogs; hfsprogs; Paragon HFS+ / APFS / extFS / NTFS
product pages; OWC MacDrive; Tuxera NTFS; 7-Zip history (`\\.\` device opening, APFS / HFS
support, `-slt`, `-bsp1`); Debian 7zip package (binary renamed `7z` in 26.x);
Blackmagic Disk Speed Test and CrystalDiskMark (decimal MB/s, cache bypass); DaisyDisk;
`FILE_FLAG_NO_BUFFERING` alignment rules; `O_DIRECT` in `open(2)`; `F_NOCACHE` / `F_FULLFSYNC`
in `fcntl(2)`; freedesktop.org Trash specification; `Microsoft.VisualBasic.FileIO.FileSystem`
(SendToRecycleBin).
