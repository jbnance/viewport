# viewport — Architecture & Flow Diagrams

## Module Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                          main.py                                 │
│  Entry point: args, logging, Gst.init, config, signal handlers   │
│  Creates: ViewportPipeline, Cell[], GLib.MainLoop                │
└──────────┬───────────────┬───────────────┬───────────────────────┘
           │               │               │
           ▼               ▼               ▼
┌──────────────┐  ┌────────────┐  ┌──────────────────────────────┐
│  config.py   │  │ pipeline.py│  │          cell.py             │
│              │  │            │  │                              │
│ load_config()│  │ Viewport-  │  │ Cell (×N)                    │
│ DisplayConfig│  │  Pipeline  │  │  - active branch management  │
│ CellConfig   │  │  compositor│  │  - shadow-branch preloading  │
│ DecoderConfig│  │  capsfilter│  │  - rotation timer            │
│ AppConfig    │  │  kmssink   │  │  - watchdog timer            │
│              │  │  bus watch │  │  - background teardown queue │
└──────────────┘  └────────────┘  └──────────────────────────────┘
```

## Startup Sequence

```
main()
  │
  ├── Bootstrap logging (JournalHandler or StreamHandler)
  ├── Gst.init(None)
  ├── load_config(path)
  │     ├── Parse YAML
  │     ├── Resolve streams: section (name → URL)
  │     ├── Resolve groups: section (name → [URLs])
  │     ├── Resolve cells: (expand names/groups → URL lists)
  │     └── _autoplace_cells() — assign (row, col) to each cell
  │
  ├── _setup_logging(config.log_level)
  ├── Apply gst_debug filter (if configured)
  ├── detect_decoders() — probe registry for hw/sw decoder names
  │
  ├── ViewportPipeline(config)
  │     └── _build()
  │           ├── Create compositor (background=black, ignore-inactive-pads)
  │           ├── Allocate sink pads (one per cell, positioned in grid)
  │           ├── Create capsfilter (WxH @ framerate fps)
  │           ├── Create kmssink (DRM/KMS output, sync=False)
  │           └── Link: compositor → capsfilter → kmssink
  │
  ├── For each cell config:
  │     └── Cell(index, cell_cfg, decoders, pipeline, compositor_pad, ...)
  │
  ├── For each cell:
  │     └── cell.start()
  │           ├── _connect_stream(streams[0])   — build & link first branch
  │           ├── Schedule rotation timer        — if multi-URL cell
  │           └── Schedule watchdog timer        — all cells
  │
  ├── GLib.MainLoop()
  ├── vp.attach_bus_handler(loop)  — add_signal_watch + message handler
  ├── Register SIGINT/SIGTERM → loop.quit()
  ├── vp.play()                    — pipeline → PLAYING
  └── loop.run()                   — blocks until quit
        │
        └── On quit:
              ├── cell.stop() for each cell
              │     ├── Cancel rotation timer
              │     ├── Cancel watchdog timer
              │     ├── _abort_preload() if in progress
              │     ├── _teardown_branch()
              │     └── _teardown_queue.join()  — wait for background NULLs
              └── vp.stop()  — pipeline → NULL
```

## GStreamer Pipeline Topology

```
 Per cell (×N):                                    Shared (×1):

 rtspsrc ──(pad-added)──► rtph264depay              ┌─────────────────────┐
                               │                    │     compositor      │
                          h264parse                 │                     │
                               │                    │  sink_0  sink_1 ... │
                    v4l2slh264dec (hw)              │    │       │        │
                    or avdec_h264 (sw)              └────┼───────┼────────┘
                               │                         │
                         videoconvert                capsfilter
                               │                    (WxH@fps)
                       queue (leaky=2)                   │
                               │                      kmssink
                               ▼                    (DRM/KMS)
                     compositor sink pad ─────────►    │
                                                    Display
