# viewport

Headless RTSP multi-stream display for Raspberry Pi 4+.

Displays any number of RTSP video streams in a configurable grid on a connected
monitor.  Output goes directly to the GPU via DRM/KMS — no X11, Wayland, or
display manager required.

Default layout (3 rows × 2 columns):

```
┌──────────────┬──────────────┐
│   stream 0   │   stream 1   │
├──────────────┼──────────────┤
│   stream 2   │   stream 3   │
├──────────────┼──────────────┤
│   stream 4   │   stream 5   │
└──────────────┴──────────────┘
```

Grid dimensions, cell sizes, and spanning are all configurable in YAML.  Each
cell can show a single stream or rotate through a playlist on a timer.  When
rotating, the next stream is preloaded in the background so the transition
appears seamless — the current stream stays visible until the new one is ready.

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

See `config.example.yaml` for a fully-documented reference.  The sections below
describe each part of the config file.

### Top-level keys

| Key | Default | Description |
|-----|---------|-------------|
| `log_level` | `INFO` | Verbosity: `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |

### `display:`

Controls the output resolution, grid layout, and rotation behaviour.

| Key | Default | Description |
|-----|---------|-------------|
| `width` | `1920` | Output width in pixels (must match your display) |
| `height` | `1080` | Output height in pixels (must match your display) |
| `framerate` | `15` | Compositor output framerate (fps) — must be set; see note below |
| `rows` | `3` | Number of grid rows |
| `cols` | `2` | Number of grid columns |
| `connector_id` | *(auto)* | DRM connector ID for `kmssink`; omit for auto-detect |
| `preload_timeout` | `10` | Seconds to wait for a preloaded stream's first frame before skipping it |

> **Why `framerate` must be set:** Without it, the GStreamer compositor waits for
> all input pads to produce a frame at the *same* timestamp before compositing.
> Six independent RTSP clocks almost never agree, so the output drops to <1 fps
> despite available CPU.  Setting `framerate` switches the compositor to a
> fixed-interval timer that composites whatever frame each pad currently has.

```yaml
display:
  width: 1920
  height: 1080
  framerate: 15
  rows: 3
  cols: 2
  # connector_id: 42      # run `modetest -c` to list available IDs
  # preload_timeout: 10   # increase for slow/remote cameras
```

### `decoder:`

| Key | Default | Description |
|-----|---------|-------------|
| `prefer_hardware` | `true` | Use `v4l2slh264dec` / `v4l2slh265dec` when available; fall back to `avdec_*` |

### `streams:` (optional)

A named registry that maps friendly names to RTSP URLs.  Names can be used
anywhere a raw URL is accepted — in cells and as group members.

```yaml
streams:
  front_door: rtsp://192.168.1.100/stream1
  back_yard:  rtsp://192.168.1.101/stream1
  driveway:   rtsp://192.168.1.102/stream1
```

### `groups:` (optional)

Maps group names to lists of stream names or raw URLs.  A group referenced in a
cell's `streams` list is expanded (flattened) inline to all of its member URLs.

```yaml
groups:
  exterior: [front_door, back_yard, driveway]
```

Groups cannot reference other groups.  Group names must not clash with stream names.

### `cells:`

A list of cell definitions placed left-to-right, top-to-bottom into the grid.
A bare `-` (null entry) skips one slot, leaving it black.  Cells with fewer
entries than `rows × cols` leave the remaining slots black.

Each item in a cell's `streams` list is resolved as:
1. Contains `://` → used as a raw RTSP URL
2. Matches a name in `streams:` → resolved to its URL
3. Matches a name in `groups:` → expanded to all member URLs in order

**Cell options:**

| Key | Default | Description |
|-----|---------|-------------|
| `streams` | *(required)* | List of stream references (URLs, named streams, or groups) |
| `rotation_interval` | `0` | Seconds between stream switches; `0` disables rotation |
| `codec` | `h264` | `h264` or `h265` |
| `col_span` | `1` | Number of grid columns this cell occupies |
| `row_span` | `1` | Number of grid rows this cell occupies |

**Example cells block:**

```yaml
cells:
  # Single named stream — no rotation
  - streams:
      - front_door

  # Rotate through all cameras in the 'exterior' group every 20 s
  - streams:
      - exterior
    rotation_interval: 20

  # H.265 stream
  - streams:
      - rtsp://192.168.1.103/stream1
    codec: h265

  # Camera with embedded credentials
  - streams:
      - rtsp://admin:password@192.168.1.104/stream1

  # Wide cell spanning 2 columns
  - streams:
      - back_yard
    col_span: 2
```

**Merged-cell layout example** (2 rows × 3 cols):

```
┌──────────────────────┬──────┬──────┐
│                      │  B   │  C   │
│          A           ├──────┼──────┤
│      (col_span: 2)   │  D   │  E   │
└──────────────────────┴──────┴──────┘
```

```yaml
display:
  rows: 2
  cols: 3

cells:
  - streams: [front_door]   # A — spans 2 rows, 1 column (left side)
    row_span: 2
  - streams: [back_yard]    # B — top-centre
  - streams: [driveway]     # C — top-right
  - streams: [garage]       # D — bottom-centre
  - streams: [side_gate]    # E — bottom-right
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

### Stream rotation shows a flash or grey frame

viewport preloads the next stream in the background while the current stream
keeps displaying.  The swap only happens once the new stream has produced its
first decoded frame, so the transition should be seamless.  If cameras are slow
to connect, increase `preload_timeout` in the `display:` section.

If a camera is unreachable when a rotation is due, viewport skips it
immediately (without interrupting the displayed stream) and tries each remaining
stream in turn.  Only if every stream in the list is unavailable does the
current stream stay on screen until the next rotation interval.

## Architecture

```
src/main.py      — entry point, argument parsing, GLib main loop
src/config.py    — YAML config loading and dataclasses
src/pipeline.py  — shared GStreamer pipeline (compositor + kmssink)
src/cell.py      — per-cell RTSP branch management, rotation, and preloading
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

**Stream rotation** uses a shadow-branch preloading strategy.  When the rotation
timer fires, a second branch for the next stream is built and linked to a
temporary `fakesink`.  While the current stream continues displaying, the shadow
branch connects, negotiates, and decodes its first keyframe.  Once that frame
arrives, the branches are hot-swapped on the GLib main loop: the shadow branch
is re-linked to the compositor and the old branch is torn down.  If a stream
never produces a frame within `preload_timeout` seconds, the shadow branch is
discarded and the next stream in the list is tried immediately.

**Single-URL cells** use a watchdog timer instead: if no decoded frame arrives
for 30 seconds (including the case where a stream never connected), the branch
is torn down and reconnected automatically.

## License

MIT
