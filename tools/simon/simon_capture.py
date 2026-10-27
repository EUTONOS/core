from __future__ import annotations
import ctypes
from ctypes import wintypes
from typing import Any
import os

def enable_process_dpi_awareness() -> dict[str, Any]:
    if os.name != "nt":
        return {"enabled": False, "method": "non_windows"}
    user32 = getattr(ctypes.windll, "user32", None)
    if user32 is None:
        return {"enabled": False, "method": "user32_unavailable"}
    try:
        set_context = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if set_context is not None:
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            if bool(set_context(ctypes.c_void_p(-4))):
                return {"enabled": True, "method": "per_monitor_v2"}
    except Exception:
        pass
    try:
        shcore = getattr(ctypes.windll, "shcore", None)
        if shcore is not None and hasattr(shcore, "SetProcessDpiAwareness"):
            result = int(shcore.SetProcessDpiAwareness(2))
            if result == 0:
                return {"enabled": True, "method": "per_monitor"}
    except Exception:
        pass
    try:
        if bool(user32.SetProcessDPIAware()):
            return {"enabled": True, "method": "system_dpi_aware"}
    except Exception:
        pass
    return {"enabled": False, "method": "unchanged"}


DPI_AWARENESS = enable_process_dpi_awareness()

try:
    from PIL import Image, ImageGrab
except ImportError:
    Image = ImageGrab = None
try:
    import pyautogui
except ImportError:
    pyautogui = None
LAST_CAPTURE_ERROR = ""
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def capture_window_image(window: WindowInfo) -> Any | None:
    image, _meta = capture_window_image_with_details(window)
    return image


def capture_window_image_with_details(window: WindowInfo) -> tuple[Any | None, dict[str, Any]]:
    global LAST_CAPTURE_ERROR
    LAST_CAPTURE_ERROR = ""
    errors: list[str] = []
    attempts: list[dict[str, Any]] = []
    if ImageGrab is not None:
        try:
            image = ImageGrab.grab(bbox=(window.left, window.top, window.right, window.bottom), all_screens=True)
            attempts.append({"method": "window_pillow_all_screens", "status": "SUCCESS"})
            return image, {"method": "window_pillow_all_screens", "attempts": attempts}
        except Exception as first_exc:
            errors.append(f"pillow all_screens failed: {first_exc}")
            attempts.append({"method": "window_pillow_all_screens", "status": "FAILED", "error": str(first_exc)})
            try:
                image = ImageGrab.grab(bbox=(window.left, window.top, window.right, window.bottom))
                attempts.append({"method": "window_pillow_bbox", "status": "SUCCESS"})
                return image, {"method": "window_pillow_bbox", "attempts": attempts}
            except Exception as exc:
                errors.append(f"pillow bbox failed: {exc}")
                attempts.append({"method": "window_pillow_bbox", "status": "FAILED", "error": str(exc)})
    else:
        errors.append("Pillow ImageGrab is not installed.")
        attempts.append({"method": "window_pillow", "status": "SKIPPED", "error": "Pillow ImageGrab is not installed."})
    if pyautogui is not None:
        try:
            image = pyautogui.screenshot(region=(window.left, window.top, window.width, window.height))
            attempts.append({"method": "window_pyautogui_region", "status": "SUCCESS"})
            return image, {"method": "window_pyautogui_region", "attempts": attempts}
        except Exception as third_exc:
            errors.append(f"pyautogui failed: {third_exc}")
            attempts.append({"method": "window_pyautogui_region", "status": "FAILED", "error": str(third_exc)})
    try:
        image = capture_region_gdi(window.left, window.top, window.width, window.height)
        attempts.append({"method": "window_gdi_region", "status": "SUCCESS"})
        return image, {"method": "window_gdi_region", "attempts": attempts}
    except Exception as gdi_exc:
        errors.append(f"gdi failed: {gdi_exc}")
        attempts.append({"method": "window_gdi_region", "status": "FAILED", "error": str(gdi_exc)})
    try:
        image = capture_window_printwindow(window)
        attempts.append({"method": "window_printwindow", "status": "SUCCESS"})
        return image, {"method": "window_printwindow", "attempts": attempts}
    except Exception as print_exc:
        errors.append(f"printwindow failed: {print_exc}")
        attempts.append({"method": "window_printwindow", "status": "FAILED", "error": str(print_exc)})
    LAST_CAPTURE_ERROR = "; ".join(errors)
    return None, {"method": "capture_failed", "attempts": attempts, "error": LAST_CAPTURE_ERROR}


