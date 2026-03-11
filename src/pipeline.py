"""GStreamer pipeline management for viewport.

Owns the single top-level Gst.Pipeline, the compositor element, and the
kmssink output.  Hands out pre-allocated compositor sink pads to Cell objects.
"""

from __future__ import annotations

import logging
from typing import Optional

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402 (must follow gi.require_version)

from config import AppConfig

log = logging.getLogger(__name__)


def _make(element_type: str, name: str) -> Gst.Element:
    el = Gst.ElementFactory.make(element_type, name)
    if el is None:
        raise RuntimeError(
            f"Could not create GStreamer element '{element_type}'. "
            f"Is the required plugin package installed?"
        )
    return el


class ViewportPipeline:
    """Manages the shared GStreamer pipeline (compositor + kmssink)."""

    def __init__(self, config: AppConfig, connector_id: Optional[int] = None) -> None:
        self.config = config
        self.pipeline: Gst.Pipeline = Gst.Pipeline.new("viewport")
        self._compositor_pads: list[Gst.Pad] = []
        self._loop: Optional[GLib.MainLoop] = None

        self._build(connector_id)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_compositor_pad(self, cell_index: int) -> Gst.Pad:
        """Return the pre-allocated compositor sink pad for *cell_index*."""
        return self._compositor_pads[cell_index]

    def play(self) -> None:
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Pipeline failed to enter PLAYING state")
        log.info("Pipeline PLAYING")

    def stop(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)
        log.info("Pipeline stopped")

    # ------------------------------------------------------------------
    # Internal build
    # ------------------------------------------------------------------

    def _build(self, connector_id: Optional[int]) -> None:
        cfg = self.config
        cw = cfg.display.cell_width
        ch = cfg.display.cell_height

        # --- compositor ---
        compositor = _make("compositor", "compositor")
        compositor.set_property("background", 1)  # 1 = black background

        # Without this, the compositor waits for a buffer on *every* sink pad
        # before producing its first output frame.  If even one camera is slow
        # to deliver its first keyframe, all six cells are frozen.
        # Property added in GStreamer 1.20; silently skipped on 1.18.
        if hasattr(compositor.props, "ignore_inactive_pads"):
            compositor.set_property("ignore-inactive-pads", True)
            log.debug("compositor: ignore-inactive-pads enabled")
        else:
            log.debug(
                "compositor: ignore-inactive-pads not available (GStreamer < 1.20); "
                "all pads must deliver a frame before output begins"
            )

        # Allocate one compositor sink pad per cell.  Each cell's position and
        # size are derived from its auto-placed (row, col) and span (row_span,
        # col_span); unoccupied grid positions show the black background.
        # request_pad_simple() was added in GStreamer 1.20; fall back to
        # get_request_pad() for GStreamer 1.18 (Raspberry Pi OS Bullseye).
        for idx, cell_cfg in enumerate(cfg.cells):
            pad_w = cell_cfg.col_span * cw
            pad_h = cell_cfg.row_span * ch
            xpos = cell_cfg.col * cw
            ypos = cell_cfg.row * ch

            pad_name = f"sink_{idx}"
            if hasattr(compositor, "request_pad_simple"):
                pad: Gst.Pad = compositor.request_pad_simple(pad_name)
            else:
                pad = compositor.get_request_pad(pad_name)
            if pad is None:
                raise RuntimeError(f"compositor refused to create sink pad {idx}")
            pad.set_property("xpos", xpos)
            pad.set_property("ypos", ypos)
            pad.set_property("width", pad_w)
            pad.set_property("height", pad_h)
            self._compositor_pads.append(pad)
            log.debug(
                "Compositor pad %d: xpos=%d ypos=%d size=%dx%d",
                idx,
                xpos,
                ypos,
                pad_w,
                pad_h,
            )

        # --- output: capsfilter → kmssink ---
        # The framerate here is critical: without it GstVideoAggregator has no
        # timer-based aggregation cycle and instead waits for all 6 input pads to
        # supply frames at the *same* PTS before compositing.  With 6 independent
        # RTSP clocks that never perfectly align, the compositor produces <1 fps
        # output despite 50-60% available CPU.  Setting framerate=N/1 switches the
        # compositor to fire every 1/N seconds and composite whatever frame each
        # pad currently has — the correct behaviour for a live multi-source display.
        # videorate is not needed: the compositor itself throttles output to exactly
        # cfg.display.framerate fps once the sink caps include a framerate.
        caps_str = (
            f"video/x-raw"
            f",width={cfg.display.width}"
            f",height={cfg.display.height}"
            f",framerate={cfg.display.framerate}/1"
        )
        out_capsfilter = _make("capsfilter", "out_capsfilter")
        out_capsfilter.set_property("caps", Gst.Caps.from_string(caps_str))

        kmssink = _make("kmssink", "kmssink")
        if connector_id is not None:
            kmssink.set_property("connector-id", connector_id)
        # Allow kmssink to render without strict sync to avoid underruns on slow streams
        kmssink.set_property("sync", False)

        self.pipeline.add(compositor, out_capsfilter, kmssink)

        if not compositor.link(out_capsfilter):
            raise RuntimeError("Failed to link compositor → out_capsfilter")
        if not out_capsfilter.link(kmssink):
            raise RuntimeError("Failed to link out_capsfilter → kmssink")

        log.debug(
            "Pipeline skeleton built: %d×%d grid, %d cell pad(s)",
            cfg.display.rows,
            cfg.display.cols,
            len(cfg.cells),
        )

    # ------------------------------------------------------------------
    # Bus / error handling
    # ------------------------------------------------------------------

    def attach_bus_handler(self, loop: GLib.MainLoop) -> None:
        """Attach a bus watcher that logs errors and quits *loop* on EOS."""
        self._loop = loop
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

    def _on_bus_message(self, bus: Gst.Bus, message: Gst.Message) -> None:
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            src_name = message.src.get_name() if message.src else "unknown"
            log.error("GStreamer error from %s: %s", src_name, err.message)
            if debug:
                log.debug("Debug info: %s", debug)
            # Don't quit the loop — individual cells handle their own reconnects.
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            src_name = message.src.get_name() if message.src else "unknown"
            log.warning("GStreamer warning from %s: %s", src_name, warn.message)
        elif t == Gst.MessageType.EOS:
            log.info("Pipeline received EOS — stopping")
            if self._loop:
                self._loop.quit()
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipeline:
                old, new, pending = message.parse_state_changed()
                log.debug(
                    "Pipeline state: %s → %s",
                    Gst.Element.state_get_name(old),
                    Gst.Element.state_get_name(new),
                )
