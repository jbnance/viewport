#!/usr/bin/env python3
"""viewport — headless RTSP multi-stream display for Raspberry Pi.

Displays up to 6 RTSP streams in a 3×2 grid on a 1080p display using
GStreamer's DRM/KMS sink (no X11 or Wayland required).

Usage:
    python3 main.py config.yaml

All settings (log level, display options, decoder preferences, streams) are
read from the YAML configuration file.  See config.example.yaml for a
documented reference.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gst, GLib  # noqa: E402

from config import load_config
from pipeline import ViewportPipeline
from cell import Cell, detect_decoders


def _setup_logging(level_name: str) -> None:
    # logging.basicConfig() is a no-op after its first call, so we must set the
    # level directly on the root logger and any handlers it already has.
    level = getattr(logging, level_name.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        handler.setLevel(level)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="viewport: headless RTSP grid display for Raspberry Pi"
    )
    p.add_argument(
        "config",
        metavar="CONFIG",
        help="Path to YAML configuration file (see config.example.yaml)",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    # Bootstrap with INFO until the config file tells us the real level.
    # INFO lets startup messages (e.g. "Loaded config") through; the user-
    # configured level is applied by _setup_logging() after the config loads.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("main")

    # GStreamer init — must happen before any Gst calls
    Gst.init(None)

    # Load configuration (determines log level, connector id, streams, …)
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        log.error("Configuration error: %s", exc)
        return 1

    # Apply the log level from the config file now that we have it.
    _setup_logging(config.log_level)
    log = logging.getLogger("main")

    # Probe the GStreamer registry once to pick hardware vs. software decoders.
    # This runs a single registry query per codec instead of repeating it on
    # every stream connection, rotation, and reconnect across all 6 cells.
    decoders = detect_decoders(config.decoder)

    # Build the shared pipeline (compositor + kmssink)
    try:
        vp = ViewportPipeline(config, connector_id=config.display.connector_id)
    except RuntimeError as exc:
        log.error("Pipeline setup failed: %s", exc)
        return 1

    # Create and start each cell
    cells: list[Cell] = []
    for i, cell_cfg in enumerate(config.cells):
        cell = Cell(
            index=i,
            cell_cfg=cell_cfg,
            decoders=decoders,
            pipeline=vp.pipeline,
            compositor_pad=vp.get_compositor_pad(i),
            preload_timeout=config.display.preload_timeout,
        )
        cells.append(cell)

    for cell in cells:
        log.debug("Cell %d: starting", cell.index)
        try:
            cell.start()
        except RuntimeError as exc:
            log.error("Cell %d failed to start: %s", cell.index, exc)
            return 1

    # GLib main loop
    loop = GLib.MainLoop()
    vp.attach_bus_handler(loop)

    # Start playing
    try:
        vp.play()
    except RuntimeError as exc:
        log.error("Failed to start pipeline: %s", exc)
        return 1

    # Handle Ctrl-C and SIGTERM gracefully
    def _shutdown(signum, frame):  # type: ignore[no-untyped-def]
        log.info("Shutting down (signal %d)…", signum)
        loop.quit()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info(
        "viewport running — %dx%d @ %d fps, %d cell(s). Press Ctrl-C to stop.",
        config.display.width,
        config.display.height,
        config.display.framerate,
        sum(1 for c in config.cells if c is not None),
    )

    try:
        loop.run()
    finally:
        log.info("Stopping cells…")
        for cell in cells:
            cell.stop()
        vp.stop()
        log.info("Exited.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
