# viewport

Headless RTSP multi-stream display for Raspberry Pi 4+.

Displays up to **6 RTSP video streams** in a **3 rows × 2 columns** grid on a
1080p display.  Output goes directly to the GPU via DRM/KMS — no X11, Wayland,
or display manager required.  Each grid cell can show a single stream or rotate
through a list of streams on a configurable timer.

```
┌──────────────┬──────────────┐
│   stream 0   │   stream 1   │
├──────────────┼──────────────┤
│   stream 2   │   stream 3   │
├──────────────┼──────────────┤
│   stream 4   │   stream 5   │
└──────────────┴──────────────┘
```

## Requirements

- Raspberry Pi 4 (or newer) running Raspberry Pi OS (Bookworm/Bullseye) or Ubuntu
- Python 3.9+
- GStreamer 1.18+ with the plugins listed below

## Installation

### 1. Install system packages

```bash
sudo apt-get update
sudo apt-get install -y \
    python3-gi gir1.2-gstreamer-1.0 python3-gst-1.0 \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    gstreamer1.0-tools \
    python3-yaml
```

### 2. Add your user to the `video` group

Direct KMS/DRM access requires membership in the `video` group:

```bash
sudo usermod -aG video $USER
# Log out and back in, or run: newgrp video
```

### 3. Copy the application files

```bash
sudo mkdir -p /opt/viewport
sudo cp main.py config.py pipeline.py cell.py /opt/viewport/
sudo chmod +x /opt/viewport/main.py
```

### 4. Create your configuration

```bash
sudo mkdir -p /etc/viewport
sudo cp config.example.yaml /etc/viewport/config.yaml
sudo nano /etc/viewport/config.yaml   # edit with your RTSP URLs
```

## Configuration

See `config.example.yaml` for a fully-documented example.  The key section is
`cells`, which defines one entry per grid position:

```yaml
cells:
  # Single stream — no rotation
  - streams:
      - rtsp://192.168.1.100/stream1

  # Rotation — switches every 30 seconds
  - streams:
      - rtsp://192.168.1.101/cam1
      - rtsp://192.168.1.102/cam2
    rotation_interval: 30

  # H.265 / HEVC stream
  - streams:
      - rtsp://192.168.1.103/stream1
    codec: h265

  # Camera with credentials embedded in URL
  - streams:
      - rtsp://admin:password@192.168.1.104/stream1

  - streams: [rtsp://192.168.1.105/stream1]
  - streams: [rtsp://192.168.1.106/stream1]
```

**Cell options:**

| Key | Default | Description |
|-----|---------|-------------|
| `streams` | *(required)* | List of RTSP URLs for this cell |
| `rotation_interval` | `0` | Seconds between stream switches; `0` disables rotation |
| `codec` | `h264` | `h264` or `h265` |

**Decoder options:**

```yaml
decoder:
  prefer_hardware: true   # use v4l2h264dec/v4l2h265dec when available
```

## Running

### Direct (foreground)

```bash
python3 /opt/viewport/main.py --config /etc/viewport/config.yaml
```

### With debug logging

```bash
python3 /opt/viewport/main.py --config /etc/viewport/config.yaml --log-level DEBUG
```

### Specifying a DRM connector

If you have multiple monitors or the auto-detected connector is wrong:

```bash
# List available connectors
modetest -c

# Use a specific connector
python3 /opt/viewport/main.py --config /etc/viewport/config.yaml --connector-id 42
```

### As a systemd service (autostart at boot)

```bash
sudo cp viewport.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable viewport
sudo systemctl start viewport

# Check status / logs
sudo systemctl status viewport
sudo journalctl -u viewport -f
```

## Troubleshooting

### Verify KMS/DRM is available

```bash
ls /dev/dri/
# Expected: card0  renderD128
```

### Test a single RTSP stream with GStreamer

```bash
# Software decoder (works without hardware)
gst-launch-1.0 rtspsrc location=rtsp://YOUR_URL ! \
    rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! kmssink

# Hardware decoder
gst-launch-1.0 rtspsrc location=rtsp://YOUR_URL ! \
    rtph264depay ! h264parse ! v4l2h264dec ! videoconvert ! kmssink
```

### Check available GStreamer plugins

```bash
gst-inspect-1.0 kmssink       # DRM/KMS output
gst-inspect-1.0 v4l2h264dec   # hardware H.264 decoder
gst-inspect-1.0 v4l2h265dec   # hardware H.265 decoder
gst-inspect-1.0 compositor    # multi-stream compositor
```

### Hardware decoder not found

If `v4l2h264dec` is unavailable, set `prefer_hardware: false` in your config.
The application will fall back to software decoding (`avdec_h264`).

On Raspberry Pi OS, hardware decoders are provided by the `rpicam` or V4L2
kernel modules.  Ensure your firmware is up to date:

```bash
sudo rpi-update
```

### Black cells / stream not connecting

- Verify the RTSP URL is reachable: `ffprobe rtsp://YOUR_URL`
- Ensure the Pi can reach the camera network
- Check logs with `--log-level DEBUG` or `GST_DEBUG=rtspsrc:5`

## Architecture

```
main.py         — entry point, argument parsing, GLib main loop
config.py       — YAML config loading and dataclasses
pipeline.py     — shared GStreamer pipeline (compositor + kmssink)
cell.py         — per-cell RTSP branch management and stream rotation
```

Each cell owns an independent GStreamer element branch:

```
rtspsrc ─(pad-added)─► queue ─► rtph264depay ─► h264parse
                                                     │
                                              v4l2h264dec (hw)
                                              or avdec_h264 (sw)
                                                     │
                                              videoconvert
                                                     │
                                               videoscale
                                                     │
                                        capsfilter (cell WxH)
                                                     │
                                                  queue
                                                     │
                                         compositor sink pad
                                                     │
                                               kmssink (DRM/KMS)
```

Stream rotation uses GStreamer pad probes to block the compositor input pad
while the old branch is torn down and a new branch is connected, minimising
visual glitches during the transition.

## License

MIT
