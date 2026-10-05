# DiskWorks

DiskWorks is a disk and partition manager for people who would rather not open a
terminal. It shows your drives the way Windows Disk Management does and offers the
detailed operations of GParted, on Windows, Linux and macOS, from one window.

**Status: 0.3.1** (Windows x64 and Linux x86_64 built and tested here; macOS Apple Silicon
builds with `./build-mac.sh`, see `MACOS.md` — not yet run on a Mac). See
`ARCHITECTURE.md` for how it is built, `REQUIREMENTS.md` for what it must do, what is
done and what was deliberately left out, and `CHANGELOG.md` for releases.

Run from source: `python diskworks.py` (needs `pip install -r requirements.txt` for the
native window; `--browser` works with the standard library alone). Build:
`python build.py` on Windows, `python build.py --wsl --linux-only` for the Linux binary
(after `python3 fetch-helpers.py linux-tools` inside WSL has collected the bundled tools);
`python fetch-helpers.py win64` fetches 7-Zip and smartctl for the Windows build; on a Mac `./build-mac.sh`.

| Tab | What it does |
|---|---|
| **Disks** | Every disk as a bar, every partition as a segment, unallocated space hatched. Select a partition or a disk to see its details. Queue changes (create, delete, format, resize, move, label, flags, drive letter, table type), preview the result, then Apply. **Drive health** reads a disk's S.M.A.R.T. data and sums it up as Good, Caution or Bad, with temperature, hours, wear, bad sectors and a short self-test. |
| **Image** | Write an image or a bootable ISO to a USB drive (like Rufus), back up a disk or partition to a compressed image, restore it later, verify what was written. |
| **Access** | When Windows or Linux cannot open a partition by itself (ext4 on Windows, NTFS on a bare Linux box), DiskWorks works through the ways to make it readable and writable from the operating system and tells you which one it used. |
| **Speed** | A Blackmagic-style disk speed test: sequential write and read with the cache off, live dials, a run history and a table of which video formats the drive can sustain. |
| **Space** | DaisyDisk-style rings or a WizTree-style treemap of what fills a drive or folder, the largest files anywhere in the scan and the totals per file type; click into folders, move files to the Recycle Bin / Trash or delete them, reveal them in the file manager. |
| **Log** | Every command the app ran, with its output. |

**Self-contained and offline.** Everything DiskWorks needs is inside the one
executable: the partition and filesystem tools on Linux, the Windows-side helpers,
and the drivers it may offer to install. It never phones home and never downloads
anything at run time.

**Current version:** see `CHANGELOG.md`. Once code exists the version lives once in
`diskworks.py` (`VERSION`) and is shown in the app's header; clicking it opens the
release notes for that version.

Related projects with the same conventions: `../LinkTest` (network toolbox).
