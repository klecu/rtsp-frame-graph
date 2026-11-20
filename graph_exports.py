"""CSV recording and PNG snapshots of the comparison graph."""

import csv
from datetime import datetime
from html import escape
from math import ceil
from time import monotonic

from PySide6.QtCore import QIODevice, QSaveFile
from PySide6.QtGui import QPainter, QPixmap, QTextDocument, QTextOption


class GraphRecording:
    def __init__(self, path):
        self.file = open(path, "w", encoding="utf-8", newline="")
        self.started = monotonic()
        self.writer = csv.writer(self.file)
        try:
            self.writer.writerow(("session", "camera", "elapsed_seconds",
                                  "frame_size_kib", "smoothed_bitrate_mbps"))
            self.file.flush()
        except OSError:
            self.file.close()
            raise

    def write(self, session, camera, elapsed, frame_size, bitrate):
        self.writer.writerow((session, camera, f"{elapsed:.6f}", frame_size, bitrate))


def save_graph_snapshot(path, graph, cameras):
    chart = graph.grab()
    ratio = chart.devicePixelRatio()
    date = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    sections = [f"<h2>RTSP Frame Graph</h2><p>{date}</p>"]
    for name, status, url, summary in cameras:
        lines = "<br>".join(escape(line) for line in summary.splitlines())
        sections.append(f"<p><b>{escape(name)} · {escape(status)}</b><br>"
                        f"{escape(url or 'No URL configured')}<br>{lines}</p>")

    palette = graph.palette()
    header = QTextDocument()
    header.setDefaultFont(graph.font())
    header.setDocumentMargin(14)
    option = header.defaultTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    header.setDefaultTextOption(option)
    header.setHtml(f'<body style="color: {palette.windowText().color().name()}">'
                   + "".join(sections) + "</body>")
    header.setTextWidth(chart.width() / ratio)
    header_height = ceil(header.size().height())
    image = QPixmap(chart.width(), chart.height() + ceil(header_height * ratio))
    image.setDevicePixelRatio(ratio)
    image.fill(palette.window().color())
    painter = QPainter(image)
    try:
        header.drawContents(painter)
        painter.drawPixmap(0, header_height, chart)
    finally:
        painter.end()

    output = QSaveFile(str(path))
    if not output.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError(output.errorString())
    if not image.save(output, "PNG"):
        output.cancelWriting()
        raise OSError("Could not encode the snapshot as PNG.")
    if not output.commit():
        raise OSError(output.errorString())
