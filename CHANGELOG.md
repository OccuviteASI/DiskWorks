# Changelog

All notable changes to DiskWorks. The version number is defined once, in
`diskworks.py` (`VERSION`), and read by the UI and `build.py`. Clicking the version
pill in the app shows the section below that matches the running version.

## 0.4.4 - 2026-11-13

- **Fix:** switching tabs no longer nudges the page sideways. Tabs long enough to scroll
  (Disks, Speed) showed a scrollbar and the others did not, so the page was 15 pixels
  narrower on some tabs than on others. The scrollbar's space is now always kept, so every
  tab lines up the same.

## 0.4.3 - 2026-11-13

- **Fix (Windows update):** the previous version's `DiskWorks.old.exe` was left beside the
  new one. Windows keeps the old file locked until the one-file launcher of the old
  DiskWorks has exited, which happens a moment after the app inside it, so the single
  delete attempt at start-up failed. The new version now waits for that launcher too, and
  keeps retrying the delete in the background for two minutes. A leftover from an earlier
  update is removed the same way, including the `DiskWorks.old-<number>.exe` name used when
  an older `.old.exe` was still in the way.

## 0.4.2 - 2026-11-13

Updates and the tray now follow the same standard as LinkTest.
- **Settings** (the new cog button next to About) holds the options, instead of About:
  *Updates* (the state, what changed, *Check for updates*, *Update DiskWorks*, and *Check for
  updates when DiskWorks starts and every 30 minutes*) and *Window* (*Minimize to the tray*,
  *Keep running in the tray when the window is closed*, and *Quit DiskWorks*).
- **Update now** button next to the version, pulsing brighter/darker whenever an update is
  waiting; one click updates (it turns into "Updating…"). DiskWorks now checks GitHub every
  30 minutes while it runs as well as at launch, so the button shows up soon after a release.
- Clicking the version opens What's new and checks for updates straight away; the result
  sits at the top with *Check for updates* and *Update now*.
- The launch prompt (*Update now* / *Skip for now*) appears once per launch, in one window,
  even after a reload.
- **Close to tray** (optional, off by default): closing the window keeps DiskWorks running in
  the tray. A window comes back from the tray the way it went in, maximized or not, and a
  minimized window's parked position (-32000) is no longer saved.
- The tray menu is *Open DiskWorks*, *Update DiskWorks* (with the new version when one is
  waiting) and *Quit DiskWorks*; the icon gets an amber dot while an update waits. macOS has
  no tray icon: the Dock keeps DiskWorks, as with LinkTest.

## 0.4.1 - 2026-11-13

- **Fix (Linux, macOS):** a window that reopened where a disconnected monitor used to be
  now moves back onto a screen. The check always failed on these platforms because
  pywebview's screen list is a proxy that only pretends to be a function; DiskWorks now
  reads it as the list it is. (Windows was not affected.)
- First release published to test the online update from 0.4.0.

## 0.4.0 - 2026-11-13

- **Update DiskWorks online.** Every time DiskWorks starts it asks GitHub for the newest
  release; when there is one it shows what changed since your version and asks **Update
  now** or **Skip for now** (skipping asks again at the next launch). *Check for updates*
  (About, the tray icon's menu, or the version pill) does the same on demand. *Update now*
  downloads this platform's build, checks its SHA-256 against the
  one published with it, puts it in place of the running DiskWorks and restarts into it; the
  release notes open after the restart. Nothing is changed if the download or the check
  fails. A disk job in progress blocks the update. When an update is waiting, the version
  pill reads *Update to …* and the tray icon gets an amber dot. The launch check can be
  turned off in About (*Check for updates when DiskWorks starts*). Running from source, it
  tells you to `git pull` instead.
- **System tray.** A DiskWorks icon in the tray (the menu bar on a Mac) while it runs, like
  SNMPathy's: *Open DiskWorks*, a status line (version, unlocked, a running job, an update),
  *Minimize to tray*, *Check for updates… / Update to …*, *Quit DiskWorks*. With
  *Minimize to tray* on (the default, also in About), minimizing the window hides it into the
  tray; clicking the icon or starting DiskWorks again brings it back. Quit refuses while a
  disk job runs. On a Linux desktop without a tray (GNOME without the AppIndicator
  extension) there is no icon and minimizing works as before.
- **Builds for all three platforms on GitHub.** `.github/workflows/release.yml` (replaces
  `build-mac.yml`) builds Windows, Linux and macOS for every push to `main`, runs the unit
  checks and a headless start on each, and publishes `DiskWorks-<v>-win64.exe`,
  `DiskWorks-<v>-linux-x86_64` and `DiskWorks-<v>-mac-arm64.zip` with `.sha256` files on
  the release `v<v>`, which becomes the latest release once all three are in. The Mac build
  moved to GitHub's `macos-15` runner (macOS 14 runners are retired), and the Windows build
  fetches 7-Zip 25.01 from 7-Zip's versioned GitHub release (the unversioned 7-zip.org link
  now serves a newer build, which the pinned checksum rightly refused).

