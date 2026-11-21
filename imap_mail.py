"""Mail export over IMAP (app password). Read-only: EXAMINE + BODY.PEEK."""

import imaplib
import os
import re

from common import AuthError, safe_name, write_bytes

HOST = "imap.fastmail.com"
PORT = 993
BATCH = 100

LIST_RE = re.compile(r'\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)')
UID_RE = re.compile(rb"UID (\d+)")


def decode_mutf7(s):
    """Decode IMAP modified UTF-7 folder names (non-ASCII names)."""
    out, i = [], 0
    while i < len(s):
        if s[i] == "&":
            j = s.index("-", i)
            if j == i + 1:
                out.append("&")
            else:
                b64 = s[i + 1:j].replace(",", "/")
                out.append(("+" + b64 + "-").encode("ascii").decode("utf-7"))
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _quote(name):
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _connect(user, password, host, port):
    imap = imaplib.IMAP4_SSL(host, port)
    try:
        imap.login(user, password)
    except imaplib.IMAP4.error as e:
        raise AuthError(f"IMAP login failed ({e}). Check the user and the mail app password.")
    return imap


def _folders(imap):
    typ, data = imap.list()
    if typ != "OK":
        raise RuntimeError("IMAP LIST failed")
    folders = []
    for raw in data:
        if raw is None:
            continue
        line = (raw[0].decode() + raw[1].decode()) if isinstance(raw, tuple) else raw.decode()
        m = LIST_RE.match(line)
        if not m:
            continue
        flags = m.group("flags").lower().split()
        delim = None if m.group("delim") == "NIL" else m.group("delim").strip('"')
        name = m.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        folders.append((name, delim, flags))
    return folders


def is_excluded(name, delim, flags, excludes):
    if "\\noselect" in flags or "\\nonexistent" in flags:
        return True
    if "\\trash" in flags and "trash" in excludes:
        return True
    pretty = decode_mutf7(name)
    top = pretty.split(delim)[0] if delim else pretty
    full = pretty.replace(delim, "/") if delim else pretty
    return top.lower() in excludes or full.lower() in excludes


def list_folders(user, password, excludes, host=HOST, port=PORT):
    excludes = {e.lower() for e in excludes}
    imap = _connect(user, password, host, port)
    try:
        return [(decode_mutf7(n), not is_excluded(n, d, f, excludes)) for n, d, f in _folders(imap)]
    finally:
        _logout(imap)


def _logout(imap):
    try:
        imap.logout()
    except Exception:
        pass


def _export_folder(imap, name, delim, out_root, log):
    typ, data = imap.select(_quote(name), readonly=True)
    if typ != "OK":
        log(f"  ! could not open {decode_mutf7(name)}: {data}")
        return 0
    _, data = imap.response("UIDVALIDITY")
    uidvalidity = data[0].decode() if data and data[0] else "0"

    parts = decode_mutf7(name).split(delim) if delim else [decode_mutf7(name)]
    folder_dir = os.path.join(out_root, *[safe_name(p) for p in parts])
    os.makedirs(folder_dir, exist_ok=True)

    typ, data = imap.uid("SEARCH", None, "ALL")
    uids = data[0].split() if typ == "OK" and data[0] else []
    todo = [u for u in uids
            if not os.path.exists(os.path.join(folder_dir, f"{uidvalidity}-{u.decode()}.eml"))]
    log(f"  {decode_mutf7(name)}: {len(todo)} messages")

    saved = 0
    for i in range(0, len(todo), BATCH):
        chunk = b",".join(todo[i:i + BATCH]).decode()
        typ, data = imap.uid("FETCH", chunk, "(UID BODY.PEEK[])")
        if typ != "OK":
            log(f"  ! fetch failed for a batch in {decode_mutf7(name)}")
            continue
        for item in data:
            if not isinstance(item, tuple):
                continue
            m = UID_RE.search(item[0])
            if m:
                write_bytes(os.path.join(folder_dir, f"{uidvalidity}-{m.group(1).decode()}.eml"),
                            item[1])
                saved += 1
        done = min(i + BATCH, len(todo))
        if len(todo) > BATCH * 10 and (done % (BATCH * 10) == 0 or done == len(todo)):
            log(f"    {done}/{len(todo)}")
    return saved


def export(user, password, out_dir, excludes, log, host=HOST, port=PORT):
    """Export every folder except excluded ones as .eml files. Returns message count."""
    excludes = {e.lower() for e in excludes}
    imap = _connect(user, password, host, port)
    try:
        total = 0
        for name, delim, flags in _folders(imap):
            if is_excluded(name, delim, flags, excludes):
                if "\\noselect" not in flags:
                    log(f"  skipping {decode_mutf7(name)}")
                continue
            total += _export_folder(imap, name, delim, out_dir, log)
        return total
    finally:
        _logout(imap)
