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

Stream rotation uses a shadow-branch preloading strategy to eliminate visible
gaps during stream changes.  When the rotation timer fires, a "shadow" branch
for the next stream is built and linked to a temporary fakesink.  While the
current stream continues displaying normally, the shadow branch connects,
negotiates, and decodes its first keyframe.  Once a decoded frame arrives, the
branches are hot-swapped on the GLib main loop: the shadow branch is re-linked
to the compositor and the old branch is torn down.  The current stream is
visible right up to the moment the new stream is ready, eliminating the gap.

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

from config import CellConfig, DecoderConfig, ResolvedDecoders

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


def detect_decoders(dec_cfg: DecoderConfig) -> ResolvedDecoders:
    """Probe the GStreamer registry to pick the best available decoder for each codec.

    Must be called after Gst.init().  Uses Gst.ElementFactory.find() to query
    the registry without constructing any elements, so there is no pipeline side
    effect.  Call once at startup; pass the returned ResolvedDecoders to every
    Cell so that each branch build simply does ElementFactory.make(known_name)
    without repeating the hardware-availability check.
    """
    def _pick(hw_name: Optional[str], sw_name: str, label: str) -> str:
        if hw_name is not None and Gst.ElementFactory.find(hw_name) is not None:
            log.info("Decoder (%s): hardware '%s' available", label, hw_name)
            return hw_name
        if hw_name is not None:
            log.warning(
                "Decoder (%s): hardware '%s' not available, falling back to software '%s'",
                label, hw_name, sw_name,
            )
        else:
            log.info("Decoder (%s): using software '%s'", label, sw_name)
        return sw_name

    h264_hw = "v4l2slh264dec" if dec_cfg.prefer_hardware else None
    h265_hw = "v4l2slh265dec" if dec_cfg.prefer_hardware else None
    return ResolvedDecoders(
        h264=_pick(h264_hw, "avdec_h264", "H.264"),
        h265=_pick(h265_hw, "avdec_h265", "H.265"),
    )


