#!/usr/bin/env python3
"""Persistent desktop sticky-note widget for Focus Guardian (PySide6 or GTK3 + KWin keepBelow)."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path

from config import CACHE_PATH, DATA_DIR
from env_checks import should_spare_oled

STATE_PATH = DATA_DIR / "state.json"
KWIN_SCRIPT_NAME = "agy_focus_sticky"


def _load_kwin_rule() -> None:
    js_path = Path(__file__).resolve().parent / "kwin_sticky_rule.js"
    if not js_path.exists():
        return
    subprocess.run(
        ["qdbus-qt6", "org.kde.KWin", "/Scripting", "unloadScript", KWIN_SCRIPT_NAME],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["qdbus-qt6", "org.kde.KWin", "/Scripting", "loadScript", str(js_path), KWIN_SCRIPT_NAME],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["qdbus-qt6", "org.kde.KWin", "/Scripting", "start"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _unload_kwin_rule() -> None:
    subprocess.run(
        ["qdbus-qt6", "org.kde.KWin", "/Scripting", "unloadScript", KWIN_SCRIPT_NAME],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _read_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    if CACHE_PATH.exists():
        try:
            raw = json.loads(CACHE_PATH.read_text())
            return {
                "verdict": "UNKNOWN",
                "reason": "Waiting for next 5m check",
                "tasks": raw.get("tasks", []),
                "events": raw.get("events", []),
            }
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _format_texts() -> tuple[str, str, str, list, list]:
    state = _read_state()
    verdict = state.get("verdict", "UNKNOWN")
    reason = state.get("reason", "Waiting for next 5m check")
    tasks = state.get("tasks", [])
    events = state.get("events", [])

    if events:
        ev_names = ", ".join(e.get("summary", "Event") for e in events[:2])
        status_text = f"In scheduled event: {ev_names}"
    elif verdict == "DISTRACTED":
        status_text = f"DISTRACTED: {reason}"
    elif verdict == "FOCUSED":
        status_text = f"FOCUSED: {reason}"
    else:
        status_text = f"{verdict}: {reason}"

    if tasks:
        lines = [f"• {t.get('title', '')}" for t in tasks[:8]]
        if len(tasks) > 8:
            lines.append(f"  (+{len(tasks) - 8} more)")
        tasks_text = "To-Do Tasks:\n" + "\n".join(lines)
    else:
        tasks_text = "No pending Google Tasks loaded.\n(Run `cli.py auth` if not signed in.)"

    return verdict, status_text, tasks_text, tasks, events


def _run_gtk3() -> int:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    css = Gtk.CssProvider()
    css.load_from_data(
        b"""
        window.sticky-root {
            background-color: rgba(23, 26, 35, 0.90);
            border: 1px solid rgba(106, 166, 255, 0.45);
            border-radius: 12px;
        }
        label.sticky-header { color: #6aa6ff; font-weight: bold; font-size: 13px; }
        label.sticky-status { color: #9aa3b2; font-size: 12px; }
        label.sticky-status-focused { color: #50d890; font-size: 12px; }
        label.sticky-status-distracted { color: #f5b971; font-weight: bold; font-size: 12px; }
        label.sticky-tasks { color: #e6e9ef; font-size: 12px; }
        label.sticky-footer { color: #50d890; font-size: 11px; }
        """
    )
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )

    win = Gtk.Window(title="Focus Guardian Sticky Note")
    win.get_style_context().add_class("sticky-root")
    win.set_decorated(False)
    win.set_skip_taskbar_hint(True)
    win.set_skip_pager_hint(True)
    win.set_keep_below(True)
    win.set_default_size(340, -1)
    win.set_resizable(False)
    win.stick()

    screen = win.get_screen()
    visual = screen.get_rgba_visual()
    if visual is not None and screen.is_composited():
        win.set_visual(visual)
    win.set_app_paintable(True)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.set_margin_top(12)
    box.set_margin_bottom(12)
    box.set_margin_start(14)
    box.set_margin_end(14)

    header = Gtk.Label(label="Focus Guardian", xalign=0)
    header.get_style_context().add_class("sticky-header")
    status_lbl = Gtk.Label(label="Starting...", xalign=0)
    status_lbl.set_line_wrap(True)
    status_lbl.set_max_width_chars(42)
    status_lbl.get_style_context().add_class("sticky-status")

    tasks_lbl = Gtk.Label(label="Loading tasks...", xalign=0)
    tasks_lbl.set_line_wrap(True)
    tasks_lbl.set_max_width_chars(42)
    tasks_lbl.get_style_context().add_class("sticky-tasks")


    for w in (header, status_lbl, tasks_lbl):
        box.pack_start(w, False, False, 0)
    win.add(box)

    allow_quit = [False]

    def on_delete(_w: object, _e: object) -> bool:
        return not allow_quit[0]

    def on_button_press(_w: object, event: Gdk.EventButton) -> bool:
        if event.button == 1:
            win.begin_move_drag(1, int(event.x_root), int(event.y_root), event.time)
        return False

    win.connect("delete-event", on_delete)
    win.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
    win.connect("button-press-event", on_button_press)

    def refresh() -> bool:
        verdict, status_text, tasks_text, _tasks, _events = _format_texts()
        display = Gdk.Display.get_default()
        screen_names: list[str] = []
        if display is not None:
            for i in range(display.get_n_monitors()):
                mon = display.get_monitor(i)
                model = mon.get_model() if mon else None
                if model:
                    screen_names.append(model)
        if should_spare_oled(screen_names, verdict):
            win.hide()
            return True
        if not win.get_visible():
            win.show_all()

        ctx = status_lbl.get_style_context()
        for cls in ("sticky-status", "sticky-status-focused", "sticky-status-distracted"):
            ctx.remove_class(cls)
        if verdict == "FOCUSED":
            ctx.add_class("sticky-status-focused")
        elif verdict == "DISTRACTED":
            ctx.add_class("sticky-status-distracted")
        else:
            ctx.add_class("sticky-status")

        status_lbl.set_text(status_text)
        tasks_lbl.set_text(tasks_text)
        return True

    _load_kwin_rule()
    win.show_all()
    refresh()
    GLib.timeout_add_seconds(5, refresh)

    def _quit(*_args: object) -> None:
        allow_quit[0] = True
        _unload_kwin_rule()
        Gtk.main_quit()

    signal.signal(signal.SIGINT, _quit)
    signal.signal(signal.SIGTERM, _quit)
    Gtk.main()
    return 0


def main() -> int:
    try:
        import PySide6  # noqa: F401
        return _run_pyside6()
    except ImportError:
        return _run_gtk3()


def _run_pyside6() -> int:
    from PySide6.QtCore import QPoint, Qt, QTimer
    from PySide6.QtGui import QCloseEvent, QGuiApplication, QMouseEvent
    from PySide6.QtWidgets import QApplication, QFrame, QLabel, QVBoxLayout, QWidget

    class StickyNoteWindow(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self._drag_offset: QPoint | None = None
            self._allow_quit = False

            self.setWindowTitle("Focus Guardian Sticky Note")
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnBottomHint
                | Qt.WindowType.Tool
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setFixedWidth(340)

            root_layout = QVBoxLayout(self)
            root_layout.setContentsMargins(0, 0, 0, 0)

            self.card = QFrame()
            self.card.setObjectName("card")
            self.card.setStyleSheet(
                """
                #card {
                    background-color: rgba(23, 26, 35, 225);
                    border: 1px solid rgba(106, 166, 255, 110);
                    border-radius: 12px;
                }
                QLabel { color: #e6e9ef; background: transparent; }
                """
            )
            card_layout = QVBoxLayout(self.card)
            card_layout.setContentsMargins(14, 12, 14, 12)
            card_layout.setSpacing(8)

            self.header_lbl = QLabel("Focus Guardian")
            self.header_lbl.setStyleSheet("font-size: 13px; font-weight: 700; color: #6aa6ff;")
            card_layout.addWidget(self.header_lbl)

            self.status_lbl = QLabel("Starting...")
            self.status_lbl.setWordWrap(True)
            self.status_lbl.setStyleSheet("font-size: 12px; color: #9aa3b2;")
            card_layout.addWidget(self.status_lbl)

            self.tasks_lbl = QLabel("Loading tasks...")
            self.tasks_lbl.setWordWrap(True)
            self.tasks_lbl.setStyleSheet("font-size: 12px; color: #e6e9ef;")
            card_layout.addWidget(self.tasks_lbl)

            self.footer_lbl = QLabel("Allowed: Analysis & Discrete Mathematics")
            self.footer_lbl.setWordWrap(True)
            self.footer_lbl.setStyleSheet("font-size: 11px; color: #50d890;")
            card_layout.addWidget(self.footer_lbl)

            root_layout.addWidget(self.card)

            self.timer = QTimer(self)
            self.timer.timeout.connect(self.refresh)
            self.timer.start(5000)
            self.refresh()

        def closeEvent(self, event: QCloseEvent) -> None:
            if not self._allow_quit:
                event.ignore()
                return
            super().closeEvent(event)

        def mousePressEvent(self, event: QMouseEvent) -> None:
            if event.button() == Qt.MouseButton.LeftButton:
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()

        def mouseMoveEvent(self, event: QMouseEvent) -> None:
            if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
                self.move(event.globalPosition().toPoint() - self._drag_offset)
                event.accept()

        def mouseReleaseEvent(self, event: QMouseEvent) -> None:
            self._drag_offset = None
            event.accept()

        def refresh(self) -> None:
            verdict, status_text, tasks_text, _tasks, _events = _format_texts()
            screen_names = [s.name() for s in QGuiApplication.screens()]
            if should_spare_oled(screen_names, verdict):
                if self.isVisible():
                    self.hide()
                return
            if not self.isVisible():
                self.show()

            if verdict == "DISTRACTED":
                self.status_lbl.setStyleSheet("font-size: 12px; font-weight: 600; color: #f5b971;")
            elif verdict == "FOCUSED":
                self.status_lbl.setStyleSheet("font-size: 12px; color: #50d890;")
            else:
                self.status_lbl.setStyleSheet("font-size: 12px; color: #9aa3b2;")
            self.status_lbl.setText(status_text)
            self.tasks_lbl.setText(tasks_text)
            self.adjustSize()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    _load_kwin_rule()
    win = StickyNoteWindow()
    win.show()

    def _quit(*_args: object) -> None:
        win._allow_quit = True
        _unload_kwin_rule()
        app.quit()

    signal.signal(signal.SIGINT, _quit)
    signal.signal(signal.SIGTERM, _quit)
    sig_timer = QTimer()
    sig_timer.timeout.connect(lambda: None)
    sig_timer.start(500)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
