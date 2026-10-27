"""One planner image per result; native pixels stay private for verification."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from simon_paths import COMMAND_SCREENSHOT_DIR
from simon_geometry import delivered_transform


def observation_packets(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from observation_packets(item)
        if any(value.get(key) for key in ("grid_screenshot_path", "grid_crop_screenshot_path", "candidate_overlay_screenshot_path", "raw_screenshot_path")):
            yield value
    elif isinstance(value, list):
        for item in value:
            yield from observation_packets(item)


def planner_image(result: dict[str, Any], args: dict[str, Any]) -> dict[str, Any] | None:
    packets = list(observation_packets(result.get("data", {})))
    if not packets:
        return None
    # Follow-up observations are newer than the enclosing action's before/after
    # evidence. Prefer an explicit next observation when one is present.
    data = result.get("data", {})
    next_packets = list(observation_packets(data.get("next_observation") or data.get("follow_up_capture") or {}))
    packet = next_packets[-1] if next_packets else packets[-1]
    source = next((packet.get(key) for key in ("grid_crop_screenshot_path", "grid_screenshot_path", "candidate_overlay_screenshot_path", "raw_screenshot_path") if packet.get(key)), None)
    path = Path(str(source))
    root = COMMAND_SCREENSHOT_DIR.resolve()
    if not path.resolve().is_relative_to(root) or not path.is_file():
        return None
    destination = path.with_name(path.stem + "_planner.png")
    max_edge = min(1568, max(1, int(args.get("image_max_edge", 1568))))
    with Image.open(path) as original:
        image = original.copy()
        original_size = list(image.size)
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        image.save(destination)
    output = {"path": str(destination), "width": image.width, "height": image.height,
              "size_bytes": destination.stat().st_size, "source_size": original_size,
              "targeting": "labels_only", "request_id": packet.get("request_id")}
    if packet.get("image_to_screen"):
        render = (packet.get("grid_render") or {}) if source != packet.get("raw_screenshot_path") else {}
        output["image_to_screen"] = delivered_transform(packet["image_to_screen"], render, original_size, image.size)
    extras = []
    for name, keys in {"raw": ("raw_screenshot_path", "raw_crop_screenshot_path"),
                       "grid": ("grid_screenshot_path", "grid_crop_screenshot_path"),
                       "candidates": ("candidate_overlay_screenshot_path",),
                       "context": ("context_screenshot_path", "context_grid_screenshot_path")}.items():
        if name not in args.get("include_images", []):
            continue
        source_packet = packet.get("context", {}) if name == "context" else packet
        if name == "context":
            keys = ("raw_screenshot_path", "grid_screenshot_path", *keys)
        for key in keys:
            candidate = Path(source_packet[key]) if source_packet.get(key) else None
            if candidate and candidate.resolve().is_relative_to(root) and candidate.is_file():
                extras.append({"kind": name, "path": str(candidate), "size_bytes": candidate.stat().st_size})
    if extras:
        output["extra_images"] = extras
    return output
