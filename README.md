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

### 3. Run the install script

```bash
sudo bash install.sh
```

The script installs system packages, adds your user to the `video` group,
copies the application files to `/opt/viewport`, places an example config at
`/etc/viewport/config.yaml`, installs the systemd unit, and enables the service.

Run `bash install.sh --help` for available options (custom user, paths, etc.).

### 4. Edit your configuration

```bash
sudo nano /etc/viewport/config.yaml   # add your RTSP stream URLs
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
  prefer_hardware: true   # use v4l2slh264dec/v4l2slh265dec when available
```

## Running

### Direct (foreground)

```bash
python3 /opt/viewport/main.py /etc/viewport/config.yaml
```

### With debug logging

Set `log_level: DEBUG` in your config file, then run as above.

### Specifying a DRM connector

If you have multiple monitors or the auto-detected connector is wrong:

```bash
# List available connectors
modetest -c
```

Then set `connector_id` in your config file under the `display:` section:

```yaml
display:
  connector_id: 42   # replace with the ID shown by modetest -c
```

### As a systemd service (autostart at boot)

```bash
sudo cp deploy/viewport.service /etc/systemd/system/
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
# Software decoder (works without hardware acceleration)
gst-launch-1.0 rtspsrc location=rtsp://YOUR_URL ! \
    rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! kmssink

# Hardware decoder (stateless rpivid, Raspberry Pi 4+)
gst-launch-1.0 rtspsrc location=rtsp://YOUR_URL ! \
    rtph264depay ! h264parse ! v4l2slh264dec ! videoconvert ! kmssink
```

### Check available GStreamer plugins

```bash
gst-inspect-1.0 kmssink         # DRM/KMS output
gst-inspect-1.0 v4l2slh264dec   # hardware H.264 decoder (stateless rpivid)
gst-inspect-1.0 v4l2slh265dec   # hardware H.265 decoder (stateless rpivid)
gst-inspect-1.0 compositor      # multi-stream compositor
```

### Hardware decoder not found

viewport uses the stateless V4L2 decoder (`v4l2slh264dec` / `v4l2slh265dec`),
which requires the **rpivid** kernel driver.  If it is unavailable, the
application automatically falls back to software decoding (`avdec_h264`); you
can also opt out explicitly with `prefer_hardware: false` in your config.

Ensure your firmware is up to date:

```bash
sudo rpi-update
```

### Black cells / stream not connecting

- Verify the RTSP URL is reachable: `ffprobe rtsp://YOUR_URL`
- Ensure the Pi can reach the camera network
- Set `log_level: DEBUG` in your config, or run with `GST_DEBUG=rtspsrc:5`

## Architecture

```
src/main.py      — entry point, argument parsing, GLib main loop
src/config.py    — YAML config loading and dataclasses
src/pipeline.py  — shared GStreamer pipeline (compositor + kmssink)
src/cell.py      — per-cell RTSP branch management and stream rotation
```

Each cell owns an independent GStreamer element branch:

```
rtspsrc ─(pad-added)─► rtph264depay ─► h264parse
                                             │
                                    v4l2slh264dec (hw, stateless)
                                    or avdec_h264 (sw fallback)
                                             │
                                       videoconvert
                                             │
                                     queue (leaky, drop-oldest)
                                             │
                                   compositor sink pad
                                             │
                                      capsfilter (framerate)
                                             │
                                       kmssink (DRM/KMS)
```

The compositor scales each input to its cell dimensions via pad properties,
so no separate `videoscale` or per-cell `capsfilter` is needed.

Stream rotation and reconnection (for stalled single-URL cells) both run on
the GLib main loop thread, calling a teardown + reconnect sequence without
any GStreamer pad probes.

## License

MIT
