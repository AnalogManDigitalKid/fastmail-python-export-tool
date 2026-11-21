"""Shared helpers for fastmail-backup."""

import os
import re

BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED = {"con", "prn", "aux", "nul",
                    *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_name(s, maxlen=120):
    """Make a string safe to use as a single file or folder name on Windows/macOS/Linux."""
    s = BAD_CHARS.sub("_", s or "").strip().rstrip(". ")
    if s.lower() in WINDOWS_RESERVED:
        s = "_" + s
    return s[:maxlen] or "_"


def write_bytes(path, data):
    """Write atomically so an interrupted run never leaves half-written files."""
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def write_text(path, text):
    """Write text with CRLF line endings (what .ics/.vcf files expect)."""
    text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    write_bytes(path, text.encode("utf-8"))


class AuthError(Exception):
    """Credentials were rejected. Message says which credential to check."""
