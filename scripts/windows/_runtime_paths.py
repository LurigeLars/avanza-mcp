"""Windows runtime path helpers that avoid trusting mutable environment paths."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import shutil
from pathlib import Path

_CSIDL_LOCAL_APPDATA = 0x001C
_SHGFP_TYPE_CURRENT = 0
_MAX_PATH = 260


def local_appdata_dir() -> Path:
    """Return the Windows Local AppData directory via the shell API."""
    if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
        raise RuntimeError("Windows Local AppData is only available on Windows")

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.SHGetFolderPathW.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
    ]
    shell32.SHGetFolderPathW.restype = ctypes.c_long

    buffer = ctypes.create_unicode_buffer(_MAX_PATH)
    result = shell32.SHGetFolderPathW(
        None, _CSIDL_LOCAL_APPDATA, None, _SHGFP_TYPE_CURRENT, buffer
    )
    if result != 0:
        raise OSError(result, "SHGetFolderPathW(CSIDL_LOCAL_APPDATA) failed")

    path = Path(buffer.value)
    if not path.is_absolute():
        raise RuntimeError("Windows Local AppData path is not absolute")
    return path


def find_node_executable() -> Path:
    """Resolve node.exe without accepting a command path from argv."""
    candidates = [
        Path(r"C:\Program Files\nodejs\node.exe"),
        Path(r"C:\Program Files (x86)\nodejs\node.exe"),
    ]
    discovered = shutil.which("node.exe") or shutil.which("node")
    if discovered:
        candidates.append(Path(discovered))

    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and resolved.name.lower() in {"node.exe", "node"}:
            return resolved

    raise FileNotFoundError("node executable was not found")