## 0.3.3 - 2026-10-05

- **macOS build alongside every build.** PyInstaller cannot make a Mac app on Windows or
  Linux, so a GitHub workflow (`.github/workflows/build-mac.yml`, now `release.yml`) now builds
  `DiskWorks.app` on an Apple Silicon Mac runner for every push to `main`: it runs the
  macOS unit checks, builds with `build-mac.sh`, starts the app headless to check it, and
  attaches `DiskWorks-<version>-mac-arm64.zip` to the release `v<version>`.
- `python build.py` on Windows or Linux now also fills `dist/mac-arm64/`: the Mac app for
  this version when GitHub has finished building it, the source zip, and
  `BUILD-ON-MAC.txt` with steps both for running the ready-made app (including getting past
  the "unidentified developer" block) and for compiling it yourself on a Mac.
  `--no-mac` skips this.

## 0.3.2 - 2026-10-05

- **Space:** click the middle of the rings to go back up one folder, the way DaisyDisk does.
  Inside a folder the centre shows an ↑ arrow and highlights on hover; at the top of the
  scan it does nothing. The **Up** button is now shown only in the Treemap view.

## 0.3.1 - 2026-10-05

- **Space:** free space is now hidden by default in both the rings and the treemap, so the
  picture shows only what is on the drive. Tick **Show free space** above the picture to
  bring it back at a drive's root; the choice is remembered.

## 0.3.0 - 2026-10-04

- **Disks: Drive health (S.M.A.R.T.).** Right-click a disk (or use its Actions strip) →
  *Drive health*. DiskWorks reads the drive's self-monitoring data and sums it up as
  **Good**, **Caution** or **Bad** with the reasons spelled out, then shows the figures:
  temperature, time powered on, power cycles, total written, life used, reallocated /
  pending / uncorrectable sectors (or spare capacity and media errors on NVMe), the full
  attribute table with current / worst / threshold / raw, the NVMe health log, the
  self-test history, and a *Run short self-test* button. *Copy report* puts it all on the
  clipboard. Reading needs Unlock (the drive is asked directly); nothing is changed on the
  disk. A chip on the disk bar and in the detail pane remembers the last verdict.
  Sources: smartctl (smartmontools 7.5, now bundled on Windows and Linux; `brew install
  smartmontools` on a Mac), with fallbacks to Windows' storage reliability counters and
  the WMI failure-prediction attribute block, and to `diskutil`'s SMART status on macOS.
- **Space: treemap view (WizTree style).** A *Rings / Treemap* switch above the picture.
  The treemap tiles the folder being viewed: folders are frames with a name strip and their
  contents drawn inside (four levels deep, tiles too small to see are summed), files are
  coloured by what they are (video, pictures, music, archives, documents, code, programs
  and system files, disk images, databases) with a legend. Hover for size and share, click
  a folder's name strip or double-click to open it, click a file to tick it. Free space is
  part of the picture at a drive's root, as in the rings.
- **Space: largest files and file types.** Two new sections under the picture: the 200
  largest files anywhere in the scan (tickable for Move to Trash / Delete, click to jump to
  the folder) and the totals per file type with share bars. Version suffixes such as
  `.so.1` do not count as a type.
- Linux: `partx` and `smartctl` join the bundled tools (`fetch-helpers.py linux-tools`);
  Windows: `fetch-helpers.py win64` unpacks `smartctl.exe` from the smartmontools installer
  (SHA-256 pinned) next to 7-Zip. Both are optional: without them the health panel says
  what it could not read.

## 0.2.2 - 2026-09-22

- **Fix (Linux, operations):** after a partition-table write the engine now checks that the
  kernel actually picked the table up. On some kernels the re-read request succeeds without
  registering the new partitions (seen with loop devices on Linux 6.18), so
  `/dev/<disk>p1` never appeared and the format step failed with "did not appear". When the
  kernel's partition count differs from the table's, the helper runs `partx -u` (per-partition
  add / remove / resize), then `partprobe`, and logs both counts. Nothing runs when they agree.
  `partx` joins the bundled util-linux tools.
