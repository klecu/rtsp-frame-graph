# RTSP Frame Graph

Version 1.0

Monitor one RTSP camera, or compare two in a single graph. Each camera uses one PyAV connection for both analysis and its optional live preview.

## Run on Linux

Use Python 3.12 or later and a graphical desktop session. From the source checkout, create a virtual environment, install the dependencies, and launch the app.

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python rtsp_frame_graph.py
```

The PyAV wheels provide the FFmpeg libraries used for capture. You can also launch with a URL to start Camera A immediately.

```sh
.venv/bin/python rtsp_frame_graph.py rtsp://camera.example/stream
```

Enter the RTSP URL and any credentials, then start either camera. Open **Live View** and select **Actual pixels (1:1)** to inspect a portion of the source at native resolution. Drag the image or use the scrollbars to move around it.

Each camera remembers the URL and username from its last successful connection and prefills those fields on launch. If the camera requires a password, enter it again before starting.

On Linux, these settings are stored outside the checkout in `$XDG_CONFIG_HOME/rtsp-frame-graph.ini`, which defaults to `~/.config/rtsp-frame-graph.ini`. Git also ignores that settings file if the configuration directory points inside the checkout.

**MBs received** shows the total compressed video payload in decimal megabytes and the overall bitrate in Mbps over active capture time. Automatic reconnects preserve the totals. A manual **Start** or **Restart** resets that camera's totals.

The controls to the right of Camera B act on both configured camera slots. The graph retains 10 minutes of history and follows the latest 60 seconds by default. Drag horizontally to pan or use the scroll wheel to zoom the time axis. **Back to Live** restores automatic following. You can browse the retained data after stopping both cameras. Starting either camera from that state clears the plot and starts at zero elapsed time.

The bitrate graph uses centered exponential smoothing with up to 2.5 seconds of samples on each side of each point. It trails the live frame-size graph by about 2.5 to 3 seconds. CSV rows use the center time shown on the graph. Use **Start Recording** to save new frame-size and smoothed-bitrate points to a CSV. Each row identifies the camera and graph session. Recording stays enabled across camera stops and starts until you select **Stop Recording** or close the app. **Save Snapshot** exports the current graph to a PNG with both camera summaries and their RTSP URLs. The snapshot excludes login details. The default save location is your Documents folder, when available.

## Codec coverage

Decoding depends on the FFmpeg build linked to PyAV and on how the camera packetizes video over RTP. H.264 and H.265 can also provide encoded frame sizes when the decoder can associate a frame with its original packet. Other codecs, including AV1 when the linked build supports its RTSP payload and decoder, can provide live video, packet-byte bitrate, and decoded frame counts. The graph indicates when frame sizes are unavailable.

## Windows bundle

Download `RTSP-Frame-Graph-Windows-x64.zip` from the [v1.0 release](https://github.com/klecu/rtsp-frame-graph/releases/tag/v1.0), extract the entire ZIP, and launch `RTSP Frame Graph.exe`. The bundle includes Python, Qt, and FFmpeg.

To build the ZIP on Windows, use 64-bit Python 3.12 or later and run the following from the source checkout in PowerShell.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
.\.venv\Scripts\python.exe build_windows.py
```

The script writes the ZIP in `dist/`. See [the spec](rtsp_frame_graph_spec.md) for the behavior and acceptance checks.
