"""Per-cell RTSP stream branch management and rotation logic.

Each Cell owns:
- A list of RTSP URLs to show (one or more)
- The GStreamer elements for the currently active stream branch
- A GLib timeout that triggers stream rotation when rotation_interval > 0

The branch topology for each active stream is:

  rtspsrc ─(pad-added)─► rtph264depay ─► h264parse
                                              │
                                         v4l2slh264dec  (hw preferred, stateless)
                                         or avdec_h264  (sw fallback)
                                              │
                                         videoconvert
                                              │
                                           queue
                                              │
                                    compositor sink pad

There is no separate in_queue between rtspsrc and the depayloader.  rtspsrc
contains an internal rtpjitterbuffer that already handles packet ordering,
reordering, and timing — a second queue is redundant and adds latency.

The compositor scales each input to the cell's width/height via its pad
properties, so no separate videoscale or capsfilter is needed per branch.
videoconvert IS kept because v4l2h264dec outputs video/x-raw(memory:V4L2Memory)
buffers; the software compositor requires plain system-memory video/x-raw, so
videoconvert bridges the memory type (cheap: format stays NV12, just memory
mapping) before handing frames to the compositor.

The out_queue between videoconvert and the compositor uses leaky=2 (drop
oldest).  This ensures the compositor always receives the most recently decoded
frame rather than working through a growing backlog that would cause the display
to drift further and further behind real-time.  The RTCP fixes applied earlier
(max-rtcp-rtp-time-diff=-1, do-rtcp enabled by default, config-interval=-1)
address the original cause of 30–60 s stalls, making leaky=2 safe to use again.

Stream rotation runs directly on the GLib main loop thread (the GLib timeout
callback), so teardown and reconnect are safe.

Single-URL cells (no rotation) use a watchdog timer to detect stalled streams
and reconnect automatically.  A buffer pad probe on out_queue.src updates
_last_frame_time each time a decoded frame leaves the branch; a GLib timer
fires every _WATCH_SECS seconds and reconnects if no frame has arrived for
_STALE_SECS seconds (including the case where the stream never produced its
first frame).  Both the watchdog callback and the pad probe write/read a
single float under the Python GIL, so no explicit lock is needed.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402

from config import CellConfig, DecoderConfig

log = logging.getLogger(__name__)

# Module-level monotonic counter so every element created across all cells and
# all rotation cycles gets a unique name.  Using id(url) was unreliable because
# Python reuses object ids, causing "element already exists" warnings.
_branch_seq: int = 0

# Watchdog parameters for single-URL cells.
_STALE_SECS = 30   # seconds without a frame before reconnecting
_WATCH_SECS = 10   # watchdog polling interval (seconds)


def _next_suffix(cell_idx: int) -> str:
    global _branch_seq
    _branch_seq += 1
    return f"cell{cell_idx}_{_branch_seq}"


class Cell:
    """Manages one grid cell: one active RTSP branch + optional rotation."""

    def __init__(
        self,
        index: int,
        cell_cfg: Optional[CellConfig],
        dec_cfg: DecoderConfig,
        pipeline: Gst.Pipeline,
        compositor_pad: Gst.Pad,
        cell_width: int,
        cell_height: int,
    ) -> None:
        self.index = index
        self.cell_cfg = cell_cfg
        self.dec_cfg = dec_cfg
        self.pipeline = pipeline
        self.compositor_pad = compositor_pad
        self.cell_width = cell_width
        self.cell_height = cell_height

        self._current_idx: int = 0
        self._branch: list[Gst.Element] = []
        self._rotation_source_id: Optional[int] = None
        self._reconnect_source_id: Optional[int] = None
        # fakesink elements created for non-video RTP pads (audio, data, …).
        # Tracked separately so _teardown_branch can clean them up.
        self._aux_elements: list[Gst.Element] = []

        # Watchdog timestamps (monotonic).  Written by the pad probe on the
        # GStreamer streaming thread; read by the watchdog on the GLib main
        # loop thread.  A single float assignment is atomic under the GIL.
        self._last_frame_time: float = 0.0    # 0.0 = no frame received yet
        self._stream_start_time: float = 0.0  # set when _connect_stream runs

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Add the initial stream branch to the pipeline and schedule rotation."""
        if self.cell_cfg is None:
            log.debug("Cell %d: blank (no stream configured)", self.index)
            return

        self._connect_stream(self.cell_cfg.streams[0])

        if (
            self.cell_cfg.rotation_interval > 0
            and len(self.cell_cfg.streams) > 1
        ):
            interval_ms = self.cell_cfg.rotation_interval * 1000
            self._rotation_source_id = GLib.timeout_add(
                interval_ms, self._on_rotation_timer
            )
            log.debug(
                "Cell %d: rotation every %ds across %d streams",
                self.index,
                self.cell_cfg.rotation_interval,
                len(self.cell_cfg.streams),
            )
        elif len(self.cell_cfg.streams) == 1:
            # Single-URL cells use a watchdog to detect stalls and reconnect.
            self._reconnect_source_id = GLib.timeout_add_seconds(
                _WATCH_SECS, self._on_reconnect_watchdog
            )
            log.debug(
                "Cell %d: reconnect watchdog active (%ds stale threshold)",
                self.index,
                _STALE_SECS,
            )

    def stop(self) -> None:
        """Tear down the active branch cleanly."""
        if self._rotation_source_id is not None:
            GLib.source_remove(self._rotation_source_id)
            self._rotation_source_id = None
        if self._reconnect_source_id is not None:
            GLib.source_remove(self._reconnect_source_id)
            self._reconnect_source_id = None
        self._teardown_branch()

    # ------------------------------------------------------------------
    # Branch management
    # ------------------------------------------------------------------

    def _connect_stream(self, url: str) -> None:
        """Build a new branch for *url*, add it to the pipeline, and sync state."""
        codec = self.cell_cfg.codec if self.cell_cfg else "h264"
        log.info("Cell %d: connecting to %s (%s)", self.index, url, codec)

        branch = self._build_branch(url, codec)
        for el in branch:
            self.pipeline.add(el)

        # Link all static-linked elements (everything except rtspsrc → depay,
        # which is dynamic and handled by pad-added).
        self._link_static_branch(branch)

        # Sync new elements to the pipeline's current state.
        for el in branch:
            el.sync_state_with_parent()

        self._branch = branch

        # Record when this connection attempt started so the watchdog can
        # measure the time since the last frame (or since we first connected
        # if no frame has arrived yet).
        self._stream_start_time = time.monotonic()
        self._last_frame_time = 0.0

    def _teardown_branch(self) -> None:
        """Unlink, stop, and remove the current branch elements from the pipeline."""
        branch = self._branch
        self._branch = []

        if not branch:
            return

        # Step 1: Unlink the output queue from the compositor pad FIRST.
        # This prevents the compositor receiving further data or state events
        # from elements that are about to go away.
        last_queue = branch[-1]
        src_pad = last_queue.get_static_pad("src")
        if src_pad and src_pad.is_linked():
            src_pad.unlink(self.compositor_pad)

        # Step 2: Set all elements to NULL (stops streaming threads).
        for el in branch:
            el.set_state(Gst.State.NULL)

        # Step 3: Remove from the pipeline.
        for el in branch:
            self.pipeline.remove(el)

        log.debug("Cell %d: branch torn down (%d elements removed)", self.index, len(branch))

        # Step 4: Remove auxiliary elements (fakesinks for non-video RTP pads).
        # Must happen after step 2 so rtspsrc's audio pads are deactivated
        # before we pull the fakesinks they were linked to out of the pipeline.
        aux = self._aux_elements
        self._aux_elements = []
        for el in aux:
            el.set_state(Gst.State.NULL)
            self.pipeline.remove(el)
        if aux:
            log.debug("Cell %d: removed %d auxiliary element(s)", self.index, len(aux))

        # Reset watchdog timestamps so the next _connect_stream call starts
        # fresh; the probe will repopulate _last_frame_time once frames flow.
        self._last_frame_time = 0.0
        self._stream_start_time = 0.0

    # ------------------------------------------------------------------
    # Branch construction
    # ------------------------------------------------------------------

    def _build_branch(self, url: str, codec: str) -> list[Gst.Element]:
        """Create all GStreamer elements for one RTSP stream branch."""
        suffix = _next_suffix(self.index)

        src = self._make("rtspsrc", f"src_{suffix}")
        src.set_property("location", url)
        src.set_property("latency", 200)          # 200ms jitter buffer
        # do-rtcp intentionally left at default (True): many cameras require receiving
        # RTCP receiver reports to confirm the connection is alive; without them
        # the camera may stop sending after 60-120 s.
        src.set_property("protocols", 0x4)        # prefer TCP (4 = GST_RTSP_LOWER_TRANS_TCP)
        src.set_property("retry", 5)
        # Disable RTCP/RTP timestamp divergence check.  Some cameras produce RTCP
        # sender reports whose NTP-derived timestamps drift from the RTP timestamps;
        # the default 1000 ms tolerance can cause rtspsrc to periodically reset the
        # pipeline clock, which stalls the compositor.  -1 disables the check.
        src.set_property("max-rtcp-rtp-time-diff", -1)

        if codec == "h265":
            depay = self._make("rtph265depay", f"depay_{suffix}")
            parser = self._make("h265parse", f"parse_{suffix}")
        else:
            depay = self._make("rtph264depay", f"depay_{suffix}")
            parser = self._make("h264parse", f"parse_{suffix}")
        # Prepend codec parameters (SPS/PPS for H.264, VPS/SPS/PPS for H.265) before
        # every IDR frame so that the decoder can re-sync immediately after any gap
        # or flush without waiting for an out-of-band configuration event.
        parser.set_property("config-interval", -1)

        decoder = self._make_decoder(codec)
        decoder.set_name(f"dec_{suffix}")

        # videoconvert converts v4l2h264dec's V4L2Memory buffers to plain system
        # memory so the software compositor can access the pixel data.  It does
        # NOT change the pixel format (NV12 in → NV12 out when possible), so
        # the CPU cost is essentially just an mmap + copy, not a color conversion.
        videoconvert = self._make("videoconvert", f"vconv_{suffix}")

        out_queue = self._make("queue", f"outq_{suffix}")
        # Drop the oldest decoded frame when the queue is full (leaky=2).
        # The compositor (GstVideoAggregator) uses the most recent buffer it has
        # received for each pad; keeping only the newest 2 decoded frames ensures
        # the compositor always composites current video rather than working through
        # a growing backlog.  The RTCP fixes (max-rtcp-rtp-time-diff=-1, do-rtcp
        # default True) address the original cause of 30–60 s stalls, so leaky=2
        # is safe to restore here.
        out_queue.set_property("max-size-buffers", 2)
        out_queue.set_property("leaky", 2)              # drop oldest when full

        branch = [src, depay, parser, decoder, videoconvert, out_queue]

        # rtspsrc has dynamic pads — connect via signal.  Link directly to the
        # depayloader; rtspsrc's internal jitter buffer handles packet ordering.
        src.connect("pad-added", self._on_pad_added, depay)
        src.connect("no-more-pads", self._on_no_more_pads)

        # Watchdog probe: update _last_frame_time on every buffer that leaves
        # this branch.  Runs on a GStreamer streaming thread; a single float
        # write is atomic under the Python GIL so no lock is needed.
        out_src_pad = out_queue.get_static_pad("src")
        if out_src_pad is not None:
            out_src_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_frame_probe)

        return branch

    def _link_static_branch(self, branch: list[Gst.Element]) -> None:
        """Link the static part of the branch (depay → … → out_queue → compositor).

        rtspsrc (branch[0]) links to depay (branch[1]) dynamically via pad-added.
        """
        # branch: [rtspsrc, depay, parser, decoder, videoconvert, out_queue]
        static_chain = branch[1:]  # everything after rtspsrc
        for i in range(len(static_chain) - 1):
            if not static_chain[i].link(static_chain[i + 1]):
                raise RuntimeError(
                    f"Cell {self.index}: failed to link "
                    f"{static_chain[i].get_name()} → {static_chain[i+1].get_name()}"
                )

        # Link the output queue's src pad to the compositor's pre-allocated sink pad
        out_queue = branch[-1]
        src_pad = out_queue.get_static_pad("src")
        if src_pad is None:
            raise RuntimeError(f"Cell {self.index}: output queue has no src pad")
        ret = src_pad.link(self.compositor_pad)
        if ret != Gst.PadLinkReturn.OK:
            raise RuntimeError(
                f"Cell {self.index}: failed to link output queue → compositor pad: {ret}"
            )

    # ------------------------------------------------------------------
    # Decoder factory
    # ------------------------------------------------------------------

    def _make_decoder(self, codec: str) -> Gst.Element:
        prefer_hw = self.dec_cfg.prefer_hardware

        # v4l2h264dec (GStreamer's stateful V4L2 decoder, bcm2835-codec driver)
        # is intentionally NOT used: on Debian Trixie / GStreamer 1.24 it emits
        # "N initial frames were not dequeued: bug in decoder" and stalls
        # permanently after the first frame.  The driver's CAPTURE queue does not
        # properly resume after the flush that rtspsrc performs when its jitter
        # buffer synchronises on startup.
        #
        # v4l2slh264dec uses the *stateless* V4L2 API (rpivid driver) which
        # avoids the flush-resume bug.  It ships in gstreamer1.0-plugins-bad and
        # is the preferred hardware path on Debian Trixie.  If unavailable,
        # avdec_h264 (FFmpeg software decode) is used as a fully reliable
        # fallback.
        if codec == "h265":
            hw_candidates = ("v4l2slh265dec",) if prefer_hw else ()
            sw_name = "avdec_h265"
        else:
            hw_candidates = ("v4l2slh264dec",) if prefer_hw else ()
            sw_name = "avdec_h264"

        for hw_name in hw_candidates:
            el = Gst.ElementFactory.make(hw_name)
            if el is not None:
                log.debug("Cell %d: using hardware decoder %s", self.index, hw_name)
                return el
            log.warning(
                "Cell %d: hardware decoder '%s' not available, falling back to software",
                self.index,
                hw_name,
            )

        el = Gst.ElementFactory.make(sw_name)
        if el is None:
            raise RuntimeError(
                f"Cell {self.index}: no usable decoder for codec '{codec}'"
            )
        # Cap per-stream thread count.  avdec_h264's default (max-threads=0,
        # auto) may claim all 4 cores per instance; 6 concurrent decoders ×
        # 2 threads = 12 threads on 4 cores — far better than the uncapped
        # default while still allowing slice-level parallelism within each stream.
        el.set_property("max-threads", 2)
        log.debug("Cell %d: using software decoder %s", self.index, sw_name)
        return el

    @staticmethod
    def _make(element_type: str, name: str) -> Gst.Element:
        el = Gst.ElementFactory.make(element_type, name)
        if el is None:
            raise RuntimeError(
                f"Could not create GStreamer element '{element_type}'. "
                f"Is the required plugin installed?"
            )
        return el

    # ------------------------------------------------------------------
    # GStreamer signal callbacks
    # ------------------------------------------------------------------

    def _on_pad_added(
        self, src: Gst.Element, new_pad: Gst.Pad, depay: Gst.Element
    ) -> None:
        """Called when rtspsrc negotiates and creates a new output pad.

        rtspsrc creates one pad per media stream (video, audio, …).  We must
        link only the video RTP pad; linking an audio pad to rtph264depay
        would fail with GST_PAD_LINK_NOFORMAT because the depayloader only
        accepts video RTP caps.
        """
        caps = new_pad.get_current_caps() or new_pad.query_caps(None)
        if caps is None or caps.is_empty():
            return

        # Use caps.to_string() to inspect the media type — avoids calling
        # structure.get_name() which is not available in all PyGObject versions.
        # A video pad looks like:
        #   "application/x-rtp, media=(string)video, encoding-name=(string)H264, ..."
        # An audio pad looks like:
        #   "application/x-rtp, media=(string)audio, ..."
        caps_str = caps.to_string()

        if not caps_str.startswith("application/x-rtp"):
            log.debug("Cell %d: ignoring non-RTP pad", self.index)
            return

        # Non-video RTP pads (audio, data, …) must be linked to a fakesink.
        # Leaving them unlinked causes rtspsrc's streaming loop to receive
        # GST_FLOW_NOT_LINKED when it tries to push data, which rtspsrc
        # treats as a fatal error — stopping the video stream for this cell.
        if "media=(string)video" not in caps_str:
            fakesink = Gst.ElementFactory.make("fakesink", None)
            if fakesink is None:
                log.debug(
                    "Cell %d: could not create fakesink for non-video pad (%s…)",
                    self.index,
                    caps_str[:60],
                )
                return
            fakesink.set_property("sync", False)   # discard immediately, no clock wait
            fakesink.set_property("async", False)  # don't block pipeline preroll
            self.pipeline.add(fakesink)
            fakesink.sync_state_with_parent()
            sink = fakesink.get_static_pad("sink")
            try:
                new_pad.link(sink)
                self._aux_elements.append(fakesink)
                log.debug(
                    "Cell %d: linked non-video RTP pad to fakesink (%s…)",
                    self.index,
                    caps_str[:60],
                )
            except Exception as exc:
                log.warning("Cell %d: could not link non-video pad: %s", self.index, exc)
                fakesink.set_state(Gst.State.NULL)
                self.pipeline.remove(fakesink)
            return

        sink_pad = depay.get_static_pad("sink")
        if sink_pad is None or sink_pad.is_linked():
            return

        # PyGObject's Gst.Pad.link() override raises LinkError on failure
        # instead of returning the error code, so we use try/except.
        try:
            new_pad.link(sink_pad)
            log.debug("Cell %d: linked rtspsrc video pad to depayloader", self.index)
        except Exception as exc:  # gi.overrides.Gst.LinkError or similar
            log.error("Cell %d: failed to link rtspsrc pad: %s", self.index, exc)

    def _on_no_more_pads(self, src: Gst.Element) -> None:
        log.debug("Cell %d: rtspsrc no-more-pads", self.index)

    def _on_frame_probe(
        self, pad: Gst.Pad, info: Gst.PadProbeInfo
    ) -> Gst.PadProbeReturn:
        """Streaming-thread probe: stamp the time of each decoded frame.

        Called on a GStreamer streaming thread.  Writes a single float, which
        is atomic under the Python GIL — no explicit lock needed.
        """
        self._last_frame_time = time.monotonic()
        return Gst.PadProbeReturn.OK

    def _on_reconnect_watchdog(self) -> bool:
        """GLib timer: reconnect the stream if no frames have arrived recently.

        Runs on the GLib main loop thread — safe for pipeline topology changes.
        Returns True to keep the timer running, False to cancel it.
        """
        if self.cell_cfg is None or len(self.cell_cfg.streams) != 1:
            return False  # mis-configured; cancel timer

        now = time.monotonic()
        if self._last_frame_time > 0.0:
            # At least one frame has been received; measure staleness from it.
            elapsed = now - self._last_frame_time
        elif self._stream_start_time > 0.0:
            # No frame yet; measure from when we first connected.
            elapsed = now - self._stream_start_time
        else:
            # _connect_stream hasn't run yet (shouldn't normally happen).
            return True

        if elapsed < _STALE_SECS:
            return True  # stream is healthy; keep timer running

        url = self.cell_cfg.streams[0]
        log.warning(
            "Cell %d: no frames for %.0f s — reconnecting to %s",
            self.index,
            elapsed,
            url,
        )
        self._teardown_branch()
        try:
            self._connect_stream(url)
        except Exception as exc:
            log.error("Cell %d: reconnect failed: %s", self.index, exc)
            # _stream_start_time was reset by _teardown_branch and will be set
            # again by _connect_stream; next watchdog tick will try again after
            # another _STALE_SECS of silence.

        return True  # keep timer running

    # ------------------------------------------------------------------
    # Rotation
    # ------------------------------------------------------------------

    def _on_rotation_timer(self) -> bool:
        """GLib timeout callback: rotate to the next stream.

        Runs on the GLib main loop thread — safe for pipeline topology changes.
        A brief black frame during the switch (~100 ms) is acceptable.
        """
        if self.cell_cfg is None or len(self.cell_cfg.streams) <= 1:
            return False  # stop timer

        next_idx = (self._current_idx + 1) % len(self.cell_cfg.streams)
        next_url = self.cell_cfg.streams[next_idx]
        log.info(
            "Cell %d: rotating → stream %d (%s)", self.index, next_idx, next_url
        )
        self._current_idx = next_idx

        # Tear down current branch, then connect the next one.
        # Both operations run here on the GLib main loop — no pad probes needed.
        self._teardown_branch()
        try:
            self._connect_stream(next_url)
        except Exception as exc:
            log.error("Cell %d: failed to connect new stream: %s", self.index, exc)

        return True  # keep timer running
