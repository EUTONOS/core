from __future__ import annotations
import math
import re
from typing import Any
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None


def image_transform(width: int, height: int, bounds: dict, *, route: str, verified: bool) -> dict:
    """Physical source geometry; unknown routes remain observation-only."""
    return {"schema": "image-to-screen.v1", "verified": bool(verified), "route": route,
            "width": width, "height": height, "scale_x": 1.0, "scale_y": 1.0,
            "offset_x": bounds.get("left", 0), "offset_y": bounds.get("top", 0),
            "source_bounds": bounds, "root_offset_x": 0, "root_offset_y": 0}


def crop_transform(parent: dict, bbox: tuple) -> dict:
    child = dict(parent)
    child.update(width=bbox[2] - bbox[0], height=bbox[3] - bbox[1],
                 offset_x=float(parent.get("offset_x", 0)) + bbox[0] * float(parent.get("scale_x", 1)),
                 offset_y=float(parent.get("offset_y", 0)) + bbox[1] * float(parent.get("scale_y", 1)),
                 root_offset_x=int(parent.get("root_offset_x", 0)) + bbox[0],
                 root_offset_y=int(parent.get("root_offset_y", 0)) + bbox[1])
    return child


def image_point_to_screen(transform: dict, coordinate: tuple) -> tuple | None:
    if transform.get("verified") is not True:
        return None
    try:
        x, y = coordinate
        sx, sy = float(transform["scale_x"]), float(transform["scale_y"])
        ox, oy = float(transform["offset_x"]), float(transform["offset_y"])
        if not all(math.isfinite(v) for v in (x, y, sx, sy, ox, oy)) or sx <= 0 or sy <= 0:
            return None
        if not (0 <= x < transform["width"] and 0 <= y < transform["height"]):
            return None
        content = transform.get("content_bounds")
        if content and not (content["left"] <= x < content["right"] and content["top"] <= y < content["bottom"]):
            return None
        return round(ox + x * sx), round(oy + y * sy)
    except (KeyError, TypeError, ValueError):
        return None


def render_planner_grid(image: Any, columns: int, rows: int, *, zoom: bool = False) -> tuple[Any, dict]:
    """Rulers live outside the content. Grid lines never cover labels with boxes."""
    scale = 1024 / max(image.size) if zoom else min(1.0, 1488 / max(image.size))
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    content = image.convert("RGB").resize(size, Image.Resampling.LANCZOS)
    margin = 40
    canvas = Image.new("RGB", (size[0] + margin, size[1] + margin), (30, 30, 30))
    canvas.paste(content, (margin, margin))
    lines = Image.new("RGBA", canvas.size)
    draw = ImageDraw.Draw(lines)
    for col in range(columns + 1):
        x = margin + round(col * size[0] / columns)
        draw.line((x, margin, x, canvas.height - 1), fill=(255, 220, 0, 90), width=1)
    for row in range(rows + 1):
        y = margin + round(row * size[1] / rows)
        draw.line((margin, y, canvas.width - 1, y), fill=(255, 220, 0, 90), width=1)
    canvas = Image.alpha_composite(canvas.convert("RGBA"), lines).convert("RGB")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=16)
    for col in range(columns):
        draw.text((margin + (col + .5) * size[0] / columns, 18), column_label(col), font=font, fill="white", anchor="mm")
    for row in range(rows):
        draw.text((18, margin + (row + .5) * size[1] / rows), str(row + 1), font=font, fill="white", anchor="mm")
    return canvas, {"scale_x": size[0] / image.width, "scale_y": size[1] / image.height,
                    "margin_x": margin, "margin_y": margin}


def delivered_transform(native: dict, render: dict, source_size: list, delivered_size: tuple) -> dict:
    result = dict(native)
    rx = delivered_size[0] / source_size[0]
    ry = delivered_size[1] / source_size[1]
    sx = float(render.get("scale_x", 1)) * rx
    sy = float(render.get("scale_y", 1)) * ry
    result.update(scale_x=float(native.get("scale_x", 1)) / sx,
                  scale_y=float(native.get("scale_y", 1)) / sy,
                  offset_x=float(native.get("offset_x", 0)) - float(render.get("margin_x", 0)) * float(native.get("scale_x", 1)) / float(render.get("scale_x", 1)),
                  offset_y=float(native.get("offset_y", 0)) - float(render.get("margin_y", 0)) * float(native.get("scale_y", 1)) / float(render.get("scale_y", 1)),
                  width=delivered_size[0], height=delivered_size[1],
                  content_bounds={"left": float(render.get("margin_x", 0)) * rx, "top": float(render.get("margin_y", 0)) * ry,
                                  "right": delivered_size[0], "bottom": delivered_size[1]})
    return result

GRID_OFFSET_PRESETS: dict[str, tuple[float, float]] = {
    "center": (0.5, 0.5),
    "left-center": (0.25, 0.5),
    "right-center": (0.75, 0.5),
    "top-center": (0.5, 0.25),
    "bottom-center": (0.5, 0.75),
    "top-left": (0.25, 0.25),
    "top-right": (0.75, 0.25),
    "bottom-left": (0.25, 0.75),
    "bottom-right": (0.75, 0.75),
}


def column_label(index: int) -> str:
    label = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        label = chr(65 + remainder) + label
    return label


def grid_cell_center(cell: str, grid: dict[str, Any]) -> tuple[int, int] | None:
    bbox = grid_cell_bbox(cell, grid)
    if bbox is None:
        return None
    return (bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2


def grid_cell_point(
    cell: str,
    grid: dict[str, Any],
    *,
    offset: str = "center",
    x_pct: float | None = None,
    y_pct: float | None = None,
) -> tuple[int, int] | None:
    bbox = grid_cell_bbox(cell, grid)
    if bbox is None:
        return None
    left, top, right, bottom = bbox
    width = max(1, right - left)
    height = max(1, bottom - top)
    if x_pct is None or y_pct is None:
        preset = GRID_OFFSET_PRESETS.get(offset.strip().lower()) or GRID_OFFSET_PRESETS["center"]
        x_pct = preset[0] if x_pct is None else x_pct
        y_pct = preset[1] if y_pct is None else y_pct
    x_pct = min(1.0, max(0.0, float(x_pct)))
    y_pct = min(1.0, max(0.0, float(y_pct)))
    return (
        left + int(round((width - 1) * x_pct)),
        top + int(round((height - 1) * y_pct)),
    )


def grid_cell_bbox(cell: str, grid: dict[str, Any]) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"([A-Za-z]+)([1-9][0-9]*)", str(cell).strip())
    if match is None or not isinstance(grid, dict):
        return None
    letters, digits = match.groups()
    col = 0
    for char in letters:
        col = col * 26 + (ord(char.upper()) - 64)
    col -= 1
    row = int(digits) - 1
    try:
        columns = int(grid["columns"])
        rows = int(grid["rows"])
        cell_width = float(grid["cell_width"])
        cell_height = float(grid["cell_height"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if columns <= 0 or rows <= 0 or cell_width <= 0 or cell_height <= 0:
        return None
    if not math.isfinite(cell_width) or not math.isfinite(cell_height):
        return None
    if col < 0 or row < 0 or col >= columns or row >= rows:
        return None
    left = int(round(col * cell_width))
    top = int(round(row * cell_height))
    right = int(round((col + 1) * cell_width))
    bottom = int(round((row + 1) * cell_height))
    return left, top, right, bottom

