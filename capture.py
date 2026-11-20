"""RTSP input, per-camera connection state, and bitrate measurements."""

from collections import Counter, deque
from dataclasses import dataclass, field
from math import ceil, nan
from queue import Empty, SimpleQueue
from threading import Event, Lock, Thread
from time import monotonic
from urllib.parse import quote, unquote, urlsplit, urlunsplit

try:
    import av
except ImportError:
    av = None

DISPLAY_SECONDS = 60
HISTORY_SECONDS = 600
BITRATE_SAMPLE_SECONDS = 0.5
BITRATE_HALF_LIFE_SECONDS = 2.5
BITRATE_LOOKAHEAD_SECONDS = 2.5
FIRST_FRAME_SECONDS = 15
FRAME_TIMEOUT = 5
BACKOFF = (1, 2, 4, 8, 16, 30)
STABLE_SECONDS = 10


class ConnectionInputError(ValueError):
    def __init__(self, field_name, message):
        super().__init__(message)
        self.field_name = field_name


def camera_url(address, username="", password=""):
    """Validate the fields without echoing credentials in errors."""
    address = address.strip()
    try:
        parts = urlsplit(address)
        port = parts.port
        if (parts.scheme != "rtsp" or not parts.hostname or parts.fragment
                or port == 0 or any(char.isspace() for char in address)):
            raise ValueError
    except ValueError:
        raise ConnectionInputError("url", "Enter an RTSP URL with a valid hostname and port.") from None

    if parts.username is not None:
        if username and username != unquote(parts.username):
            raise ConnectionInputError("username", "Username conflicts with the RTSP URL.")
        if password and password != unquote(parts.password or ""):
            raise ConnectionInputError("password", "Password conflicts with the RTSP URL.")
        if not username and not password:
            return address
        username = username or unquote(parts.username)
        password = password or unquote(parts.password or "")

    if password and not username:
        raise ConnectionInputError("username", "Enter a username with the password.")
    if not username:
        return address

    login = quote(username, safe="")
    if password:
        login += ":" + quote(password, safe="")
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(parts._replace(netloc=f"{login}@{host}"))


def remembered_login(url):
    """Extract the display URL and username from a validated connection URL."""
    parts = urlsplit(url)
    secret_keys = {"password", "passwd", "pwd", "pass", "token", "access_token",
                   "auth_token", "api_key", "apikey", "signature", "sig"}
    # Leave camera-specific query values encoded exactly as the user entered them.
    query = "&".join(parameter for parameter in parts.query.split("&")
                     if unquote(parameter.partition("=")[0]).lower() not in secret_keys)
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(parts._replace(netloc=host, query=query)), unquote(parts.username or "")


@dataclass(frozen=True)
class StreamInfo:
    codec: str


@dataclass(frozen=True)
class PacketRecord:
    when: float
    size: int


@dataclass(frozen=True)
class FrameRecord:
    when: float
    is_intra: bool
    size: int | None


@dataclass(frozen=True)
class Failure:
    reason: str
    permanent: bool = False


