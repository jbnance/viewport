"""Configuration loading and validation for viewport."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)


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
    # Grid dimensions — number of rows and columns in the display layout.
    # Each cell occupies one slot; cells with row_span/col_span > 1 span
    # multiple slots.  Defaults to the original 3×2 layout.
    rows: int = 3
    cols: int = 2
    # DRM connector ID for kmssink.  None = auto-detect (works for most setups).
    # Run `modetest -c` on the Pi to list available connector IDs.
    connector_id: Optional[int] = None
    # Shadow-branch preload timeout.  When a cell preloads the next stream
    # (or refreshes a single-URL cell connection) in the background, it waits
    # up to this many seconds for the first decoded frame before giving up.
    preload_timeout: int = 10
    # Proactive connection refresh for single-URL cells.  After a connection
    # has been alive for this many hours the cell preloads a fresh instance of
    # the same stream in the background and hot-swaps to it seamlessly —
    # preventing the gradual degradation some cameras cause on very long-lived
    # RTSP/TCP sessions.  Set to 0 (the default) to disable.
    max_connection_age_hours: float = 0.0
    # TCP timeout for RTSP connections (seconds).  Bounds how long rtspsrc
    # blocks during connection attempts and teardown when a camera is
    # unreachable.  Lower values detect failures faster; raise if cameras
    # are on a slow or lossy network.
    tcp_timeout: int = 5

    def __post_init__(self) -> None:
        if self.width < 1 or self.height < 1:
            raise ValueError(
                f"display width and height must each be >= 1, "
                f"got {self.width}×{self.height}"
            )
        if self.framerate < 1:
            raise ValueError(
                f"display.framerate must be >= 1 (got {self.framerate!r})"
            )
        if self.rows < 1 or self.cols < 1:
            raise ValueError(
                f"display rows and cols must each be >= 1, got {self.rows}×{self.cols}"
            )
        if self.connector_id is not None and self.connector_id < 0:
            raise ValueError(
                f"display.connector_id must be >= 0 (got {self.connector_id!r})"
            )
        if self.preload_timeout < 1:
            raise ValueError(
                f"display.preload_timeout must be >= 1 second "
                f"(got {self.preload_timeout!r})"
            )
        if self.max_connection_age_hours < 0:
            raise ValueError(
                f"display.max_connection_age_hours must be >= 0 "
                f"(got {self.max_connection_age_hours!r})"
            )
        if self.tcp_timeout < 1:
            raise ValueError(
                f"display.tcp_timeout must be >= 1 second "
                f"(got {self.tcp_timeout!r})"
            )

    @property
    def cell_width(self) -> int:
        """Width of one 1×1 grid cell in pixels."""
        return self.width // self.cols

    @property
    def cell_height(self) -> int:
        """Height of one 1×1 grid cell in pixels."""
        return self.height // self.rows


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

    h264: str  # e.g. "v4l2slh264dec" or "avdec_h264"
    h265: str  # e.g. "v4l2slh265dec" or "avdec_h265"


@dataclass
class CellConfig:
    streams: list[str]  # resolved RTSP URLs
    rotation_interval: int = 0  # seconds; 0 = no rotation
    codec: str = "h264"  # "h264" or "h265"
    # Cell span — how many grid columns / rows this cell occupies.
    # Defaults to 1×1 (a single slot).  Auto-placement handles positioning.
    col_span: int = 1
    row_span: int = 1
    # Human-readable label for each resolved URL, parallel to streams[].
    # label[i] == streams[i] means the stream has no name (raw URL).
    # Used only for log messages; empty list is valid (falls back to URL).
    stream_labels: list[str] = field(default_factory=list)
    # Grid position — set by _autoplace_cells() in load_config(), not by the user.
    row: int = field(default=0, init=False)
    col: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not self.streams:
            raise ValueError("Each cell must have at least one stream URL")
        self.codec = self.codec.lower()
        if self.codec not in ("h264", "h265"):
            raise ValueError(f"codec must be 'h264' or 'h265', got '{self.codec}'")
        if self.rotation_interval < 0:
            raise ValueError("rotation_interval must be >= 0")
        if self.col_span < 1 or self.row_span < 1:
            raise ValueError(
                f"col_span and row_span must each be >= 1, "
                f"got col_span={self.col_span} row_span={self.row_span}"
            )
        if len(self.streams) == 1:
            if self.rotation_interval != 0:
                log.warning(
                    "rotation_interval=%d ignored: cell has only one stream",
                    self.rotation_interval,
                )
            self.rotation_interval = 0  # no rotation needed with a single stream


@dataclass
class AppConfig:
    display: DisplayConfig
    decoder: DecoderConfig
    cells: list[CellConfig]
    log_level: str = "INFO"  # DEBUG | INFO | WARNING | ERROR
    # GStreamer debug filter string — same format as the GST_DEBUG environment
    # variable (e.g. "rtspsrc:4", "*:2", "rtspsrc:4,compositor:3").
    # When set this overrides any GST_DEBUG value that was present at startup.
    # None (the default) leaves GStreamer's debug thresholds untouched.
    # Level reference: 0=none 1=error 2=warning 3=fixme 4=info 5=debug 6=log
    gst_debug: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.cells:
            raise ValueError("At least one cell must be configured")

        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR"}
        self.log_level = self.log_level.upper()
        if self.log_level not in valid_levels:
            raise ValueError(
                f"log_level must be one of {sorted(valid_levels)}, got '{self.log_level}'"
            )


def _autoplace_cells(
    raw_cells: list,
    rows: int,
    cols: int,
) -> list[CellConfig]:
    """Auto-place cells in the grid using row-major (left-to-right, top-to-bottom) order.

    Each cell is placed at the first available rectangular block that fits its
    col_span × row_span.  Null entries in *raw_cells* advance the cursor by one
    1×1 slot without producing a cell (blank/black area).

    Sets .row and .col on each CellConfig in-place; returns only the non-null cells.
    Raises ValueError if any cell cannot be placed (grid too full or span too large).
    """
    occupied = [[False] * cols for _ in range(rows)]
    cursor_row, cursor_col = 0, 0
    result: list[CellConfig] = []

    def _advance() -> None:
        nonlocal cursor_row, cursor_col
        cursor_col += 1
        if cursor_col >= cols:
            cursor_col = 0
            cursor_row += 1

    def _fits(r: int, c: int, rs: int, cs: int) -> bool:
        if r + rs > rows or c + cs > cols:
            return False
        return all(not occupied[r + dr][c + dc] for dr in range(rs) for dc in range(cs))

    for idx, raw in enumerate(raw_cells):
        rs = 1 if raw is None else raw.row_span
        cs = 1 if raw is None else raw.col_span

        if cs > cols or rs > rows:
            raise ValueError(
                f"Cell {idx}: span {rs}×{cs} exceeds grid size {rows}×{cols}"
            )

        placed = False
        while cursor_row + rs <= rows:
            if _fits(cursor_row, cursor_col, rs, cs):
                if raw is not None:
                    raw.row = cursor_row
                    raw.col = cursor_col
                    result.append(raw)
                for dr in range(rs):
                    for dc in range(cs):
                        occupied[cursor_row + dr][cursor_col + dc] = True
                # Advance cursor past this cell's right edge
                cursor_col += cs
                if cursor_col >= cols:
                    cursor_col = 0
                    cursor_row += 1
                placed = True
                break
            _advance()

        if not placed:
            which = "null placeholder" if raw is None else f"cell {idx}"
            raise ValueError(
                f"Layout error: {which} (span {rs}×{cs}) does not fit in the "
                f"remaining {rows}×{cols} grid space — check spans and cell count"
            )

    return result


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
        rows=int(disp_raw.get("rows", 3)),
        cols=int(disp_raw.get("cols", 2)),
        connector_id=int(raw_connector) if raw_connector is not None else None,
        preload_timeout=int(disp_raw.get("preload_timeout", 10)),
        max_connection_age_hours=float(disp_raw.get("max_connection_age_hours", 0.0)),
        tcp_timeout=int(disp_raw.get("tcp_timeout", 5)),
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
    parsed_cells: list = []  # CellConfig or None (null placeholder)
    for i, c in enumerate(cells_raw):
        if c is None:
            parsed_cells.append(None)
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

            log.debug(
                "Cell %d: %d stream(s) resolved — %s",
                i,
                len(resolved_streams),
                ", ".join(resolved_labels),
            )
            parsed_cells.append(
                CellConfig(
                    streams=resolved_streams,
                    stream_labels=resolved_labels,
                    rotation_interval=int(c.get("rotation_interval", 0)),
                    codec=str(c.get("codec", "h264")),
                    col_span=int(c.get("col_span", 1)),
                    row_span=int(c.get("row_span", 1)),
                )
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Invalid cell config at index {i}: {exc}") from exc

    # Auto-place cells into the grid (sets .row / .col on each CellConfig)
    try:
        cells = _autoplace_cells(parsed_cells, display.rows, display.cols)
    except ValueError as exc:
        raise ValueError(f"Grid layout error: {exc}") from exc

    raw_gst_debug = raw.get("gst_debug", None)
    cfg = AppConfig(
        display=display,
        decoder=decoder,
        cells=cells,
        log_level=str(raw.get("log_level", "INFO")),
        gst_debug=str(raw_gst_debug) if raw_gst_debug is not None else None,
    )
    log.info(
        "Loaded config: %dx%d @ %d fps, %d×%d grid, %d cell(s)",
        display.width,
        display.height,
        display.framerate,
        display.rows,
        display.cols,
        len(cfg.cells),
    )
    return cfg
