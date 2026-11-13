"""System-tray icon for the native window, to the same standard as LinkTest's tray.py
(ARCHITECTURE.md §1, decision D-041).

Minimizing the window hides it to the tray (Settings > Window, on by default) and, if chosen
there, so does closing it.  The icon's menu has Open DiskWorks, Update DiskWorks (with the
new version when one is waiting) and Quit DiskWorks; clicking the icon opens the window.
Starting DiskWorks again while it sits in the tray also brings the window back.

Backends:
  Windows  pystray (win32), in its own thread.
  Linux    Qt's QSystemTrayIcon on the Qt GUI thread pywebview runs (the Linux build ships
           PySide6): StatusNotifierItem (KDE, GNOME with the AppIndicator extension) and the
           XEmbed tray, with a real menu (pystray's X11 backend has none).
  macOS    no tray icon (the Dock already keeps the app), as in LinkTest.
When no tray is there, `ok` stays False and minimizing / closing behave normally, so a
window can never be hidden with no way back.  The window handling itself (hide, show,
close to tray) lives in diskworks.App; this module only shows the icon and its menu.
"""
from __future__ import annotations

import io
import os
import sys
import threading
from typing import Callable

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
BLUE = (11, 110, 245, 255)
DOT = (240, 173, 0, 255)