```

## Steady-State: GLib Main Loop Event Sources

```
┌─────────────────────── GLib Main Loop ─────────────────────────┐
│                                                                │
│  Timer Sources (per cell):                                     │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ Rotation Timer (interval_ms)    → _on_rotation_timer()   │  │
│  │   Only for multi-URL cells; fires periodically           │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ Watchdog Timer (every 10s)      → _on_reconnect_watchdog │  │
│  │   All cells; detects stale/aged connections              │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ Preload Timeout (one-shot)      → _on_preload_timeout()  │  │
│  │   Only while a shadow branch is being preloaded          │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  Idle Callbacks:                                               │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ _complete_swap()  — scheduled by shadow frame probe      │  │
│  │   via GLib.idle_add from streaming thread                │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  Bus Signal Watch:                                             │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ _on_bus_message()                                        │  │
│  │   ERROR    → log (cells self-recover)                    │  │
│  │   WARNING  → log                                         │  │
│  │   CLOCK_LOST → pipeline PAUSED → PLAYING                 │  │
│  │   EOS      → loop.quit()                                 │  │
│  │   STATE_CHANGED (pipeline only) → debug log              │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  Streaming Threads (run outside main loop):                    │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ _on_frame_probe()         — updates _last_frame_time     │  │
│  │ _on_shadow_frame_probe()  — schedules _complete_swap()   │  │
│  │ _on_pad_added()           — links rtspsrc dynamic pads   │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                │
│  Background Thread (1 total, shared by all cells):             │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ _teardown_worker()  — set_state(NULL) on orphaned elems  │  │
│  │   Elements already removed from pipeline on main loop    │  │
│  │   Cannot contend with compositor or affect frame delivery│  │
│  └──────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
```

## Stream Rotation Flow (Multi-URL Cells)

```
_on_rotation_timer()                          _on_preload_timeout()
       │                                              │
       ▼                                              ▼
  _preloading?  ──yes──► skip (try next tick)    Abort shadow branch
       │no                                            │
       ▼                                         Advance _current_idx
  _start_preload(next_idx)                            │
       │                                         next_next_idx ==
       ├── Build shadow branch                   _rotation_attempt_start?
       │   (rtspsrc→depay→parse→                      │
       │    dec→vconv→queue)                    yes──► "all streams
       │                                         │     unavailable"
       ├── Create shadow fakesink                │     stop cascade
       ├── Link shadow → fakesink                │
       ├── Add all to pipeline                   no──► _start_preload(next)
       ├── sync_state_with_parent()                    (try next stream
       ├── Install one-shot probe                       immediately)
       │   on shadow out_queue.src
       ├── Start preload timeout timer
       └── _preloading = True

              │
   [streaming thread]
   Shadow produces first frame
              │
              ▼
   _on_shadow_frame_probe()
       │
       └── GLib.idle_add(_complete_swap)

              │
   [main loop]
              ▼
   _complete_swap()
       │
       ├── Cancel preload timeout
       ├── Pause shadow elements
       ├── Unlink shadow out_queue ──✕──► fakesink
       ├── Unlink old out_queue    ──✕──► compositor pad
       ├── Link shadow out_queue   ──────► compositor pad
       ├── Resume shadow elements (sync_state_with_parent)
       ├── Promote: _branch = shadow, _current_idx = next
       ├── Install watchdog probe on new branch
       ├── _teardown_elements_async(old_branch + old_aux + fakesink)
       │        │
       │        ├── pipeline.remove() for each  ← main loop (fast)
       │        └── enqueue for set_state(NULL) ← background thread
       └── _preloading = False
```

## Watchdog Flow (All Cells)

```
_on_reconnect_watchdog()  — fires every 10s
       │
       ├── Compute elapsed time since last frame (or since connect)
       │
       ├── Single-URL cell:
       │     │
       │     ├── max_connection_age_hours > 0 and connection old enough?
       │     │     └── yes: _start_preload(0)  — proactive refresh
       │     │
       │     └── elapsed >= 30s (stale)?
       │           └── yes, not preloading: _start_preload(0)
       │               (reconnect same URL via shadow branch)
       │
       └── Multi-URL cell:
             └── elapsed >= 30s (stale)?
                   └── yes, not preloading: _start_preload(next_idx)
                       (force early rotation to next stream)
```

## Element Teardown Flow

```
                  Main Loop                    Background Thread
                  ─────────                    ─────────────────

  _teardown_branch()
       │
       ├── Unlink out_queue.src ──✕──► compositor pad   (instant)
       │
       └── _teardown_elements_async(branch + aux)
                │
                ├── pipeline.remove(el) for each        (fast, no I/O,
                │   - unlinks from peers                 unsets bus)
                │   - unsets element's bus
                │   - element is now standalone
                │
                └── enqueue (elements, cell_idx)
                         │                       ┌──────────────────┐
                         └──────────────────────►│ _teardown_queue  │
                                                 └────────┬─────────┘
                                                          │
                                                          ▼
                                                 _teardown_worker()
                                                   for el in elements:
                                                     el.set_state(NULL)
                                                       │
                                                       ├── Sends RTSP TEARDOWN
                                                       ├── Closes TCP connection
                                                       └── Joins internal threads
                                                           (may block for tcp_timeout
                                                            seconds — does NOT affect
                                                            main loop or compositor)
```

## Shutdown Sequence

```
SIGINT / SIGTERM
       │
       ▼
  loop.quit()
       │
       ▼
  For each cell:
    cell.stop()
       ├── GLib.source_remove(rotation timer)
       ├── GLib.source_remove(watchdog timer)
       ├── _abort_preload()     — if shadow branch exists
       ├── _teardown_branch()   — remove + enqueue active branch
       └── _teardown_queue.join()  — block until background NULLs finish
                                     (ensures clean release before pipeline stop)
  vp.stop()
       └── pipeline.set_state(NULL)  — stops compositor, kmssink
```
