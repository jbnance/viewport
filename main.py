#!/usr/bin/env python3
"""viewport — headless RTSP multi-stream display for Raspberry Pi.

Displays up to 6 RTSP streams in a 3×2 grid on a 1080p display using
GStreamer's DRM/KMS sink (no X11 or Wayland required).

Usage:
    python3 main.py --config config.yaml
    python3 main.py --config config.yaml --connector-id 42
    python3 main.py --config config.yaml --log-level DEBUG
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from typing import Optional

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gst, GLib  # noqa: E402

from config import load_config
from pipeline import ViewportPipeline
from cell import Cell


def _setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="viewport: headless RTSP grid display for Raspberry Pi"
    )
    p.add_argument(
        "--config",
        required=True,
        metavar="FILE",
        help="Path to YAML configuration file",
    )
    p.add_argument(
        "--connector-id",
        type=int,
        default=None,
        metavar="N",
        help=(
            "DRM connector ID to use for output (default: auto-detect). "
            "List connectors with: modetest -c"
        ),
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    _setup_logging(args.log_level)
    log = logging.getLogger("main")

    # GStreamer init — must happen before any Gst calls
    Gst.init(None)

    # Load configuration
    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        log.error("Configuration error: %s", exc)
        return 1

    # Build the shared pipeline (compositor + kmssink)
    try:
        vp = ViewportPipeline(config, connector_id=args.connector_id)
    except RuntimeError as exc:
        log.error("Pipeline setup failed: %s", exc)
        return 1

    # Create and start each cell
    cells: list[Cell] = []
    for i, cell_cfg in enumerate(config.cells):
        cell = Cell(
            index=i,
            cell_cfg=cell_cfg,
            dec_cfg=config.decoder,
            pipeline=vp.pipeline,
            compositor_pad=vp.get_compositor_pad(i),
            cell_width=config.display.cell_width,
            cell_height=config.display.cell_height,
        )
        cells.append(cell)

    for cell in cells:
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
        "viewport running — %dx%d, %d cell(s). Press Ctrl-C to stop.",
        config.display.width,
        config.display.height,
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
