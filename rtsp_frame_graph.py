#!/usr/bin/env python3
"""Compare RTSP camera measurements and inspect live video."""

import argparse
from bisect import bisect_left, bisect_right
from datetime import datetime
from itertools import islice
from operator import itemgetter
from pathlib import Path
import signal
import sys
from threading import Condition, Thread
from time import monotonic

import numpy as np
import pyqtgraph as pg
from PySide6 import __version__ as pyside_version
from PySide6.QtCore import QEvent, QRectF, QSettings, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QGraphicsPixmapItem, QGraphicsScene,
    QGraphicsView, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QVBoxLayout, QWidget,
)

from capture import (BITRATE_LOOKAHEAD_SECONDS, BITRATE_SAMPLE_SECONDS, Camera,
                     ConnectionInputError, DISPLAY_SECONDS, HISTORY_SECONDS, av, camera_url,
                     remembered_login)
from graph_exports import GraphRecording, save_graph_snapshot

CAMERAS = ("Camera A", "Camera B")
COLORS = (("#67aaff", "#2b6bb8"), ("#ffae5c", "#a74a0a"))
CODEC_LABELS = {"h264": "H.264", "hevc": "H.265", "av1": "AV1"}


class ImageConverter:
    """Convert only the most recent preview frame away from the GUI thread."""

    def __init__(self):
        self.condition = Condition()
        self.pending = self.result = None
        self.closed = False
        self.thread = Thread(target=self.run, name="Preview conversion")
        self.thread.start()

    def submit(self, attempt, frame):
        with self.condition:
            self.pending = (attempt, frame)
            self.condition.notify()

    def take(self):
        with self.condition:
            result, self.result = self.result, None
            return result

    def close(self):
        with self.condition:
            self.closed = True
            self.pending = self.result = None
            self.condition.notify()

    def run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.pending is not None)
                if self.closed:
                    return
                (attempt, frame), self.pending = self.pending, None
            try:
                pixels = frame.to_ndarray(format="rgba")
                result = (attempt, pixels, "")
            except (av.FFmpegError, ValueError):
                result = (attempt, None, "Could not convert the decoded frame")
            with self.condition:
                if not self.closed:
                    self.result = result


