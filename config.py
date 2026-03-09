"""Configuration loading and validation for viewport."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)

GRID_ROWS = 3
GRID_COLS = 2
CELL_COUNT = GRID_ROWS * GRID_COLS


@dataclass
class DisplayConfig:
    width: int = 1920
    height: int = 1080
    # Target compositor output framerate.  Without an explicit framerate the
    # GstVideoAggregator compositor waits for all 6 independent RTSP clocks to
    # produce frames at the *same* timestamp before compositing, which almost
    # never happens and results in <1 fps output despite available CPU headroom.
    # Setting this to the camera stream framerate (or any reasonable value)
    # switches the compositor to a fixed-interval timer that composites
    # whatever frame each pad currently has every 1/framerate seconds.
    framerate: int = 15

    @property
    def cell_width(self) -> int:
        return self.width // GRID_COLS

    @property
    def cell_height(self) -> int:
        return self.height // GRID_ROWS


@dataclass
class DecoderConfig:
    prefer_hardware: bool = True


@dataclass
class CellConfig:
    streams: list[str]
    rotation_interval: int = 0   # seconds; 0 = no rotation
    codec: str = "h264"          # "h264" or "h265"

    def __post_init__(self) -> None:
        if not self.streams:
            raise ValueError("Each cell must have at least one stream URL")
        self.codec = self.codec.lower()
        if self.codec not in ("h264", "h265"):
            raise ValueError(f"codec must be 'h264' or 'h265', got '{self.codec}'")
        if self.rotation_interval < 0:
            raise ValueError("rotation_interval must be >= 0")
        if len(self.streams) == 1:
            self.rotation_interval = 0  # no rotation needed with a single stream


@dataclass
class AppConfig:
    display: DisplayConfig
    decoder: DecoderConfig
    cells: list[CellConfig]

    def __post_init__(self) -> None:
        if len(self.cells) > CELL_COUNT:
            raise ValueError(
                f"Too many cells configured ({len(self.cells)}); maximum is {CELL_COUNT}"
            )
        # Pad with blank placeholder cells if fewer than CELL_COUNT are given.
        # Blank cells show black; they have no streams and no rotation.
        while len(self.cells) < CELL_COUNT:
            self.cells.append(None)  # type: ignore[arg-type]


_BLANK_CELL: Optional[CellConfig] = None  # sentinel for empty cells


def load_config(path: str) -> AppConfig:
    """Load and validate configuration from a YAML file."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with config_path.open() as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ValueError("Config file must be a YAML mapping")

    # Display
    disp_raw = raw.get("display", {})
    display = DisplayConfig(
        width=int(disp_raw.get("width", 1920)),
        height=int(disp_raw.get("height", 1080)),
        framerate=int(disp_raw.get("framerate", 15)),
    )

    # Decoder
    dec_raw = raw.get("decoder", {})
    decoder = DecoderConfig(
        prefer_hardware=bool(dec_raw.get("prefer_hardware", True)),
    )

    # Cells
    cells_raw = raw.get("cells", [])
    cells: list[CellConfig] = []
    for i, c in enumerate(cells_raw):
        if c is None:
            cells.append(None)  # type: ignore[arg-type]
            continue
        try:
            cells.append(
                CellConfig(
                    streams=c["streams"],
                    rotation_interval=int(c.get("rotation_interval", 0)),
                    codec=str(c.get("codec", "h264")),
                )
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Invalid cell config at index {i}: {exc}") from exc

    cfg = AppConfig(display=display, decoder=decoder, cells=cells)
    log.info(
        "Loaded config: %dx%d @ %d fps display, %d cell(s) configured",
        display.width,
        display.height,
        display.framerate,
        sum(1 for c in cfg.cells if c is not None),
    )
    return cfg