- **Fix (Linux, operations):** the helper's safety check before each step ("the disks changed
  since the plan was made") compared against the raw `lsblk` view, which has no partition-table
  type where no udev database exists (containers, minimal systems) and therefore refused every
  step on such a disk. It now compares against the same refined view the window planned on.
- Verified in a Linux container (root, loop disks, Python 3.14): 17 operation steps in three
  queues (GPT table, create ext4 / exFAT, label, shrink ext4, check, delete, FAT32 format,
  zero-wipe), the imaging round trip (raw write + verify, `.img.zst` backup + manifest +
  verify, sparse raw backup) and the ext4 access ladder (mount, list, unmount).

## 0.2.1 - 2026-09-22

- **Fix (Speed):** testing the Windows system drive (C:) failed with "Access is denied".
  Windows does not let a normal user create a file at the root of C:, so the test file
  is now placed in a writable folder on the chosen drive — your Temp folder or home
  folder when they live on that drive, else the drive root, else a `DiskWorks-speedtest`
  folder — and the status line says where. The file is still removed afterwards.
- The project is now a git repository (`.gitignore` keeps builds, bundled tools, caches
  and machine-specific fixtures out).

## 0.2.0 - 2026-09-11

- **Unlock pill:** a padlock icon shows the state at a glance — closed while locked
  ("Locked · Unlock"), open once unlocked, dotted while the prompt is up. The same
  icon marks locked partitions and locked actions instead of an emoji.
- **Disks:** resize by dragging. A partition that can grow into the free space to its
  right, or shrink, gets a grip on its right edge; drag it and a label follows with the
  new size and the change; release to queue the resize, Esc cancels. One pending
  resize per partition (a new drag replaces the old one). The Resize dialog stays for
  exact numbers; both produce the same operation.
- **Speed tab** (new): a Blackmagic-style disk speed test. Writes a temporary file to
  the chosen drive and reads it back with the operating-system cache switched off
  (Windows `FILE_FLAG_NO_BUFFERING`, Linux `O_DIRECT`, macOS `F_NOCACHE`), repeats
  until stopped, shows live dials, a run history with averages and a "Will it work?"
  table of common video formats. The test file is removed afterwards.
- **Space tab** (new): DaisyDisk-style rings of what uses a drive or folder. Click a
  slice or a row to look inside, tick files or folders and move them to the Recycle
  Bin / Trash (or delete them for good after typing "delete"), or reveal them in the
  file manager. Only items inside the scanned folder can be removed; system folders
  are refused.
- **Access:** the read-only browser is here, built on **7-Zip** (bundled on Windows
  and Linux, `brew install 7zip` on a Mac): list folders and files with sizes and
  dates on any partition 7-Zip can read — APFS, HFS+, ext2/3/4, NTFS, FAT, UDF, ISO —
  and copy files or folders out to a chosen folder with progress. Mac partitions are
  now handled on Windows and Linux: HFS+ read-only through the Linux kernel driver,
  APFS through `apfs-dkms` where installed, the browser everywhere. Where only a
  commercial driver adds write access (Paragon HFS+ / APFS for Windows, MacDrive,
  Paragon extFS / NTFS for Mac, Tuxera) the ladder names it with its website; nothing
  is installed.
- **macOS:** DiskWorks builds and runs on a Mac (Apple Silicon or Intel): the Disks
  view through `diskutil`, Unlock through the standard administrator prompt,
  operations through `diskutil partitionDisk / addPartition / eraseVolume /
  resizeVolume / apfs resizeContainer / rename / verifyVolume`, imaging on
  `/dev/rdiskN`, the Access ladder for NTFS (built-in read-only, ntfs-3g through
  macFUSE) and ext (ExtendFS / ext4fuse), Speed and Space. Build with
  `./build-mac.sh`; `MACOS.md` has the steps, the first-start prompts and a smoke
  checklist. The port was written and unit-checked on Windows against recorded
  `diskutil` output; its first run on Mac hardware is still to come.
- **Linux:** HFS+ can be created and checked with the bundled `mkfs.hfsplus` /
  `fsck.hfsplus`; 7-Zip (`7zz`) is bundled.