class Cell:
    """Manages one grid cell: one active RTSP branch + optional rotation."""

    def __init__(
        self,
        index: int,
        cell_cfg: CellConfig,
        decoders: ResolvedDecoders,
        pipeline: Gst.Pipeline,
        compositor_pad: Gst.Pad,
        preload_timeout: int = 10,
    ) -> None:
        self.index = index
        self.cell_cfg = cell_cfg
        self._decoders = decoders
        self.pipeline = pipeline
        self.compositor_pad = compositor_pad

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

        # Shadow branch state — used during preload-based rotation.
        # _shadow_branch holds the not-yet-active branch being preloaded;
        # _shadow_fakesink is the temporary sink that discards its output;
        # _shadow_aux_elements holds any non-video fakesinks for the shadow rtspsrc.
        # _preloading is True from _start_preload() until the first shadow frame
        # arrives or the timeout fires; it gates the idle_add in the probe so
        # the swap is only scheduled once.
        # _rotation_attempt_start records _current_idx at the beginning of each
        # rotation timer tick.  When a preload fails, _on_preload_timeout()
        # immediately tries the next stream; _rotation_attempt_start lets it
        # detect when it has gone all the way around the list without success
        # and should stop rather than loop forever.  -1 = not in a rotation cycle.
        self._shadow_branch: list[Gst.Element] = []
        self._shadow_fakesink: Optional[Gst.Element] = None
        self._shadow_aux_elements: list[Gst.Element] = []
        self._shadow_next_idx: int = 0
        self._preloading: bool = False
        self._preload_timeout_id: Optional[int] = None
        self._preload_timeout: int = preload_timeout
        self._rotation_attempt_start: int = -1

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Add the initial stream branch to the pipeline and schedule rotation."""
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
        # Abort any in-progress preload before tearing down the active branch.
        if self._preloading or self._shadow_branch:
            self._abort_preload()
        self._teardown_branch()

    # ------------------------------------------------------------------
    # Branch management
    # ------------------------------------------------------------------

    def _connect_stream(self, url: str) -> None:
        """Build a new branch for *url*, add it to the pipeline, and sync state."""
        codec = self.cell_cfg.codec
        log.info("Cell %d: connecting to %s (%s)", self.index,
                 self._stream_display(url, self._current_idx), codec)

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

    def _build_branch(
        self, url: str, codec: str, install_watchdog: bool = True
    ) -> list[Gst.Element]:
        """Create all GStreamer elements for one RTSP stream branch.

        When *install_watchdog* is False, no BUFFER probe is installed on
        out_queue.src.  Pass False for shadow (preload) branches; the probe is
        added by _complete_swap() once the branch is promoted to active.
        """
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
        # this branch.  Skipped for shadow branches (install_watchdog=False);
        # _complete_swap() installs it after promotion.
        if install_watchdog:
            out_src_pad = out_queue.get_static_pad("src")
            if out_src_pad is not None:
                out_src_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_frame_probe)

        return branch

    def _link_static_branch(
        self,
        branch: list[Gst.Element],
        dst_pad: Optional[Gst.Pad] = None,
    ) -> None:
        """Link the static part of the branch (depay → … → out_queue → dst_pad).

        rtspsrc (branch[0]) links to depay (branch[1]) dynamically via pad-added.

        If *dst_pad* is None, the output queue is linked to self.compositor_pad
        (the normal case for active branches).  For shadow branches, pass the
        sink pad of the temporary fakesink.
        """
        if dst_pad is None:
            dst_pad = self.compositor_pad

        # branch: [rtspsrc, depay, parser, decoder, videoconvert, out_queue]
        static_chain = branch[1:]  # everything after rtspsrc
        for i in range(len(static_chain) - 1):
            if not static_chain[i].link(static_chain[i + 1]):
                raise RuntimeError(
                    f"Cell {self.index}: failed to link "
                    f"{static_chain[i].get_name()} → {static_chain[i+1].get_name()}"
                )

        # Link the output queue's src pad to the destination pad.
        out_queue = branch[-1]
        src_pad = out_queue.get_static_pad("src")
        if src_pad is None:
            raise RuntimeError(f"Cell {self.index}: output queue has no src pad")
        ret = src_pad.link(dst_pad)
        if ret != Gst.PadLinkReturn.OK:
            raise RuntimeError(
                f"Cell {self.index}: failed to link output queue → destination pad: {ret}"
            )

    # ------------------------------------------------------------------
    # Decoder factory
    # ------------------------------------------------------------------

    def _make_decoder(self, codec: str) -> Gst.Element:
        """Create a decoder element using the name resolved once at startup.

        Hardware vs. software selection was already decided by detect_decoders()
        before the pipeline started.  This method simply instantiates the
        pre-chosen element by name, avoiding repeated registry probes on every
        stream connection, rotation, or reconnect.

        Note on v4l2h264dec: the stateful bcm2835-codec decoder is intentionally
        excluded.  On Debian Trixie / GStreamer 1.24 it stalls permanently after
        the first frame ("N initial frames were not dequeued: bug in decoder").
        v4l2slh264dec (stateless rpivid) avoids the flush-resume bug and is the
        preferred hardware path; avdec_h264 is the software fallback.
        """
        name = self._decoders.h265 if codec == "h265" else self._decoders.h264
        el = Gst.ElementFactory.make(name)
        if el is None:
            raise RuntimeError(
                f"Cell {self.index}: failed to create decoder '{name}' "
                f"(element was available at startup but is now missing)"
            )
        # Cap thread count for software decoders (avdec_*).
        # avdec_h264's auto mode may claim all 4 cores per instance;
        # 6 streams × 2 threads = 12 threads on 4 cores provides good
        # slice-level parallelism without starving other work.
        if name.startswith("avdec_"):
            el.set_property("max-threads", 2)
        log.debug("Cell %d: created decoder %s", self.index, name)
        return el

    def _stream_display(self, url: str, idx: int) -> str:
        """Return 'label (url)' when the stream has a name, or just 'url' for raw URLs."""
        labels = self.cell_cfg.stream_labels
        if labels and idx < len(labels) and labels[idx] != url:
            return f"{labels[idx]} ({url})"
        return url

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

        Non-video aux elements (fakesinks) are routed to _shadow_aux_elements
        when the rtspsrc belongs to the shadow branch, and to _aux_elements
        otherwise.  Detection uses object identity: src is _shadow_branch[0].
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
            # Route to shadow aux list if this rtspsrc belongs to the shadow branch.
            is_shadow = bool(self._shadow_branch) and src is self._shadow_branch[0]
            aux_list = self._shadow_aux_elements if is_shadow else self._aux_elements
            try:
                new_pad.link(sink)
                aux_list.append(fakesink)
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
        if len(self.cell_cfg.streams) != 1:
            return False  # rotating cell — watchdog not needed; cancel timer

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
            self._stream_display(url, 0),
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
    # Rotation — shadow-branch preloading
    # ------------------------------------------------------------------

    def _on_rotation_timer(self) -> bool:
        """GLib timeout callback: begin preloading the next stream.

        Runs on the GLib main loop thread.  Instead of tearing down the current
        stream immediately, we build a shadow branch for the next stream and
        wait for it to produce its first decoded frame before swapping.
        """
        if len(self.cell_cfg.streams) <= 1:
            return False  # stop timer

        if self._preloading:
            # The previous preload has not yet completed (first frame still
            # pending).  Skip this rotation tick; the next timer fire will try
            # again.  This can happen if rotation_interval is shorter than the
            # time a camera takes to produce its first keyframe.
            log.debug(
                "Cell %d: rotation timer fired while preloading; skipping",
                self.index,
            )
            return True

        next_idx = (self._current_idx + 1) % len(self.cell_cfg.streams)
        next_url = self.cell_cfg.streams[next_idx]
        self._shadow_next_idx = next_idx
        # Mark the start of this rotation cycle so _on_preload_timeout can detect
        # when it has tried every alternative without success (full circle).
        self._rotation_attempt_start = self._current_idx

        log.info(
            "Cell %d: preloading stream %d (%s)",
            self.index, next_idx, self._stream_display(next_url, next_idx),
        )

        try:
            self._start_preload(next_idx)
        except Exception as exc:
            log.error(
                "Cell %d: preload failed to start (%s) — skipping stream %d",
                self.index, exc, next_idx,
            )
            # Skip this stream — advance the index and let the timer try the
            # next one on the following tick (consistent with timeout behaviour).
            self._current_idx = next_idx

        return True  # keep timer running

    def _start_preload(self, next_idx: int) -> None:
        """Build a shadow branch for stream *next_idx* linked to a temporary fakesink.

        The shadow branch runs in the background while the current stream
        continues to display normally.  When the first decoded frame arrives,
        _on_shadow_frame_probe schedules _complete_swap() via GLib.idle_add.

        Raises RuntimeError if any GStreamer element cannot be created or linked.
        The caller is responsible for falling back to a direct swap on error.
        """
        url = self.cell_cfg.streams[next_idx]
        codec = self.cell_cfg.codec

        log.debug(
            "Cell %d: building shadow branch for %s",
            self.index, self._stream_display(url, next_idx),
        )

        # Build branch without the watchdog probe; the probe is installed by
        # _complete_swap() after the branch is promoted to active.
        shadow_branch = self._build_branch(url, codec, install_watchdog=False)

        # Temporary sink that discards the shadow branch's output while it warms up.
        shadow_fakesink = Gst.ElementFactory.make("fakesink", None)
        if shadow_fakesink is None:
            raise RuntimeError(f"Cell {self.index}: failed to create shadow fakesink")
        shadow_fakesink.set_property("sync", False)   # discard immediately
        shadow_fakesink.set_property("async", False)  # don't block pipeline preroll

        # Store shadow state BEFORE adding elements to the pipeline so that
        # _on_pad_added can detect which aux list to use when rtspsrc fires
        # pad-added on a background thread.
        self._shadow_branch = shadow_branch
        self._shadow_fakesink = shadow_fakesink
        self._shadow_aux_elements = []
        self._preloading = True

        # Add all shadow elements to the pipeline.
        for el in shadow_branch:
            self.pipeline.add(el)
        self.pipeline.add(shadow_fakesink)

        # Link shadow branch to the temporary fakesink.
        fakesink_sink_pad = shadow_fakesink.get_static_pad("sink")
        self._link_static_branch(shadow_branch, dst_pad=fakesink_sink_pad)

        # Sync states — starts RTSP connection in the background.
        for el in shadow_branch:
            el.sync_state_with_parent()
        shadow_fakesink.sync_state_with_parent()

        # One-shot probe: fires when the first decoded frame leaves the shadow
        # branch.  Schedules _complete_swap() on the GLib main loop.
        shadow_out_queue = shadow_branch[-1]
        shadow_src_pad = shadow_out_queue.get_static_pad("src")
        if shadow_src_pad is not None:
            shadow_src_pad.add_probe(
                Gst.PadProbeType.BUFFER, self._on_shadow_frame_probe
            )

        # Fallback timer: if the shadow branch never produces a frame (camera
        # unreachable, codec mismatch, etc.), abort the preload and fall back
        # to a direct swap after _PRELOAD_TIMEOUT_SECS seconds.
        self._preload_timeout_id = GLib.timeout_add_seconds(
            self._preload_timeout, self._on_preload_timeout
        )

        log.debug("Cell %d: shadow branch started, waiting for first frame", self.index)

    def _on_shadow_frame_probe(
        self, pad: Gst.Pad, info: Gst.PadProbeInfo
    ) -> Gst.PadProbeReturn:
        """Streaming-thread probe: shadow branch has decoded its first frame.

        Schedules the hot-swap on the GLib main loop via idle_add and removes
        itself (REMOVE = one-shot).  The _preloading flag prevents a race where
        the probe fires again before idle_add runs (shouldn't happen since we
        return REMOVE, but the flag is a cheap safety net).
        """
        if self._preloading:
            self._preloading = False
            GLib.idle_add(self._complete_swap)
        return Gst.PadProbeReturn.REMOVE

    def _complete_swap(self) -> bool:
        """GLib main-loop callback: hot-swap the shadow branch in as the active branch.

        Called via GLib.idle_add() from _on_shadow_frame_probe() once the shadow
        branch has produced its first decoded frame.

        Sequence:
          1. Cancel the fallback timeout.
          2. Pause shadow elements to stop buffer flow during the re-link window.
          3. Unlink shadow out_queue.src from shadow_fakesink.
          4. Unlink old out_queue.src from the compositor pad.
          5. Link shadow out_queue.src to the compositor pad.
          6. Resume shadow elements (sync_state_with_parent → PLAYING).
          7. Promote shadow as the active branch; update _current_idx.
          8. Install the watchdog BUFFER probe on the newly active branch.
          9. Tear down old branch + old aux elements + shadow_fakesink.

        Returns False (GLib.SOURCE_REMOVE) — runs once only.
        """
        # Cancel the fallback timeout — swap is happening cleanly.
        if self._preload_timeout_id is not None:
            GLib.source_remove(self._preload_timeout_id)
            self._preload_timeout_id = None

        shadow_branch = self._shadow_branch
        shadow_fakesink = self._shadow_fakesink
        shadow_aux = self._shadow_aux_elements
        next_idx = self._shadow_next_idx

        if not shadow_branch or shadow_fakesink is None:
            log.warning(
                "Cell %d: _complete_swap called but shadow branch is gone", self.index
            )
            return False

        # Clear shadow state (local vars hold the references we need).
        self._shadow_branch = []
        self._shadow_fakesink = None
        self._shadow_aux_elements = []

        shadow_out_queue = shadow_branch[-1]
        shadow_src_pad = shadow_out_queue.get_static_pad("src")
        fakesink_sink_pad = shadow_fakesink.get_static_pad("sink")

        # --- Step 2: Pause shadow elements to stop buffer flow. ---
        # This prevents GST_FLOW_NOT_LINKED errors that would occur if the
        # streaming thread tries to push a buffer while shadow_src_pad is
        # temporarily unpaired (between unlink and re-link below).
        for el in shadow_branch:
            el.set_state(Gst.State.PAUSED)

        # --- Step 3: Unlink shadow from fakesink. ---
        if shadow_src_pad is not None and shadow_src_pad.is_linked():
            shadow_src_pad.unlink(fakesink_sink_pad)

        # --- Step 4: Unlink old branch from compositor. ---
        old_branch = self._branch
        old_aux = self._aux_elements
        if old_branch:
            old_src_pad = old_branch[-1].get_static_pad("src")
            if old_src_pad is not None and old_src_pad.is_linked():
                old_src_pad.unlink(self.compositor_pad)

        # --- Step 5: Link shadow to compositor. ---
        if shadow_src_pad is not None:
            ret = shadow_src_pad.link(self.compositor_pad)
            if ret != Gst.PadLinkReturn.OK:
                log.error(
                    "Cell %d: shadow → compositor link failed (%s); "
                    "re-linking old branch and aborting swap",
                    self.index, ret,
                )
                # Try to recover: re-link the old branch.
                if old_branch:
                    old_src_pad = old_branch[-1].get_static_pad("src")
                    if old_src_pad is not None:
                        old_src_pad.link(self.compositor_pad)
                    for el in old_branch:
                        el.sync_state_with_parent()
                # Discard shadow branch.
                for el in shadow_branch:
                    el.set_state(Gst.State.NULL)
                    self.pipeline.remove(el)
                for el in shadow_aux:
                    el.set_state(Gst.State.NULL)
                    self.pipeline.remove(el)
                shadow_fakesink.set_state(Gst.State.NULL)
                self.pipeline.remove(shadow_fakesink)
                return False

        # --- Step 6: Resume shadow elements. ---
        for el in shadow_branch:
            el.sync_state_with_parent()

        # --- Step 7: Promote shadow as the active branch. ---
        self._branch = shadow_branch
        self._aux_elements = shadow_aux
        self._current_idx = next_idx
        self._rotation_attempt_start = -1  # successful swap — fresh cycle next time
        self._last_frame_time = 0.0
        self._stream_start_time = time.monotonic()

        # --- Step 8: Install watchdog probe on the newly active branch. ---
        if shadow_src_pad is not None:
            shadow_src_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_frame_probe)

        log.info(
            "Cell %d: hot-swap complete → stream %d (%s)",
            self.index, next_idx,
            self._stream_display(self.cell_cfg.streams[next_idx], next_idx),
        )

        # --- Step 9: Tear down old branch and aux elements. ---
        if old_branch:
            for el in old_branch:
                el.set_state(Gst.State.NULL)
            for el in old_branch:
                self.pipeline.remove(el)
            log.debug(
                "Cell %d: old branch removed (%d elements)", self.index, len(old_branch)
            )
        for el in old_aux:
            el.set_state(Gst.State.NULL)
            self.pipeline.remove(el)
        if old_aux:
            log.debug(
                "Cell %d: removed %d old auxiliary element(s)", self.index, len(old_aux)
            )

        # Tear down the temporary shadow fakesink.
        shadow_fakesink.set_state(Gst.State.NULL)
        self.pipeline.remove(shadow_fakesink)

        return False  # GLib.SOURCE_REMOVE — run once only

    def _abort_preload(self) -> None:
        """Tear down the shadow branch without swapping it in.

        Called by stop() (pipeline shutdown) or _on_preload_timeout() (fallback).
        Cancels the fallback timer, sets all shadow elements to NULL, removes
        them from the pipeline, and clears all _shadow_* fields.
        """
        if self._preload_timeout_id is not None:
            GLib.source_remove(self._preload_timeout_id)
            self._preload_timeout_id = None

        self._preloading = False

        shadow_branch = self._shadow_branch
        shadow_fakesink = self._shadow_fakesink
        shadow_aux = self._shadow_aux_elements

        self._shadow_branch = []
        self._shadow_fakesink = None
        self._shadow_aux_elements = []

        for el in shadow_branch:
            el.set_state(Gst.State.NULL)
        for el in shadow_branch:
            self.pipeline.remove(el)

        for el in shadow_aux:
            el.set_state(Gst.State.NULL)
            self.pipeline.remove(el)

        if shadow_fakesink is not None:
            shadow_fakesink.set_state(Gst.State.NULL)
            self.pipeline.remove(shadow_fakesink)

        n_removed = len(shadow_branch) + len(shadow_aux) + (
            1 if shadow_fakesink is not None else 0
        )
        if n_removed:
            log.debug(
                "Cell %d: shadow branch aborted (%d elements removed)",
                self.index, n_removed,
            )

    def _on_preload_timeout(self) -> bool:
        """GLib timer: shadow branch did not produce a frame within the timeout.

        The target stream is unavailable (camera unreachable, codec mismatch, etc.).
        The shadow branch is discarded and we immediately try to preload the *next*
        stream in the list, so unavailable streams are skipped as fast as possible
        rather than waiting a full rotation_interval per failure.

        A full-circle guard prevents an infinite loop when every alternative is also
        unavailable: once every stream index has been tried within a single rotation
        cycle (detected by next_next_idx wrapping back to _rotation_attempt_start),
        we stop and let the currently-displaying stream continue until the next
        rotation timer tick gives the cycle another chance.

        Returns False (GLib.SOURCE_REMOVE) — the timer is not repeated.
        """
        # Mark the timeout as fired so _abort_preload doesn't try to cancel it.
        self._preload_timeout_id = None

        failed_idx = self._shadow_next_idx
        failed_url = self.cell_cfg.streams[failed_idx]

        log.warning(
            "Cell %d: preload timed out after %ds — stream %d (%s) unavailable, "
            "trying next stream immediately",
            self.index, self._preload_timeout, failed_idx,
            self._stream_display(failed_url, failed_idx),
        )

        self._abort_preload()

        # Advance past the failed stream.
        self._current_idx = failed_idx

        # Full-circle guard: if the next candidate wraps back to where this
        # rotation cycle started, every alternative has been tried and all are
        # down.  Stop here; the current stream keeps displaying and the next
        # rotation timer tick will start a fresh cycle.
        next_idx = (self._current_idx + 1) % len(self.cell_cfg.streams)
        if next_idx == self._rotation_attempt_start:
            log.warning(
                "Cell %d: all %d streams unavailable — current stream stays active",
                self.index, len(self.cell_cfg.streams),
            )
            self._rotation_attempt_start = -1
            return False  # GLib.SOURCE_REMOVE

        # Immediately start preloading the next stream.
        next_url = self.cell_cfg.streams[next_idx]
        self._shadow_next_idx = next_idx
        log.info(
            "Cell %d: preloading stream %d (%s)",
            self.index, next_idx, self._stream_display(next_url, next_idx),
        )
        try:
            self._start_preload(next_idx)
        except Exception as exc:
            log.error(
                "Cell %d: preload failed to start for stream %d (%s): %s",
                self.index, next_idx, self._stream_display(next_url, next_idx), exc,
            )
            # Treat a build failure the same as a timeout — advance and the
            # next timer tick will pick up from here.
            self._current_idx = next_idx
            self._rotation_attempt_start = -1

        return False  # GLib.SOURCE_REMOVE
