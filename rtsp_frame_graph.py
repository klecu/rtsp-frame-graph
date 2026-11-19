#!/usr/bin/env python3
"""Plot live encoded frame sizes and rolling bitrate from an RTSP camera on Linux."""

import argparse
from collections import deque
import os
import selectors
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit


DISPLAY_SECONDS = 60.0
WINDOW = 8.0
REFRESH = 0.5
PLOT_SETUP = """
set xlabel "Elapsed time (s)"
set ylabel "Encoded frame size (KiB)"
set y2label "Bitrate (Mb/s)"
set ytics nomirror
set y2tics
set grid
set key top right
"""
FRAME_PLOT = "'-' using 1:($2/1024) with impulses title 'Frame size'"
RATE_PLOT = "'-' using 1:2 axes x1y2 with lines lw 2 title '8-second rolling bitrate'"


def send(plotter, commands):
    plotter.stdin.write(commands + "\n")
    plotter.stdin.flush()


class Capture:
    def __init__(self):
        self.frames = deque()
        self.rates = deque()
        self.recent = deque()
        self.recent_bytes = 0
        self.total_bytes = 0
        self.frame_count = 0
        self.i_frames = 0
        self.start = None

    def add_frame(self, now, size, picture_type):
        if self.start is None:
            self.start = now
        self.frames.append((now, size))
        self.recent.append((now, size))
        self.recent_bytes += size
        self.total_bytes += size
        self.frame_count += 1
        self.i_frames += picture_type == b"I"

    def render(self, plotter, now):
        elapsed = now - self.start
        left = max(0.0, elapsed - DISPLAY_SECONDS)
        cutoff = now - DISPLAY_SECONDS
        while self.frames and self.frames[0][0] < cutoff:
            self.frames.popleft()
        while self.rates and self.rates[0][0] < cutoff:
            self.rates.popleft()
        while self.recent and self.recent[0][0] <= now - WINDOW:
            self.recent_bytes -= self.recent.popleft()[1]

        rolling_label = "collecting data"
        if elapsed >= WINDOW:
            rolling = self.recent_bytes * 8 / WINDOW / 1_000_000
            self.rates.append((now, rolling))
            rolling_label = f"{rolling:.3f} Mb/s"

        average_label = "collecting data"
        if elapsed > 0:
            average = self.total_bytes * 8 / elapsed / 1_000_000
            fps = self.frame_count / elapsed
            average_label = f"{average:.3f} Mb/s | Average FPS {fps:.2f}"
        title = (
            f"8-second rolling bitrate  {rolling_label}\\n"
            f"Capture average  {average_label} | "
            f"Frames {self.frame_count:,} | I-frames {self.i_frames:,}"
        )

        # An invisible zero-height impulse keeps the primary axis valid after a stall.
        frame_rows = [f"{ts - self.start:.9f} {size}" for ts, size in self.frames]
        if not frame_rows:
            frame_rows = [f"{elapsed:.9f} 0"]
        plot = FRAME_PLOT
        rows = frame_rows + ["e"]
        if self.rates:
            plot += ", " + RATE_PLOT
            rows.extend(f"{ts - self.start:.9f} {rate:.9f}" for ts, rate in self.rates)
            rows.append("e")

        frame_top = "*" if any(size for _, size in self.frames) else "1"
        rate_top = "*" if any(rate for _, rate in self.rates) else "1"
        send(plotter, "\n".join([
            f'set title "{title}"',
            f"set xrange [{left:.9f}:{left + DISPLAY_SECONDS:.9f}]",
            f"set yrange [0:{frame_top}]",
            f"set y2range [0:{rate_top}]",
            f"plot {plot}",
            *rows,
        ]))


def plot_stream(probe, plotter):
    capture = Capture()
    pending = b""
    next_refresh = time.monotonic()
    send(plotter, PLOT_SETUP)

    with selectors.DefaultSelector() as selector:
        selector.register(probe.stdout, selectors.EVENT_READ)
        while plotter.poll() is None:
            ready = selector.select(max(0, next_refresh - time.monotonic()))
            if ready:
                chunk = os.read(probe.stdout.fileno(), 65536)
                if not chunk:
                    return probe.wait()

                *lines, pending = (pending + chunk).split(b"\n")
                for line in lines:
                    fields = dict(field.split(b"=", 1) for field in line.strip().split(b"|")
                                  if b"=" in field)
                    size = fields.get(b"pkt_size", b"")
                    if size.isdigit():
                        capture.add_frame(time.monotonic(), int(size), fields.get(b"pict_type"))

            now = time.monotonic()
            if now >= next_refresh:
                if capture.start is not None:
                    capture.render(plotter, now)
                next_refresh = now + REFRESH

    return plotter.returncode


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", metavar="RTSP_URL", help="rtsp://user:password@camera/stream")
    args = parser.parse_args()
    try:
        url = urlsplit(args.url)
        valid_url = url.scheme == "rtsp" and url.hostname
    except ValueError:
        valid_url = False
    if not valid_url:
        parser.error("expected an RTSP URL with a camera hostname")

    for program in ("stdbuf", "ffprobe", "gnuplot"):
        if shutil.which(program) is None:
            parser.error(f"{program} is required and must be on PATH")

    probe_command = [
        "stdbuf", "-oL", "ffprobe", "-v", "error", "-rtsp_transport", "tcp",
        "-select_streams", "v:0", "-show_frames", "-show_entries", "frame=pkt_size,pict_type",
        "-of", "compact=p=0", args.url,
    ]
    try:
        with subprocess.Popen(["gnuplot"], stdin=subprocess.PIPE,
                              text=True) as plotter:
            try:
                with subprocess.Popen(probe_command, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL) as probe:
                    try:
                        status = plot_stream(probe, plotter)
                        if probe.returncode:
                            print(f"ffprobe exited with status {probe.returncode}. "
                                  "Check the camera URL, credentials, and connectivity.",
                                  file=sys.stderr)
                        return 1 if status else 0
                    finally:
                        stop(probe)
            finally:
                stop(plotter)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        print("gnuplot stopped accepting commands.", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Could not run the graph: {error.strerror or error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