def capture_monitor_image(monitor: MonitorInfo) -> Any | None:
    image, _meta = capture_monitor_image_with_details(monitor)
    return image


def capture_monitor_image_with_details(monitor: MonitorInfo) -> tuple[Any | None, dict[str, Any]]:
    global LAST_CAPTURE_ERROR
    LAST_CAPTURE_ERROR = ""
    errors: list[str] = []
    attempts: list[dict[str, Any]] = []
    if ImageGrab is not None:
        try:
            image = ImageGrab.grab(bbox=(monitor.left, monitor.top, monitor.right, monitor.bottom), all_screens=True)
            attempts.append({"method": "monitor_pillow_all_screens", "status": "SUCCESS"})
            return image, {"method": "monitor_pillow_all_screens", "attempts": attempts}
        except Exception as first_exc:
            errors.append(f"pillow monitor all_screens failed: {first_exc}")
            attempts.append({"method": "monitor_pillow_all_screens", "status": "FAILED", "error": str(first_exc)})
            try:
                image = ImageGrab.grab(bbox=(monitor.left, monitor.top, monitor.right, monitor.bottom))
                attempts.append({"method": "monitor_pillow_bbox", "status": "SUCCESS"})
                return image, {"method": "monitor_pillow_bbox", "attempts": attempts}
            except Exception as exc:
                errors.append(f"pillow monitor bbox failed: {exc}")
                attempts.append({"method": "monitor_pillow_bbox", "status": "FAILED", "error": str(exc)})
    else:
        attempts.append({"method": "monitor_pillow", "status": "SKIPPED", "error": "Pillow ImageGrab is not installed."})
    if pyautogui is not None:
        try:
            image = pyautogui.screenshot(region=(monitor.left, monitor.top, monitor.width, monitor.height))
            attempts.append({"method": "monitor_pyautogui_region", "status": "SUCCESS"})
            return image, {"method": "monitor_pyautogui_region", "attempts": attempts}
        except Exception as third_exc:
            errors.append(f"pyautogui monitor failed: {third_exc}")
            attempts.append({"method": "monitor_pyautogui_region", "status": "FAILED", "error": str(third_exc)})
    try:
        image = capture_region_gdi(monitor.left, monitor.top, monitor.width, monitor.height)
        attempts.append({"method": "monitor_gdi_region", "status": "SUCCESS"})
        return image, {"method": "monitor_gdi_region", "attempts": attempts}
    except Exception as gdi_exc:
        errors.append(f"gdi monitor failed: {gdi_exc}")
        attempts.append({"method": "monitor_gdi_region", "status": "FAILED", "error": str(gdi_exc)})
    LAST_CAPTURE_ERROR = "; ".join(errors)
    return None, {"method": "capture_failed", "attempts": attempts, "error": LAST_CAPTURE_ERROR}


def capture_window_printwindow(window: WindowInfo) -> Any:
    if Image is None:
        raise RuntimeError("Pillow Image is not installed.")
    if window.width <= 0 or window.height <= 0:
        raise RuntimeError(f"Invalid capture size {window.width}x{window.height}.")
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    window_dc = user32.GetWindowDC(window.hwnd)
    if not window_dc:
        raise RuntimeError("GetWindowDC failed.")
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    if not memory_dc:
        user32.ReleaseDC(window.hwnd, window_dc)
        raise RuntimeError("CreateCompatibleDC failed.")
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, window.width, window.height)
    if not bitmap:
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(window.hwnd, window_dc)
        raise RuntimeError("CreateCompatibleBitmap failed.")
    old_object = gdi32.SelectObject(memory_dc, bitmap)
    try:
        rendered = user32.PrintWindow(window.hwnd, memory_dc, 2)
        if not rendered:
            rendered = user32.PrintWindow(window.hwnd, memory_dc, 0)
        if not rendered:
            raise RuntimeError("PrintWindow failed.")
        return bitmap_to_image(memory_dc, bitmap, window.width, window.height)
    finally:
        if old_object:
            gdi32.SelectObject(memory_dc, old_object)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(window.hwnd, window_dc)