@dataclass
class Measurements:
    frames: deque = field(default_factory=deque)
    rates: deque = field(default_factory=deque)
    packets: deque = field(default_factory=deque)
    rate_intervals: deque = field(default_factory=deque)
    pending_rate_centers: deque = field(default_factory=deque)
    received: int = 0
    last_rate_sample: float | None = None
    frame_count: int = 0
    intra_count: int = 0
    unknown_sizes: int = 0
    active_seconds: float = 0
    first_frame: float | None = None
    last_frame: float | None = None
    rate: float | None = None

    def add_packet(self, record):
        if self.last_rate_sample is None:
            self.last_rate_sample = record.when
        self.packets.append(record)
        self.received += record.size

    def add_frame(self, record):
        if self.last_frame is None:
            self.first_frame = record.when
        else:
            self.active_seconds += record.when - self.last_frame
        self.last_frame = record.when
        self.frame_count += 1
        self.intra_count += record.is_intra
        if record.size is None:
            self.unknown_sizes += 1
        else:
            self.frames.append((record.when, record.size / 1024))

    def sample(self, now):
        cutoff = now - HISTORY_SECONDS
        for history in (self.frames, self.rates):
            while history and history[0][0] < cutoff:
                history.popleft()
        if self.last_frame is None or self.last_rate_sample is None:
            return []
        elapsed = now - self.last_rate_sample
        if elapsed < BITRATE_SAMPLE_SECONDS:
            return []

        received = 0
        while self.packets and self.packets[0].when <= now:
            received += self.packets.popleft().size
        sample = received * 8 / (elapsed * 1_000_000)
        self.rate_intervals.append((self.last_rate_sample, now, sample))
        self.pending_rate_centers.append(self.last_rate_sample + elapsed / 2)
        self.last_rate_sample = now

        ready = []
        latest_center = min(now, self.last_frame) - BITRATE_LOOKAHEAD_SECONDS
        while self.pending_rate_centers and self.pending_rate_centers[0] <= latest_center:
            center = self.pending_rate_centers.popleft()
            self.rate = self.centered_rate(center)
            ready.append((center, self.rate))
        self.rates.extend(ready)
        return ready

    def centered_rate(self, center):
        left = center - BITRATE_LOOKAHEAD_SECONDS
        right = center + BITRATE_LOOKAHEAD_SECONDS
        while self.rate_intervals and self.rate_intervals[0][1] <= left:
            self.rate_intervals.popleft()
        weighted_rate = total_weight = 0
        # Subtract the edge weight so an interval fades out at the window boundary.
        edge_weight = 2 ** (-BITRATE_LOOKAHEAD_SECONDS / BITRATE_HALF_LIFE_SECONDS)
        for start, end, rate in self.rate_intervals:
            if start >= right:
                break
            start, end = max(start, left), min(end, right)
            distance = abs((start + end) / 2 - center)
            weight = (2 ** (-distance / BITRATE_HALF_LIFE_SECONDS) - edge_weight) * (end - start)
            total_weight += weight
            weighted_rate += weight * rate
        return weighted_rate / total_weight

    def end_segment(self):
        if self.last_frame is not None:
            while self.rates and self.rates[-1][0] > self.last_frame:
                self.rates.pop()
            if self.rates:
                self.rates.append((self.last_frame, nan))
        self.first_frame = self.last_frame = self.rate = None
        self.packets.clear()
        self.rate_intervals.clear()
        self.pending_rate_centers.clear()
        self.last_rate_sample = None


class LatestFrame:
    """A single-frame mailbox for one connection, read by the renderer."""

    def __init__(self):
        self.lock = Lock()
        self.subscribed = False
        self.frame = None

    def subscribe(self, enabled):
        with self.lock:
            self.subscribed = enabled
            if not enabled:
                self.frame = None

    def offer(self, frame):
        with self.lock:
            if self.subscribed:
                self.frame = frame

    def take(self):
        with self.lock:
            frame, self.frame = self.frame, None
            return frame


def av_failure(error):
    # FFmpeg exception strings can include the original authenticated URL.
    descriptions = {
        "HTTPUnauthorizedError": "Camera rejected the credentials",
        "HTTPForbiddenError": "Camera denied access",
        "HTTPNotFoundError": "Camera stream was not found",
        "ConnectionRefusedError": "Camera refused the connection",
        "TimeoutError": "RTSP input timed out",
        "ExitError": "RTSP input timed out",
        "InvalidDataError": "Invalid video or RTSP data",
        "EOFError": "Stream ended",
        "DecoderNotFoundError": "Video codec is unavailable in this PyAV build",
    }
    name = type(error).__name__
    return Failure(descriptions.get(name, f"Capture failed ({name})"),
                   permanent=name == "DecoderNotFoundError")


class CaptureAttempt:
    def __init__(self, url, preview):
        self.events = SimpleQueue()
        self.preview = LatestFrame()
        self.preview.subscribe(preview)
        self.cancel = Event()
        self.thread = Thread(target=self.run, args=(url,), name="RTSP capture")
        self.thread.start()

    def run(self, url):
        failure = Failure("Stream ended")
        options = {
            "rtsp_transport": "tcp",
            "timeout": str(FRAME_TIMEOUT * 1_000_000),
            # Measurements calculate FPS from decoded frames after connecting.
            "fpsprobesize": "0",
        }
        try:
            with av.open(url, mode="r", options=options,
                         timeout=(FIRST_FRAME_SECONDS, FRAME_TIMEOUT)) as source:
                if not source.streams.video:
                    failure = Failure("RTSP input has no video stream", permanent=True)
                else:
                    self.read(source, source.streams.video[0])
        except Exception as error:
            failure = av_failure(error)
        finally:
            self.preview.subscribe(False)
            self.events.put(failure)

    def read(self, source, stream):
        codec = stream.codec_context
        self.events.put(StreamInfo(codec.codec.canonical_name))
        # FFmpeg delivers H.264/H.265 demuxer packets as access units. The
        # decoder can reorder output, so carry the original packet's size.
        track_sizes = codec.name in {"h264", "hevc"}
        if track_sizes:
            codec.flags |= av.codec.context.Flags.copy_opaque

        for packet in source.demux(stream):
            if self.cancel.is_set():
                return
            if packet.size:
                self.events.put(PacketRecord(monotonic(), packet.size))
                if track_sizes and not packet.is_corrupt:
                    packet.opaque = (packet.size, packet.pts)

            decoded = packet.decode()
            origins = Counter(frame.opaque for frame in decoded) if track_sizes else {}
            for frame in decoded:
                if self.cancel.is_set():
                    return
                if frame.is_corrupt:
                    continue
                origin = frame.opaque if track_sizes else None
                size = None
                if origin is not None and origins[origin] == 1:
                    packet_size, pts = origin
                    if frame.pts == pts:
                        size = packet_size
                self.events.put(FrameRecord(monotonic(), frame.pict_type == 1, size))
                self.preview.offer(frame)


