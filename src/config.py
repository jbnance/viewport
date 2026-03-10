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
    # DRM connector ID for kmssink.  None = auto-detect (works for most setups).
    # Run `modetest -c` on the Pi to list available connector IDs.
    connector_id: Optional[int] = None

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
class ResolvedDecoders:
    """Decoder element names chosen once at startup by probing the GStreamer registry.

    Stored as strings (element factory names) so that each Cell branch can call
    Gst.ElementFactory.make(name) without re-probing hardware availability on
    every stream connection or reconnection.
    """
    h264: str   # e.g. "v4l2slh264dec" or "avdec_h264"
    h265: str   # e.g. "v4l2slh265dec" or "avdec_h265"


@dataclass
class CellConfig:
    streams: list[str]           # resolved RTSP URLs
    rotation_interval: int = 0   # seconds; 0 = no rotation
    codec: str = "h264"          # "h264" or "h265"
    # Human-readable label for each resolved URL, parallel to streams[].
    # label[i] == streams[i] means the stream has no name (raw URL).
    # Used only for log messages; empty list is valid (falls back to URL).
    stream_labels: list[str] = field(default_factory=list)

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
    log_level: str = "INFO"   # DEBUG | INFO | WARNING | ERROR

    def __post_init__(self) -> None:
        if len(self.cells) > CELL_COUNT:
            raise ValueError(
                f"Too many cells configured ({len(self.cells)}); maximum is {CELL_COUNT}"
            )
        # Pad with blank placeholder cells if fewer than CELL_COUNT are given.
        # Blank cells show black; they have no streams and no rotation.
        while len(self.cells) < CELL_COUNT:
            self.cells.append(None)  # type: ignore[arg-type]

        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR"}
        self.log_level = self.log_level.upper()
        if self.log_level not in valid_levels:
            raise ValueError(
                f"log_level must be one of {sorted(valid_levels)}, got '{self.log_level}'"
            )


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
    raw_connector = disp_raw.get("connector_id", None)
    display = DisplayConfig(
        width=int(disp_raw.get("width", 1920)),
        height=int(disp_raw.get("height", 1080)),
        framerate=int(disp_raw.get("framerate", 15)),
        connector_id=int(raw_connector) if raw_connector is not None else None,
    )

    # Decoder
    dec_raw = raw.get("decoder", {})
    decoder = DecoderConfig(
        prefer_hardware=bool(dec_raw.get("prefer_hardware", True)),
    )

    # ------------------------------------------------------------------
    # Named stream registry: optional top-level 'streams:' mapping (name → URL)
    # ------------------------------------------------------------------
    stream_reg: dict[str, str] = {}
    streams_section = raw.get("streams", {})
    if streams_section:
        if not isinstance(streams_section, dict):
            raise ValueError(
                "Top-level 'streams:' must be a YAML mapping (name: url). "
                "To list streams in a cell use the 'streams:' key inside a cell entry."
            )
        for name, url in streams_section.items():
            if not isinstance(url, str) or "://" not in url:
                raise ValueError(
                    f"Stream '{name}': value must be a full URL containing '://' "
                    f"(stream names cannot point to other names)"
                )
            stream_reg[name] = url

    # Reverse-lookup: URL → name (for recovering stream names from group entries)
    def _name_for(url: str) -> Optional[str]:
        return next((k for k, v in stream_reg.items() if v == url), None)

    # ------------------------------------------------------------------
    # Group registry: optional top-level 'groups:' mapping (name → [names/URLs])
    # Groups are resolved eagerly to URL lists; they cannot reference other groups.
    # ------------------------------------------------------------------
    group_reg: dict[str, list[str]] = {}
    groups_section = raw.get("groups", {})
    if groups_section:
        if not isinstance(groups_section, dict):
            raise ValueError(
                "Top-level 'groups:' must be a YAML mapping (name: [stream, ...])"
            )
        for gname, members in groups_section.items():
            if gname in stream_reg:
                raise ValueError(
                    f"Group '{gname}' conflicts with a stream of the same name. "
                    f"Stream names and group names must be unique."
                )
            if not isinstance(members, list) or not members:
                raise ValueError(
                    f"Group '{gname}': value must be a non-empty list of stream names or URLs"
                )
            resolved_members: list[str] = []
            for j, item in enumerate(members):
                item_str = str(item)
                if "://" in item_str:
                    resolved_members.append(item_str)
                elif item_str in stream_reg:
                    resolved_members.append(stream_reg[item_str])
                else:
                    raise ValueError(
                        f"Group '{gname}', item {j}: unknown stream name '{item_str}' "
                        f"(groups cannot reference other groups)"
                    )
            group_reg[gname] = resolved_members

    # ------------------------------------------------------------------
    # Cells
    # ------------------------------------------------------------------
    cells_raw = raw.get("cells", [])
    cells: list[CellConfig] = []
    for i, c in enumerate(cells_raw):
        if c is None:
            cells.append(None)  # type: ignore[arg-type]
            continue
        try:
            if "streams" not in c:
                raise ValueError("must specify 'streams:'")
            raw_list = c["streams"]
            if not isinstance(raw_list, list) or not raw_list:
                raise ValueError("'streams:' must be a non-empty list")

            resolved_streams: list[str] = []
            resolved_labels: list[str] = []
            for j, item in enumerate(raw_list):
                item_str = str(item)
                if "://" in item_str:
                    # Raw URL — label is the URL itself
                    resolved_streams.append(item_str)
                    resolved_labels.append(item_str)
                elif item_str in stream_reg:
                    # Named stream
                    resolved_streams.append(stream_reg[item_str])
                    resolved_labels.append(item_str)
                elif item_str in group_reg:
                    # Group — flatten all member URLs inline
                    for k, url in enumerate(group_reg[item_str]):
                        resolved_streams.append(url)
                        sname = _name_for(url) or f"{item_str}[{k}]"
                        resolved_labels.append(f"{sname} [{item_str}]")
                else:
                    raise ValueError(
                        f"streams[{j}]: unknown stream name or group '{item_str}' "
                        f"(not a URL, not defined in top-level 'streams:', "
                        f"and not defined in top-level 'groups:')"
                    )

            cells.append(
                CellConfig(
                    streams=resolved_streams,
                    stream_labels=resolved_labels,
                    rotation_interval=int(c.get("rotation_interval", 0)),
                    codec=str(c.get("codec", "h264")),
                )
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Invalid cell config at index {i}: {exc}") from exc

    cfg = AppConfig(
        display=display,
        decoder=decoder,
        cells=cells,
        log_level=str(raw.get("log_level", "INFO")),
    )
    log.info(
        "Loaded config: %dx%d @ %d fps display, %d cell(s) configured",
        display.width,
        display.height,
        display.framerate,
        sum(1 for c in cfg.cells if c is not None),
    )
    return cfg
