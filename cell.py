"""Per-cell RTSP stream branch management and rotation logic.

Each Cell owns:
- A list of RTSP URLs to show (one or more)
- The GStreamer elements for the currently active stream branch
- A GLib timeout that triggers stream rotation when rotation_interval > 0

The branch topology for each active stream is:

  rtspsrc ─(pad-added)─► queue ─► rtph264depay ─► h264parse
                                                      │
                                                 v4l2h264dec  (hw preferred)
                                                 or avdec_h264 (sw fallback)
                                                      │
                                                   queue
                                                      │
                                            compositor sink pad

The compositor scales each input to the cell's width/height via its pad
properties, so no separate videoconvert, videoscale, or capsfilter is needed
in each branch — removing those 3 elements per cell saves 18 software
processing stages across the full 6-cell pipeline.

Stream rotation runs directly on the GLib main loop thread (the GLib timeout
callback), so teardown and reconnect are safe — no pad probes are used.
"""

from __future__ import annotations

import logging
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

    def stop(self) -> None:
        """Tear down the active branch cleanly."""
        if self._rotation_source_id is not None:
            GLib.source_remove(self._rotation_source_id)
            self._rotation_source_id = None
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

    # ------------------------------------------------------------------
    # Branch construction
    # ------------------------------------------------------------------

    def _build_branch(self, url: str, codec: str) -> list[Gst.Element]:
        """Create all GStreamer elements for one RTSP stream branch."""
        suffix = _next_suffix(self.index)

        src = self._make("rtspsrc", f"src_{suffix}")
        src.set_property("location", url)
        src.set_property("latency", 100)          # 100ms jitter buffer (was 200)
        src.set_property("drop-on-latency", True) # drop stale frames instead of stalling
        src.set_property("do-rtcp", False)        # reduce network overhead
        src.set_property("protocols", 0x4)        # prefer TCP (4 = GST_RTSP_LOWER_TRANS_TCP)
        src.set_property("retry", 5)

        in_queue = self._make("queue", f"inq_{suffix}")
        in_queue.set_property("max-size-buffers", 2)  # was 5; smaller = less buffering
        in_queue.set_property("leaky", 2)             # 2 = downstream (drop old buffers)

        if codec == "h265":
            depay = self._make("rtph265depay", f"depay_{suffix}")
            parser = self._make("h265parse", f"parse_{suffix}")
        else:
            depay = self._make("rtph264depay", f"depay_{suffix}")
            parser = self._make("h264parse", f"parse_{suffix}")

        decoder = self._make_decoder(codec)
        decoder.set_name(f"dec_{suffix}")

        # No videoconvert/videoscale/capsfilter here — the compositor scales
        # each input to the cell dimensions via its sink-pad width/height
        # properties, handling color conversion internally.  Adding those
        # elements would cause two software conversions per frame per cell.
        out_queue = self._make("queue", f"outq_{suffix}")
        out_queue.set_property("max-size-buffers", 2)
        out_queue.set_property("leaky", 2)

        branch = [src, in_queue, depay, parser, decoder, out_queue]

        # rtspsrc has dynamic pads — connect via signal
        src.connect("pad-added", self._on_pad_added, in_queue)
        src.connect("no-more-pads", self._on_no_more_pads)

        return branch

    def _link_static_branch(self, branch: list[Gst.Element]) -> None:
        """Link the static part of the branch (depay → … → out_queue → compositor).

        rtspsrc (branch[0]) links to in_queue (branch[1]) dynamically via pad-added.
        """
        # branch: [rtspsrc, in_queue, depay, parser, decoder, out_queue]
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
        hw_name = "v4l2h264dec" if codec == "h264" else "v4l2h265dec"
        sw_name = "avdec_h264" if codec == "h264" else "avdec_h265"

        if prefer_hw:
            el = Gst.ElementFactory.make(hw_name)
            if el is not None:
                log.debug("Cell %d: using hardware decoder %s", self.index, hw_name)
                return el
            log.warning(
                "Cell %d: hardware decoder '%s' not available, falling back to '%s'",
                self.index,
                hw_name,
                sw_name,
            )

        el = Gst.ElementFactory.make(sw_name)
        if el is None:
            raise RuntimeError(
                f"Cell {self.index}: neither hardware nor software decoder "
                f"available for codec '{codec}'"
            )
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
        self, src: Gst.Element, new_pad: Gst.Pad, in_queue: Gst.Element
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

        # Skip audio (and any other non-video) RTP pads.
        if "media=(string)video" not in caps_str:
            log.debug("Cell %d: ignoring non-video RTP pad (%s…)", self.index, caps_str[:60])
            return

        sink_pad = in_queue.get_static_pad("sink")
        if sink_pad is None or sink_pad.is_linked():
            return

        # PyGObject's Gst.Pad.link() override raises LinkError on failure
        # instead of returning the error code, so we use try/except.
        try:
            new_pad.link(sink_pad)
            log.debug("Cell %d: linked rtspsrc video pad to depay queue", self.index)
        except Exception as exc:  # gi.overrides.Gst.LinkError or similar
            log.error("Cell %d: failed to link rtspsrc pad: %s", self.index, exc)

    def _on_no_more_pads(self, src: Gst.Element) -> None:
        log.debug("Cell %d: rtspsrc no-more-pads", self.index)

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