class Camera:
    """Own the state machine on the UI thread and one worker at a time."""

    def __init__(self):
        self.measurements = Measurements()
        self.status = "Idle"
        self.codec = None
        self.enabled = False
        self.attempt = None
        self.url = None
        self.wants_preview = False
        self.action = None
        self.retry_at = None
        self.retry_number = 0
        self.backoff_step = 0
        self.deadline = None
        self.loss_label = None
        self.failure = None

    def start(self, url, now):
        self.url = url
        self.measurements = Measurements()
        self.retry_number = self.backoff_step = 0
        self.retry_at = None
        self.enabled = av is not None
        if not self.enabled:
            self.status = "Error · PyAV could not be loaded. Check the installation."
            return
        self.open_attempt(now)

    def open_attempt(self, now):
        self.status = (f"Reconnecting · attempt {self.retry_number}"
                       if self.retry_number else "Connecting")
        self.codec = None
        self.deadline = now + FIRST_FRAME_SECONDS
        self.attempt = CaptureAttempt(self.url, self.wants_preview)

    def set_preview(self, enabled):
        self.wants_preview = enabled
        if self.attempt:
            self.attempt.preview.subscribe(enabled and self.action is None)

    def stop(self):
        self.enabled = False
        self.cancel_attempt("stop")
        if self.attempt is None:
            self.finish_cleanup(monotonic())

    def restart(self):
        self.cancel_attempt("restart")
        if self.attempt is None:
            self.finish_cleanup(monotonic())

    def cancel_attempt(self, action):
        self.retry_at = None
        self.action = action
        if self.attempt:
            self.attempt.preview.subscribe(False)
            self.attempt.cancel.set()
        self.measurements.end_segment()
        if action == "stop":
            self.status = "Stopping"
        elif action == "restart":
            self.status = "Reconnecting"

    def failed(self, failure):
        self.loss_label = ("Connection lost" if self.measurements.first_frame is not None
                           else "Retry scheduled")
        self.failure = failure
        self.cancel_attempt("failure")
        self.status = f"{self.loss_label} · {failure.reason}"

    def finish_cleanup(self, now):
        action, self.action = self.action, None
        if action == "stop":
            self.status = "Stopped"
            self.url = None
        elif action == "restart":
            self.start(self.url, now)
        elif self.failure.permanent:
            self.enabled = False
            self.status = f"Error · {self.failure.reason}"
        else:
            delay = BACKOFF[self.backoff_step]
            self.backoff_step = min(self.backoff_step + 1, len(BACKOFF) - 1)
            self.retry_at = now + delay

    def tick(self, now, on_frame=None):
        if self.attempt is not None:
            queue = self.attempt.events
            while True:
                try:
                    event = queue.get_nowait()
                except Empty:
                    break
                if self.action is not None:
                    continue
                if isinstance(event, Failure):
                    self.failed(event)
                elif isinstance(event, StreamInfo):
                    self.codec = event.codec
                elif isinstance(event, PacketRecord):
                    self.measurements.add_packet(event)
                elif event.when < self.deadline:
                    self.measurements.add_frame(event)
                    if on_frame is not None and event.size is not None:
                        on_frame(event)
                    self.deadline = event.when + FRAME_TIMEOUT
                    self.status = "Live"
                    if event.when - self.measurements.first_frame >= STABLE_SECONDS:
                        self.backoff_step = self.retry_number = 0
                else:
                    self.watchdog_expired()

            if self.action is None and now >= self.deadline:
                self.watchdog_expired()
            if self.action is not None and not self.attempt.thread.is_alive() and queue.empty():
                self.attempt = None
                self.finish_cleanup(now)

        if self.retry_at is not None:
            remaining = self.retry_at - now
            if remaining <= 0:
                self.retry_at = None
                self.retry_number += 1
                self.open_attempt(now)
            else:
                self.status = f"{self.loss_label} · retry in {ceil(remaining)}s · {self.failure.reason}"

    def watchdog_expired(self):
        reason = ("No video frames for 5 seconds" if self.measurements.first_frame is not None
                  else "No first frame within 15 seconds")
        self.failed(Failure(reason))