- **Fix:** a speed-test read pass no longer truncates the file it is about to read.

## 0.1.1 - 2026-09-11

- **Disks:** right-click a partition, unallocated space or a disk (in the bars or the
  volume table) for its options — the same operations as before plus shortcuts to
  **Open in Windows / Linux…** (Access tab) and **Back up…** / **Write an image to
  this disk…** (Image tab). Locked entries show why.
- **Disks:** the detail card now starts with an **Actions** strip right under the
  title, and the details are laid out in tiles across the whole card instead of one
  narrow column.
- **Disks:** the disk that runs this computer is hidden by default; **Show system
  disks** in the toolbar brings it back (every launch starts hidden).
- **Fix:** the New-partition dialog offered a size that could exceed the free space
  (it rounded the gap to whole MiB, and a partition ending at the very end of a disk
  was not aligned). Sizes are now limited to the 1 MiB-aligned maximum that fits —
  the same limit Windows applies — and typed values snap back into range. Unallocated
  space is reported with that usable size.
- **Fix:** the Image tab's default backup file name could repeat itself when the
  backup mode was opened from the Disks tab; the detail pane no longer lists a drive
  letter twice.

## 0.1.0 - 2026-09-11

First runnable build, Windows and Linux.

- **Disks:** Disk-Management-style view — a volume table plus one proportional bar
  per disk with unallocated space hatched; disk and partition detail panes (type
  GUIDs with names, offsets, sector alignment, BitLocker state, protection reasons);
  live refresh when drives come and go. Viewing never needs administrator rights.
- **Unlock:** one administrator prompt (UAC / polkit) starts a small helper process
  for the rest of the session; the window itself never runs elevated. Once unlocked,
  partitions Windows calls "Unknown" are identified from their own signature (ext4,
  btrfs, xfs, LUKS, swap, …) and BitLocker status is shown.
- **Operations (GParted-style queue):** create, delete, format (NTFS, exFAT, FAT32,
  FAT16, ext2/3/4, xfs, btrfs, f2fs, swap; ReFS where the edition allows), label,
  drive letter, resize (grow / shrink with the true minimum measured first), check,
  new partition table (GPT / MBR), wipe. Changes queue up and preview in the bars;
  **Apply** runs them with live progress; **Show commands** lists exactly what will
  run. System, EFI, Reserved, Recovery, BitLocker, LUKS and RAID/LVM partitions are
  protected; the running system volume allows only resize / label / check.
- **Image:** write raw images and hybrid ISOs (also `.gz` / `.xz` / `.zst` / `.bz2`
  compressed, fixed VHD) to a removable drive with read-back verification; back a
  disk or one partition up to a sparse `.img` or a zstd `.img.zst` with a JSON
  manifest (checksum, layout); restore a backup; verify a file against a drive.
  Windows installation ISOs are recognised and explained (ISO mode comes later).
- **Access:** for a partition this computer cannot open by itself, the ladder shows
  what applies — built into the OS, an installed driver, attach to WSL (Windows;
  with the removable-flash and boot-disk exclusions explained), driver install
  offers and the read-only browser (both marked as coming later) — and opens it
  with one click: drive letter on Windows, kernel-driver mount or the bundled
  ntfs-3g on Linux, `wsl --mount` for Linux filesystems on fixed disks.
- **Log:** every command with exit code, timing and output; saveable.
- **Self-contained:** the Linux binary carries 53 partition and filesystem tools
  (util-linux, gdisk, e2fsprogs, xfsprogs, btrfs-progs, ntfs-3g, dosfstools,
  exfatprogs, f2fs-tools, fatresize, cryptsetup) with their libraries; the Windows
  binary needs only what ships with Windows. Nothing is downloaded at run time.
- Not yet: partition move, Windows-install ISO mode, FAT32 above 32 GiB on Windows,
  the in-app read-only browser, driver installers (see `REQUIREMENTS.md`).

## 0.0.1 - 2026-09-11

- Architecture and requirements documents written (`ARCHITECTURE.md`,
  `REQUIREMENTS.md`); no code yet.
- Decisions recorded: name DiskWorks; unprivileged window plus one elevated helper
  per session; Storage cmdlets on Windows and sfdisk on Linux as the partition
  engines; Linux tools bundled in the binary; foreign filesystems handled by a
  ladder that ends in an offer to install an open-source driver; imaging in the
  first build = raw/hybrid-ISO writing, Windows install ISO mode, backup and
  restore to compressed sparse images.
