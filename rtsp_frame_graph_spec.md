# RTSP camera comparison tool

## Goal

Extend `rtsp_frame_graph.py` with a desktop GUI for comparing two RTSP video streams on one live graph. The same Python application runs on Linux and Windows using PySide6 and PyQtGraph. Preserve the existing single-stream command-line invocation, `python rtsp_frame_graph.py RTSP_URL`, and support it on both platforms. Launch the GUI with `python rtsp_frame_graph.py`.

## Window and controls

- Show two camera sections labeled **Camera A** and **Camera B**. Each contains an RTSP URL, an optional username, a masked password, and an independent connection status. Anonymous streams are supported. Accept credentials in the separate fields or in the URL. Reject conflicting credentials and percent-encode separate credentials when constructing the RTSP URL. Redact credentials from displayed errors and logs. Keep credentials in memory for the session and its retries.
- **Start comparison** validates both URLs and starts the two captures independently. Highlight an invalid field next to its camera. While the comparison runs, lock the connection fields and offer **Stop**. On stop, cancel both cameras’ pending retries and end their capture processes. Allow editing and starting again after cleanup. Closing the window uses the same shutdown path.
- Match each camera’s line color to its legend and summary. Use text for all statuses so color is optional for interpreting them. Each summary shows current rolling bitrate, average capture FPS, total frames, and I-frame count.

## Live graph

- Use one graph with a shared elapsed-time axis starting when **Start comparison** is pressed. Display the latest 60 seconds and refresh every 500 ms. Draw Camera A in blue and Camera B in orange. Show encoded frame sizes as thin impulses on the left axis in KiB and 8-second rolling bitrate as solid lines on the right axis in Mb/s. Keep each axis scale shared between the two cameras.
- Use a shared monotonic clock for timestamps and timestamp records in the capture reader, before queuing them for the GUI. Start each camera’s bitrate line after 8 seconds of uninterrupted capture. Calculate bitrate from the sum of frame bytes in that window multiplied by eight and divided by 8,000,000.
- When a stream is lost, show a gap in its bitrate line beginning after its last received frame. Keep already plotted data until it scrolls out of view. Reset that camera’s rolling window on reconnect and show **Warming up** for its current bitrate until a fresh 8-second window is available. Show an em dash for current bitrate while a camera is disconnected. Keep frame and I-frame totals across retries. Compute average capture FPS over time spent receiving frames, excluding startup and reconnect gaps. Starting a new comparison resets both histories and all counters.

## Per-camera connection status and recovery

| Status | When it appears |
| --- | --- |
| **Idle** | The comparison has never been started. |
| **Connecting** | The initial capture process is starting or waiting for its first valid video frame. |
| **Live** | The current process has delivered a valid frame and the frame watchdog has yet to expire. |
| **Connection lost · retry in Ns** | A previously live process exited or stopped delivering valid frames. Show the reason and time until the next attempt. |
| **Retry scheduled · in Ns** | An initial or subsequent attempt failed before receiving a valid frame. Show the reason and time until the next attempt. |
| **Reconnecting · attempt N** | A retry is starting or waiting for its first valid frame. |
| **Error** | A permanent local setup error requires action, such as a missing `ffprobe` executable. Show the cause. |
| **Stopped** | The user ended the comparison and both captures have been cleaned up. |

1. Start a separate `ffprobe` process for each camera. Only a valid frame record marks the camera **Live** and resets its frame watchdog. Apply a 15-second deadline for the first valid frame and a 5-second deadline since the latest valid frame once live. Use process exit as an immediate failure signal and report a sanitized diagnostic when available.
2. On failure, close out the current graph segment, stop the old process, and schedule a retry only after cleanup. Retry with delays of 1, 2, 4, 8, 16, and then 30 seconds for each subsequent attempt. Continue while the comparison is active. Reset the backoff after 10 consecutive seconds of valid capture, so a stream that repeatedly connects and drops still backs off. Keep the retry count and timers independent for each camera.
3. Use a fresh process on each retry. Ignore late events from an old process or comparison session. Keep at most one capture process per camera, and cancel all retries as soon as the user presses **Stop** or closes the window. A failure, timeout, or retry for one camera leaves the other camera’s capture, status, and graph updates running.

## Capture and platform behavior

- Reuse the existing RTSP-over-TCP frame extraction and the `pkt_size` and `pict_type` fields. Separate capture, rolling statistics, and plotting so the CLI and GUI can reuse the same capture logic. Drain each process’s pipes outside the GUI thread and queue frame, error, and exit events. Use a timer on the GUI thread to update widgets and check deadlines even when neither stream produces output.
- Use a subprocess pipe reader that works on both operating systems. Keep any Linux `stdbuf` handling in the capture launcher and treat `ffprobe` output latency as a Windows release check. The graph, watchdog, and arrival-based bitrate calculations require records to arrive promptly. If the Windows build batches records enough to break those checks, use a capture method that delivers frame records promptly before claiming Windows support.
- Document how to install the Python dependencies and make `ffprobe` available on each platform. The GUI uses PyQtGraph for rendering. The existing CLI may continue to use gnuplot.

## Acceptance checks

- On native Windows and Linux, start two authenticated test streams with different frame sizes and rates. Both show **Live**, each graph series uses the correct camera and axis, and both share the same time origin. Verify frame records arrive promptly and both lines begin after their respective warm-up windows.
- Disconnect one stream while both are live. Verify its status changes within the watchdog deadline or on process exit, its bitrate line develops a gap, and the other camera continues updating. Restore the stream and verify it returns to **Live** and resumes bitrate plotting after a fresh 8-second window.
- Start with one working stream and one unreachable stream. Verify first-frame timeout, retry countdown, capped backoff, and independent operation of the working stream. Exercise repeated short reconnects and verify the backoff resets only after stable capture.
- Stop during a retry countdown and again during an active connection attempt. Verify both child processes exit, no retry starts after stop, and a new comparison begins with fresh counters. Repeat by closing the window. Confirm diagnostics omit camera credentials and the original single-stream CLI still runs on both platforms.