def make_icon(badge: bool = False, size: int = 64):
    """The DiskWorks logo (a drive on a blue tile), with an amber dot when an update is waiting."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 32
    white = (255, 255, 255, 255)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(8 * s), fill=BLUE)
    d.rounded_rectangle([6 * s, 9 * s, 26 * s, 23 * s], radius=int(3 * s), outline=white, width=max(2, int(2.5 * s)))
    d.rounded_rectangle([6 * s, 9 * s, 13 * s, 23 * s], radius=int(3 * s), fill=white)
    d.ellipse([19 * s, 14 * s, 23 * s, 18 * s], fill=white)
    if badge:
        r = 6.5 * s
        cx, cy = size - r - 1, r + 1
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=DOT, outline=white, width=max(1, int(1.5 * s)))
    return img


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _hand_off(fn: Callable[[], None]) -> None:
    """Menu actions leave the tray / GUI thread at once (they may wait on the window)."""
    threading.Thread(target=fn, daemon=True, name="tray-action").start()


class Tray:
    def __init__(self, on_open: Callable[[], None], on_update: Callable[[], None], on_quit: Callable[[], None],
                 update_label: Callable[[], str | None]):
        self.on_open, self.on_update, self.on_quit = on_open, on_update, on_quit
        self.update_label = update_label        # None hides the menu item
        self.ok = False                         # the icon is really in a tray
        self.has_menu = False
        self.why = ""                           # why there is no icon
        self.badge = False
        self._icon = None                       # pystray.Icon (Windows)
        self._qt = None                         # QSystemTrayIcon (Linux)

    # -- start / stop --------------------------------------------------------
    def start(self) -> bool:
        """Windows: call from any thread. Linux: call once pywebview's Qt application runs."""
        try:
            if IS_MAC:
                self.why = "macOS keeps DiskWorks in the Dock"
            elif IS_WIN:
                self._start_pystray()
            else:
                self._start_qt()
        except Exception as e:
            self.ok, self.why = False, f"{e.__class__.__name__}: {e}"
        return self.ok

    def _start_pystray(self) -> None:
        import pystray
        item = pystray.MenuItem
        menu = pystray.Menu(
            item("Open DiskWorks", lambda *_: _hand_off(self.on_open), default=True),
            item(lambda _i: self.update_label() or "Update DiskWorks", lambda *_: _hand_off(self.on_update),
                 visible=lambda _i: self.update_label() is not None),
            pystray.Menu.SEPARATOR,
            item("Quit DiskWorks", lambda *_: _hand_off(self.on_quit)),
        )
        self._icon = pystray.Icon("DiskWorks", make_icon(), "DiskWorks", menu)
        ready = threading.Event()

        def setup(icon):
            try:
                icon.visible = True
                self.ok = True
            except Exception as e:
                self.why = str(e)
            ready.set()

        def run():
            try:
                self._icon.run(setup=setup)
            except Exception as e:
                self.why = str(e)
                ready.set()
        threading.Thread(target=run, daemon=True, name="tray").start()
        ready.wait(5)
        self.has_menu = self.ok

    def _start_qt(self) -> None:
        import time
        from qtpy.QtCore import QTimer
        from qtpy.QtWidgets import QApplication
        for _ in range(100):
            if QApplication.instance() is not None:
                break
            time.sleep(0.1)
        app = QApplication.instance()
        if app is None:
            self.why = "the Qt application did not start"
            return
        done = threading.Event()

        def build():
            try:
                self._build_qt()
            except Exception as e:
                self.ok, self.why = False, f"{e.__class__.__name__}: {e}"
            done.set()
        QTimer.singleShot(0, app, build)
        done.wait(15)

    def _build_qt(self) -> None:
        from qtpy.QtGui import QIcon, QPixmap
        from qtpy.QtWidgets import QMenu, QSystemTrayIcon
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.why = "no system tray on this desktop (on GNOME, install the AppIndicator extension)"
            return
        menu = QMenu()
        menu.addAction("Open DiskWorks").triggered.connect(lambda *_: _hand_off(self.on_open))
        upd = menu.addAction("Update DiskWorks")
        upd.triggered.connect(lambda *_: _hand_off(self.on_update))
        menu.addSeparator()
        menu.addAction("Quit DiskWorks").triggered.connect(lambda *_: _hand_off(self.on_quit))

        def before_show():
            label = self.update_label()
            upd.setVisible(label is not None)
            upd.setText(label or "Update DiskWorks")
        menu.aboutToShow.connect(before_show)
        before_show()
        pm = QPixmap()
        pm.loadFromData(_png(make_icon()), "PNG")
        icon = QSystemTrayIcon(QIcon(pm))
        icon.setToolTip("DiskWorks")
        icon.setContextMenu(menu)

        def activated(reason):
            if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
                _hand_off(self.on_open)
        icon.activated.connect(activated)
        icon.show()
        self._qt, self._qt_menu = icon, menu
        self.ok = self.has_menu = True

    def _on_qt(self, fn) -> None:
        from qtpy.QtCore import QTimer
        from qtpy.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app, fn)

    def stop(self) -> None:
        if not self.ok:
            return
        self.ok = False
        try:
            if self._icon is not None:
                self._icon.stop()
            if self._qt is not None:
                self._on_qt(lambda: self._qt.hide())
        except Exception:
            pass

    # -- helpers ---------------------------------------------------------------
    def refresh(self, badge: bool | None = None) -> None:
        """Re-read the menu labels; badge = an update is waiting (amber dot on the icon)."""
        if not self.ok:
            return
        changed = badge is not None and badge != self.badge
        if badge is not None:
            self.badge = badge
        try:
            if self._icon is not None:
                if changed:
                    self._icon.icon = make_icon(self.badge)
                self._icon.update_menu()
            if self._qt is not None and changed:
                def apply():
                    from qtpy.QtGui import QIcon, QPixmap
                    pm = QPixmap()
                    pm.loadFromData(_png(make_icon(self.badge)), "PNG")
                    self._qt.setIcon(QIcon(pm))
                self._on_qt(apply)
        except Exception:
            pass

    def set_title(self, text: str) -> None:
        try:
            if self._icon is not None:
                self._icon.title = text[:120]
            if self._qt is not None:
                self._on_qt(lambda: self._qt.setToolTip(text[:120]))
        except Exception:
            pass

    def notify(self, message: str) -> None:
        try:
            if self._icon is not None and getattr(self._icon, "HAS_NOTIFICATION", False):
                self._icon.notify(message, "DiskWorks")
            if self._qt is not None:
                self._on_qt(lambda: self._qt.supportsMessages() and self._qt.showMessage("DiskWorks", message))
        except Exception:
            pass