def capture_desktop_image() -> Any | None:
    image, _meta = capture_desktop_image_with_details()
    return image


def capture_desktop_image_with_details() -> tuple[Any | None, dict[str, Any]]:
    global LAST_CAPTURE_ERROR
    LAST_CAPTURE_ERROR = ""
    errors: list[str] = []
    attempts: list[dict[str, Any]] = []
    if ImageGrab is not None:
        try:
            image = ImageGrab.grab(all_screens=True)
            attempts.append({"method": "desktop_pillow_all_screens", "status": "SUCCESS"})
            return image, {"method": "desktop_pillow_all_screens", "attempts": attempts}
        except Exception as first_exc:
            errors.append(f"pillow all_screens failed: {first_exc}")
            attempts.append({"method": "desktop_pillow_all_screens", "status": "FAILED", "error": str(first_exc)})
            try:
                image = ImageGrab.grab()
                attempts.append({"method": "desktop_pillow", "status": "SUCCESS"})
                return image, {"method": "desktop_pillow", "attempts": attempts}
            except Exception as exc:
                errors.append(f"pillow desktop failed: {exc}")
                attempts.append({"method": "desktop_pillow", "status": "FAILED", "error": str(exc)})
    else:
        attempts.append({"method": "desktop_pillow", "status": "SKIPPED", "error": "Pillow ImageGrab is not installed."})
    if pyautogui is not None:
        try:
            image = pyautogui.screenshot()
            attempts.append({"method": "desktop_pyautogui", "status": "SUCCESS"})
            return image, {"method": "desktop_pyautogui", "attempts": attempts}
        except Exception as third_exc:
            errors.append(f"pyautogui failed: {third_exc}")
            attempts.append({"method": "desktop_pyautogui", "status": "FAILED", "error": str(third_exc)})
    try:
        user32 = ctypes.windll.user32
        left = user32.GetSystemMetrics(76)
        top = user32.GetSystemMetrics(77)
        width = user32.GetSystemMetrics(78)
        height = user32.GetSystemMetrics(79)
        image = capture_region_gdi(left, top, width, height)
        attempts.append({"method": "desktop_gdi_virtual_screen", "status": "SUCCESS"})
        return image, {"method": "desktop_gdi_virtual_screen", "attempts": attempts}
    except Exception as gdi_exc:
        errors.append(f"gdi failed: {gdi_exc}")
        attempts.append({"method": "desktop_gdi_virtual_screen", "status": "FAILED", "error": str(gdi_exc)})
    LAST_CAPTURE_ERROR = "; ".join(errors)
    return None, {"method": "capture_failed", "attempts": attempts, "error": LAST_CAPTURE_ERROR}


def get_virtual_screen_origin() -> tuple[int, int]:
    user32 = ctypes.windll.user32
    return int(user32.GetSystemMetrics(76)), int(user32.GetSystemMetrics(77))


def capture_region_gdi(left: int, top: int, width: int, height: int) -> Any:
    if Image is None:
        raise RuntimeError("Pillow Image is not installed.")
    if width <= 0 or height <= 0:
        raise RuntimeError(f"Invalid capture size {width}x{height}.")
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    screen_dc = user32.GetDC(0)
    if not screen_dc:
        raise RuntimeError("GetDC failed.")
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    if not memory_dc:
        user32.ReleaseDC(0, screen_dc)
        raise RuntimeError("CreateCompatibleDC failed.")
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    if not bitmap:
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(0, screen_dc)
        raise RuntimeError("CreateCompatibleBitmap failed.")
    old_object = gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not gdi32.BitBlt(memory_dc, 0, 0, width, height, screen_dc, left, top, SRCCOPY):
            raise RuntimeError("BitBlt failed.")
        return bitmap_to_image(memory_dc, bitmap, width, height)
    finally:
        if old_object:
            gdi32.SelectObject(memory_dc, old_object)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(0, screen_dc)


def bitmap_to_image(memory_dc: int, bitmap: int, width: int, height: int) -> Any:
    gdi32 = ctypes.windll.gdi32
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    buffer_size = width * height * 4
    buffer = ctypes.create_string_buffer(buffer_size)
    lines = gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(bmi), DIB_RGB_COLORS)
    if lines != height:
        raise RuntimeError(f"GetDIBits returned {lines} lines.")
    image = Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1)
    return image.copy()