class VideoViewport(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.frame_bounds = QRectF()
        self.pixel_ratio = None
        self.picture = QGraphicsPixmapItem()
        self.scene().addItem(self.picture)
        self.setBackgroundBrush(QColor("#131518"))
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.actual_pixels = False
        self.set_mode(False)

    def set_mode(self, actual_pixels):
        self.actual_pixels = actual_pixels
        policy = (Qt.ScrollBarPolicy.ScrollBarAsNeeded if actual_pixels
                  else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(policy)
        self.setVerticalScrollBarPolicy(policy)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag if actual_pixels
                         else QGraphicsView.DragMode.NoDrag)
        self.adjust_scale()

    def adjust_scale(self):
        bounds = self.frame_bounds
        self.pixel_ratio = self.viewport().devicePixelRatioF()
        if self.actual_pixels:
            center = self.mapToScene(self.viewport().rect().center())
            self.resetTransform()
            scale = 1 / self.pixel_ratio
            self.scale(scale, scale)
            self.centerOn(center)
        elif not bounds.isEmpty():
            # Refit from the scene's native image size, including after DPR changes.
            self.resetTransform()
            self.fitInView(bounds, Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.actual_pixels:
            self.adjust_scale()

    def showEvent(self, event):
        super().showEvent(event)
        self.adjust_scale()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.DevicePixelRatioChange:
            QTimer.singleShot(0, self.adjust_scale)

    def display(self, pixels):
        height, width, _ = pixels.shape
        image = QImage(pixels.data, width, height, pixels.strides[0], QImage.Format.Format_RGBA8888)
        self.picture.setPixmap(QPixmap.fromImage(image))
        bounds = QRectF(0, 0, width, height)
        if bounds != self.frame_bounds:
            self.frame_bounds = bounds
            self.setSceneRect(bounds)
            self.adjust_scale()
        elif self.viewport().devicePixelRatioF() != self.pixel_ratio:
            self.adjust_scale()

    def clear(self):
        self.picture.setPixmap(QPixmap())


class LiveView(QWidget):
    def __init__(self, name, camera, parent):
        flags = (Qt.WindowType.Window | Qt.WindowType.CustomizeWindowHint
                 | Qt.WindowType.WindowTitleHint | Qt.WindowType.WindowSystemMenuHint
                 | Qt.WindowType.WindowMaximizeButtonHint | Qt.WindowType.WindowCloseButtonHint)
        super().__init__(parent, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(f"{name} · Live View")
        self.resize(850, 530)
        self.camera = camera
        self.attempt = None
        self.converter = ImageConverter()
        self.viewport = VideoViewport()

        self.mode = QComboBox()
        self.mode.addItems(["Fit to window", "Actual pixels (1:1)"])
        self.mode.currentIndexChanged.connect(lambda index: self.viewport.set_mode(index == 1))
        self.resolution = QLabel("—")
        self.playback = QLabel()
        toolbar = QHBoxLayout()
        toolbar.addWidget(self.mode)
        toolbar.addWidget(self.resolution)
        toolbar.addStretch()
        toolbar.addWidget(self.playback)
        layout = QVBoxLayout(self)
        layout.addLayout(toolbar)
        layout.addWidget(self.viewport, 1)

        self.camera.set_preview(True)
        self.timer = QTimer(self, interval=16)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def refresh(self):
        attempt = self.camera.attempt if self.camera.status == "Live" else None
        if attempt is not self.attempt:
            self.viewport.clear()
            self.attempt = attempt
            self.playback.setText("Waiting for video")
        if attempt is None:
            status = self.camera.status
            if status.startswith(("Connection lost", "Retry scheduled")):
                status = f"Reconnecting · {status}"
            elif status == "Connecting" and self.resolution.text() != "—":
                status = "Reconnecting"
            if self.playback.text() != status:
                self.playback.setText(status)
            return

        result = self.converter.take()
        if result is not None and result[0] is attempt:
            _, pixels, error = result
            if error:
                self.viewport.clear()
                self.playback.setText(error)
            else:
                self.viewport.display(pixels)
                height, width, _ = pixels.shape
                self.resolution.setText(f"{width} × {height}")
                self.playback.setText("Live")

        frame = attempt.preview.take()
        if frame is not None:
            self.converter.submit(attempt, frame)

    def closeEvent(self, event):
        self.timer.stop()
        self.camera.set_preview(False)
        self.converter.close()
        super().closeEvent(event)


class FrameSizeAxis(pg.AxisItem):
    """Label square-root plot coordinates with the original frame sizes."""

    def tickStrings(self, values, scale, spacing):
        return [f"{(value * scale) ** 2:.4g}" for value in values]


class ComparisonGraph(pg.PlotWidget):
    following_changed = Signal(bool)

    def __init__(self):
        super().__init__(axisItems={"left": FrameSizeAxis("left")})
        self.following_live = True
        self.plot_origin = None
        self.elapsed = 0
        self.measurements = []
        self.use_dark = QApplication.palette().window().color().lightness() < 128
        self.setBackground("#171a1f" if self.use_dark else "#ffffff")
        plot = self.getPlotItem()
        plot.setLabel("bottom", "Elapsed time", units="s")
        plot.getAxis("bottom").enableAutoSIPrefix(False)
        plot.setLabel("left", "Encoded frame size (square-root scale)", units="KiB")
        frame_axis = plot.getAxis("left")
        frame_axis.enableAutoSIPrefix(False)
        # The curve is in Mbps. Scale the axis to bps before applying SI prefixes.
        bitrate_axis = plot.getAxis("right")
        bitrate_axis.setScale(1_000_000)
        plot.setLabel("right", f"Centered bitrate ({BITRATE_LOOKAHEAD_SECONDS:g} s lookahead)", units="bps")
        for side in ("left", "right", "bottom"):
            axis = plot.getAxis(side)
            axis.setTextPen("#d0d7e0" if self.use_dark else "#334151")
            axis.setPen("#818d9a")
        plot.showGrid(x=True, y=True, alpha=0.18)
        plot.setMouseEnabled(x=True, y=False)
        plot.vb.setMouseMode(pg.ViewBox.PanMode)
        plot.vb.setLimits(minXRange=0.5, maxXRange=HISTORY_SECONDS)
        plot.setMenuEnabled(False)
        plot.hideButtons()

        self.bitrate = pg.ViewBox(enableMouse=False, enableMenu=False)
        self.bitrate.setEnabled(False)  # Let the main view receive drag and wheel events.
        plot.scene().addItem(self.bitrate)
        bitrate_axis.linkToView(self.bitrate)
        self.bitrate.setXLink(plot)
        plot.vb.sigResized.connect(self.align_axes)
        self.align_axes()
        self.frame_series = []
        self.frame_extrema = []
        self.rate_series = []
        self.legend = plot.addLegend(offset=(12, 12),
                                     labelTextColor="#dce3ec" if self.use_dark else "#253548")
        for name, variants in zip(CAMERAS, COLORS, strict=True):
            color = variants[0 if self.use_dark else 1]
            frame_pen = QColor(color)
            frame_pen.setAlpha(130)
            impulses = pg.PlotCurveItem(pen=pg.mkPen(frame_pen), connect="pairs",
                                        name=f"{name} frame size")
            extrema = pg.ScatterPlotItem(size=3, pen=None, brush=pg.mkBrush(color))
            rate = pg.PlotCurveItem(pen=pg.mkPen(color, width=2), connect="finite",
                                    name=f"{name} bitrate")
            plot.addItem(impulses)
            plot.addItem(extrema)
            self.bitrate.addItem(rate)
            self.legend.addItem(rate, rate.name())
            self.frame_series.append(impulses)
            self.frame_extrema.append(extrema)
            self.rate_series.append(rate)
        self.draw_timer = QTimer(self, interval=0, singleShot=True)
        self.draw_timer.timeout.connect(self.draw_visible)
        plot.vb.sigRangeChangedManually.connect(self.pause_following)
        plot.vb.sigXRangeChanged.connect(self.schedule_draw)
        plot.vb.sigResized.connect(self.schedule_draw)
        self.update_plot(None, None, [])

    def align_axes(self):
        view = self.getPlotItem().vb
        self.bitrate.setGeometry(view.sceneBoundingRect())
        self.bitrate.linkedViewChanged(view, pg.ViewBox.XAxis)

    def update_plot(self, origin, now, measurements):
        new_session = origin != self.plot_origin
        self.plot_origin = origin
        self.elapsed = max(0, now - origin) if origin is not None else 0
        self.measurements = measurements
        view = self.getPlotItem().vb
        view.setLimits(xMin=max(0, self.elapsed - HISTORY_SECONDS),
                       xMax=max(DISPLAY_SECONDS, self.elapsed))
        if new_session:
            self.set_following(True)
        if self.following_live:
            self.show_live_range()
        self.draw_visible()

    def schedule_draw(self):
        self.draw_timer.start()

    def set_following(self, following):
        if following != self.following_live:
            self.following_live = following
            self.following_changed.emit(following)

    def pause_following(self, axes):
        if axes[0]:
            self.set_following(False)

    def back_to_live(self):
        self.set_following(True)
        self.show_live_range()
        self.draw_visible()

    def show_live_range(self):
        right = max(DISPLAY_SECONDS, self.elapsed)
        self.setXRange(right - DISPLAY_SECONDS, right, padding=0)

    @staticmethod
    def points_between(history, left, right, *, neighbors=False):
        first = bisect_left(history, left, key=itemgetter(0))
        last = bisect_right(history, right, key=itemgetter(0))
        if neighbors:
            first, last = max(0, first - 1), min(len(history), last + 1)
        return np.asarray(list(islice(history, first, last))).reshape(-1, 2).T

    def draw_visible(self):
        self.draw_timer.stop()
        view = self.getPlotItem().vb
        left, right = view.viewRange()[0]
        columns = max(1, int(view.width()))
        origin = self.plot_origin or 0
        max_frame = max_rate = 0
        for stats, impulses, extrema, line in zip(self.measurements, self.frame_series,
                                                  self.frame_extrema, self.rate_series):
            times, sizes = self.points_between(stats.frames, origin + left, origin + right)
            times = times - origin
            if sizes.size:
                max_frame = max(max_frame, float(sizes.max()))
                if right - left > DISPLAY_SECONDS and sizes.size > columns:
                    # Mark the extremes without filling the zoomed-out chart with impulses.
                    pixels = np.minimum(((times - left) / (right - left) * columns).astype(int),
                                        columns - 1)
                    starts = np.r_[0, np.flatnonzero(np.diff(pixels)) + 1]
                    lows = np.minimum.reduceat(sizes, starts)
                    sizes = np.maximum.reduceat(sizes, starts)
                    times = left + (pixels[starts] + 0.5) * (right - left) / columns
                    distinct = lows < sizes
                    extrema.setData(np.r_[times, times[distinct]], np.sqrt(np.r_[sizes, lows[distinct]]))
                    impulses.setData([], [])
                else:
                    extrema.setData([], [])
                    heights = np.zeros(2 * len(sizes))
                    heights[1::2] = np.sqrt(sizes)
                    impulses.setData(np.repeat(times, 2), heights)
            else:
                impulses.setData([], [])
                extrema.setData([], [])
            times, rates = self.points_between(stats.rates, origin + left, origin + right,
                                               neighbors=True)
            if rates.size:
                line.setData(times - origin, rates)
                valid_rates = rates[np.isfinite(rates)]
                if valid_rates.size:
                    max_rate = max(max_rate, float(valid_rates.max()))
            else:
                line.setData([], [])
        self.setYRange(0, np.sqrt(max_frame) * 1.06 or 1, padding=0)
        self.bitrate.setYRange(0, max_rate * 1.06 or 1, padding=0)


class CameraPanel(QGroupBox):
    def __init__(self, window, slot, name, camera, color):
        super().__init__(name)
        self.window = window
        self.camera = camera
        self.name = name
        self.preview = None
        self.settings_key = f"cameras/{slot}"
        self.unsaved_login = None
        self.capture_address = None
        self.fields = {
            "url": QLineEdit(placeholderText="rtsp://camera/stream"),
            "username": QLineEdit(),
            "password": QLineEdit(echoMode=QLineEdit.EchoMode.Password),
        }
        for key in ("url", "username"):
            self.fields[key].setText(window.settings.value(f"{self.settings_key}/{key}", "", type=str))
        form = QFormLayout()
        for label, field_name in (("RTSP URL", "url"), ("Username", "username"), ("Password", "password")):
            form.addRow(label, self.fields[field_name])
            self.fields[field_name].textEdited.connect(self.clear_error)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {color}; font-weight: bold;")
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        controls = QHBoxLayout()
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.restart_button = QPushButton("Restart")
        self.preview_button = QPushButton("Live View")
        for button, callback in ((self.start_button, self.start), (self.stop_button, self.stop),
                                 (self.restart_button, self.restart), (self.preview_button, self.open_preview)):
            button.clicked.connect(callback)
            controls.addWidget(button)
        controls.addStretch()

        self.field_error = QLabel()
        self.field_error.setWordWrap(True)
        self.field_error.hide()
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.field_error)
        layout.addLayout(controls)
        layout.addWidget(self.status)
        layout.addWidget(self.summary)
        self.refresh()

    def clear_error(self):
        self.field_error.hide()
        for field in self.fields.values():
            field.setStyleSheet("")

    def start(self):
        self.clear_error()
        try:
            url = camera_url(self.fields["url"].text(), self.fields["username"].text(),
                             self.fields["password"].text())
        except ConnectionInputError as error:
            field = self.fields[error.field_name]
            field.setStyleSheet("QLineEdit { border: 1px solid #d34b4b; }")
            field.setFocus()
            self.field_error.setText(str(error))
            self.field_error.show()
            return
        now = monotonic()
        self.window.begin_graph_session(now)
        self.unsaved_login = remembered_login(url)
        self.capture_address = self.unsaved_login[0]
        self.camera.start(url, now)
        self.refresh()

    def stop(self):
        if self.preview is not None:
            self.preview.close()
        self.camera.stop()
        self.unsaved_login = None
        self.refresh()

    def restart(self):
        self.camera.restart()
        self.refresh()

    def open_preview(self):
        if self.preview is None:
            self.preview = LiveView(self.name, self.camera, self.window)
            self.preview.destroyed.connect(self.preview_closed)
        self.preview.show()
        self.preview.raise_()
        self.preview.activateWindow()

    def preview_closed(self):
        self.preview = None

    def refresh(self):
        if self.unsaved_login is not None and self.camera.measurements.frame_count:
            url, username = self.unsaved_login
            self.unsaved_login = None
            settings = self.window.settings
            settings.setValue(f"{self.settings_key}/url", url)
            settings.setValue(f"{self.settings_key}/username", username)
            settings.sync()
            if settings.status() != QSettings.Status.NoError:
                self.field_error.setText("Couldn't save the last connection settings.")
                self.field_error.show()
        busy = self.camera.enabled or self.camera.attempt is not None
        for field in self.fields.values():
            field.setReadOnly(busy)
        self.start_button.setEnabled(not busy)
        self.stop_button.setEnabled(self.camera.enabled)
        self.restart_button.setEnabled(self.camera.enabled and self.camera.action != "restart")
        self.preview_button.setEnabled(self.camera.enabled)
        status = self.camera.status
        if status == "Live" and self.camera.codec:
            codec = self.camera.codec
            status = f"Live · {CODEC_LABELS.get(codec, codec.upper())}"
        if self.status.text() != status:
            self.status.setText(status)

    def refresh_summary(self):
        stats = self.camera.measurements
        bitrate = "Buffering…" if self.camera.status == "Live" else "—"
        if stats.rate is not None:
            bitrate = f"{stats.rate:.3f} Mb/s"
        fps = f"{stats.frame_count / stats.active_seconds:.2f}" if stats.active_seconds >= 1 else "—"
        megabytes = stats.received / 1_000_000
        overall = f"{megabytes * 8 / stats.active_seconds:.3f}" if stats.active_seconds > 0 else "—"
        summary = (
            f"Centered bitrate  {bitrate}  ·  Avg FPS  {fps}\n"
            f"Frames  {stats.frame_count:,}  ·  I-frames  {stats.intra_count:,}\n"
            f"MBs received  {megabytes:,.3f} ({overall} Mbps overall)"
        )
        if stats.unknown_sizes:
            summary += f"\nFrame sizes unavailable for {stats.unknown_sizes:,} frames"
        self.summary.setText(summary)

    def snapshot_details(self):
        url = self.capture_address
        if url is None:
            try:
                url, _ = remembered_login(camera_url(self.fields["url"].text()))
            except ConnectionInputError:
                url = ""
        return self.name, self.status.text(), url, self.summary.text()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("RTSP Frame Graph")
        self.resize(1100, 670)
        self.origin = None
        self.graph_session = 0
        self.next_plot = 0
        self.graph_running = False
        self.closing = False
        self.recording = None
        self.settings = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                                  "rtsp-frame-graph", parent=self)
        self.cameras = [Camera(), Camera()]
        self.graph = ComparisonGraph()
        self.panels = [
            CameraPanel(self, slot, name, camera, colors[0 if self.graph.use_dark else 1])
            for slot, name, camera, colors in zip(("a", "b"), CAMERAS, self.cameras, COLORS, strict=True)
        ]
        controls = QHBoxLayout()
        for panel in self.panels:
            controls.addWidget(panel, 1)
        group_layout = QVBoxLayout()
        self.group_buttons = {}
        for title, control in (("Start Both", "start_button"), ("Stop Both", "stop_button"),
                               ("Restart Both", "restart_button"), ("Live View Both", "preview_button")):
            button = QPushButton(title)
            targets = [(panel.fields["url"], getattr(panel, control)) for panel in self.panels]
            self.group_buttons[button] = targets
            button.clicked.connect(lambda checked, targets=targets: self.run_group_action(targets))
            group_layout.addWidget(button)
        group_layout.addStretch()
        controls.addLayout(group_layout)
        exports = QHBoxLayout()
        self.live_button = QPushButton("Back to Live")
        self.live_button.setEnabled(False)
        self.live_button.setToolTip("Show the latest 60 seconds. Drag the graph to pan or scroll to zoom.")
        self.live_button.clicked.connect(self.graph.back_to_live)
        self.graph.following_changed.connect(lambda following: self.live_button.setEnabled(not following))
        exports.addWidget(self.live_button)
        exports.addStretch()
        self.record_button = QPushButton("Start Recording…")
        self.record_button.clicked.connect(self.toggle_recording)
        self.snapshot_button = QPushButton("Save Snapshot…")
        self.snapshot_button.clicked.connect(self.save_snapshot)
        exports.addWidget(self.record_button)
        exports.addWidget(self.snapshot_button)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addLayout(controls)
        layout.addLayout(exports)
        layout.addWidget(self.graph, 1)
        self.setCentralWidget(content)

        self.timer = QTimer(self, interval=50)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.tick()

    def begin_graph_session(self, now):
        if any(camera.enabled for camera in self.cameras):
            return
        self.origin = now
        self.graph_session += 1
        self.next_plot = 0
        measurements = [camera.measurements for camera in self.cameras]
        for stats in measurements:
            stats.frames.clear()
            stats.rates.clear()
        self.graph.update_plot(now, now, measurements)

    def export_path(self, title, label, suffix):
        directory = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        if not directory or not Path(directory).is_dir():
            directory = str(Path.home())
        dialog = QFileDialog(self, title, directory, label)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setDefaultSuffix(suffix)
        date = datetime.now().strftime("%Y%m%d-%H%M%S")
        dialog.selectFile(f"rtsp-graph-{date}.{suffix}")
        if dialog.exec():
            return Path(dialog.selectedFiles()[0])
        return None

    def toggle_recording(self):
        if self.recording is not None:
            self.stop_recording()
            return
        path = self.export_path("Record graph data", "CSV files (*.csv)", "csv")
        if path is None:
            return
        try:
            self.recording = GraphRecording(path)
        except OSError as error:
            self.statusBar().showMessage(f"Could not start recording: {error}", 8000)
            return
        self.record_button.setText("Stop Recording")
        self.statusBar().showMessage(f"Recording graph data to {path.name}")

    def stop_recording(self, error=None):
        recording, self.recording = self.recording, None
        if recording is None:
            return
        self.record_button.setText("Start Recording…")
        try:
            recording.file.close()
        except OSError as close_error:
            error = error or close_error
        message = f"Graph recording stopped: {error}" if error else "Graph recording saved."
        self.statusBar().showMessage(message, 8000)

    def record_point(self, camera, when, *, frame_size=None, bitrate=None):
        recording = self.recording
        if recording is None or self.origin is None or when < max(self.origin, recording.started):
            return
        try:
            recording.write(self.graph_session, camera, when - self.origin, frame_size, bitrate)
        except OSError as error:
            self.stop_recording(error)

    def save_snapshot(self):
        path = self.export_path("Save graph snapshot", "PNG images (*.png)", "png")
        if path is None:
            return
        try:
            save_graph_snapshot(path, self.graph, [panel.snapshot_details() for panel in self.panels])
        except OSError as error:
            self.statusBar().showMessage(f"Could not save snapshot: {error}", 8000)
        else:
            self.statusBar().showMessage(f"Snapshot saved to {path.name}", 8000)

    @staticmethod
    def available_group_targets(targets):
        return [button for url, button in targets if url.text().strip() and button.isEnabled()]

    def run_group_action(self, targets):
        for button in self.available_group_targets(targets):
            button.click()

    def tick(self):
        now = monotonic()
        for panel in self.panels:
            on_frame = None
            if self.recording is not None:
                on_frame = lambda frame, camera=panel.name: self.record_point(
                    camera, frame.when, frame_size=frame.size / 1024)
            panel.camera.tick(now, on_frame)
            panel.refresh()
        for button, targets in self.group_buttons.items():
            button.setEnabled(bool(self.available_group_targets(targets)))
        if self.closing and all(camera.attempt is None for camera in self.cameras):
            self.close()
            return
        active = any(camera.enabled for camera in self.cameras)
        if now >= self.next_plot or (self.graph_running and not active):
            measurements = [camera.measurements for camera in self.cameras]
            redraw = active or self.graph_running
            for panel, stats in zip(self.panels, measurements, strict=True):
                if redraw:
                    for timestamp, rate in stats.sample(now):
                        self.record_point(panel.name, timestamp, bitrate=rate)
                panel.refresh_summary()
            if redraw:
                self.graph.update_plot(self.origin, now, measurements)
            if self.recording is not None:
                try:
                    self.recording.file.flush()
                except OSError as error:
                    self.stop_recording(error)
            self.next_plot = now + BITRATE_SAMPLE_SECONDS
        self.graph_running = active

    def closeEvent(self, event):
        if not self.closing:
            self.closing = True
            self.centralWidget().setEnabled(False)
            self.stop_recording()
            for panel in self.panels:
                panel.stop()
        if any(camera.attempt is not None for camera in self.cameras):
            event.ignore()
        else:
            self.timer.stop()
            super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", metavar="RTSP_URL", nargs="?", help="start Camera A with this RTSP URL")
    args = parser.parse_args()
    if sys.version_info < (3, 12) and pyside_version == "6.12.0":
        parser.exit(1, "PySide6 6.12.0 can crash on Python 3.10 and 3.11. "
                       "Install the project's requirements in a fresh virtual environment.\n")
    if args.url is not None:
        try:
            camera_url(args.url)
        except ConnectionInputError as error:
            parser.error(str(error))

    # Player diagnostics use fixed descriptions since FFmpeg logs can echo a URL.
    if av is not None:
        av.logging.set_level(av.logging.PANIC)
    app = QApplication([sys.argv[0]])
    app.setDesktopFileName("rtsp-frame-graph")
    window = MainWindow()
    signal.signal(signal.SIGINT, lambda *_: window.close())
    window.show()
    if args.url is not None:
        panel = window.panels[0]
        panel.fields["url"].setText(args.url)
        panel.fields["username"].clear()
        panel.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
