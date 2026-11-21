"""Calendar (CalDAV) and contact (CardDAV) export with an app password. Read-only."""

import base64
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from common import AuthError, safe_name, write_text

NS = {"D": "DAV:", "C": "urn:ietf:params:xml:ns:caldav", "CR": "urn:ietf:params:xml:ns:carddav"}

KINDS = {
    "calendars": {
        "base": "https://caldav.fastmail.com/",
        "home": "/dav/calendars/user/{user}/",
        "home_prop": "C:calendar-home-set",
        "collection_type": "{urn:ietf:params:xml:ns:caldav}calendar",
        "data_prop": "C:calendar-data",
        "ext": ".ics",
        "query": ('<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
                  "<D:prop><D:getetag/><C:calendar-data/></D:prop>"
                  '<C:filter><C:comp-filter name="VCALENDAR"/></C:filter></C:calendar-query>'),
        "what": "CalDAV",
    },
    "contacts": {
        "base": "https://carddav.fastmail.com/",
        "home": "/dav/addressbooks/user/{user}/",
        "home_prop": "CR:addressbook-home-set",
        "collection_type": "{urn:ietf:params:xml:ns:carddav}addressbook",
        "data_prop": "CR:address-data",
        "ext": ".vcf",
        "query": ('<CR:addressbook-query xmlns:D="DAV:" xmlns:CR="urn:ietf:params:xml:ns:carddav">'
                  "<D:prop><D:getetag/><CR:address-data/></D:prop><CR:filter/>"
                  "</CR:addressbook-query>"),
        "what": "CardDAV",
    },
}


def q(tag):
    prefix, local = tag.split(":")
    return "{%s}%s" % (NS[prefix], local)


class Dav:
    def __init__(self, user, password, what):
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.headers = {"Authorization": "Basic " + token, "User-Agent": "fastmail-backup"}
        self.what = what

    def request(self, method, url, body=None, depth=None):
        """Follows redirects manually (urllib won't for PROPFIND/REPORT)."""
        for _ in range(10):
            headers = dict(self.headers)
            data = None
            if body is not None:
                data = body.encode("utf-8")
                headers["Content-Type"] = 'application/xml; charset="utf-8"'
            if depth is not None:
                headers["Depth"] = str(depth)
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return resp.geturl(), resp.read()
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                    url = urllib.parse.urljoin(url, e.headers["Location"])
                    continue
                if e.code in (401, 403):
                    raise AuthError(f"{self.what} login failed ({e.code}). Check the user and that "
                                    f"the app password has {self.what} access.")
                raise
        raise RuntimeError("Too many redirects")

    def propfind(self, url, props, depth):
        body = ('<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav" '
                'xmlns:CR="urn:ietf:params:xml:ns:carddav"><D:prop>'
                + "".join(f"<{p}/>" for p in props) + "</D:prop></D:propfind>")
        final_url, data = self.request("PROPFIND", url, body, depth)
        return final_url, ET.fromstring(data)


def _first_href(tree, prop):
    el = tree.find(f".//{q(prop)}/{q('D:href')}")
    return el.text.strip() if el is not None and el.text else None


def _find_home(dav, kind, user, base):
    cfg = KINDS[kind]
    fallback = urllib.parse.urljoin(base, cfg["home"].format(user=urllib.parse.quote(user)))
    if "fastmail.com" in (urllib.parse.urlparse(base).hostname or ""):
        return fallback  # Fastmail's documented path
    try:  # generic discovery for other servers (used for testing)
        url, tree = dav.propfind(base, ["D:current-user-principal"], 0)
        principal = _first_href(tree, "D:current-user-principal")
        if principal:
            url, tree = dav.propfind(urllib.parse.urljoin(url, principal), [cfg["home_prop"]], 0)
            home = _first_href(tree, cfg["home_prop"])
            if home:
                return urllib.parse.urljoin(url, home)
    except urllib.error.HTTPError:
        pass
    return fallback


def _collections(dav, kind, home):
    cfg = KINDS[kind]
    url, tree = dav.propfind(home, ["D:resourcetype", "D:displayname"], 1)
    out = []
    for resp in tree.findall(q("D:response")):
        href = resp.findtext(q("D:href"))
        rtype = resp.find(f".//{q('D:resourcetype')}")
        if href is None or rtype is None or rtype.find(cfg["collection_type"]) is None:
            continue
        name = (resp.findtext(f".//{q('D:displayname')}") or "").strip()
        full = urllib.parse.urljoin(url, href.strip())
        ident = urllib.parse.unquote(full.rstrip("/").rsplit("/", 1)[-1])
        out.append((full, name or ident, ident))
    return out


def _items(dav, kind, coll_url, log):
    cfg = KINDS[kind]
    try:
        _, data = dav.request("REPORT", coll_url, cfg["query"], 1)
        tree = ET.fromstring(data)
        items = []
        for resp in tree.findall(q("D:response")):
            href = resp.findtext(q("D:href"))
            text = resp.findtext(f".//{q(cfg['data_prop'])}")
            if href and text:
                items.append((href.strip(), text))
        return items
    except urllib.error.HTTPError as e:
        log(f"    REPORT not supported ({e.code}), fetching items one by one")
    url, tree = dav.propfind(coll_url, ["D:getcontenttype"], 1)
    items = []
    for resp in tree.findall(q("D:response")):
        href = resp.findtext(q("D:href"))
        if href and not href.strip().endswith("/"):
            _, body = dav.request("GET", urllib.parse.urljoin(url, href.strip()))
            items.append((href.strip(), body.decode("utf-8", "replace")))
    return items


def merge_ics(texts, calname):
    """Combine many VCALENDAR objects into one, de-duplicating VTIMEZONEs."""
    timezones, comps = {}, []
    for text in texts:
        depth, current, kind = 0, None, None
        for line in text.replace("\r\n", "\n").split("\n"):
            u = line.upper()
            if u.startswith("BEGIN:"):
                depth += 1
                if depth == 2:
                    current, kind = [], u[6:].strip()
            if current is not None:
                current.append(line)
            if u.startswith("END:"):
                if depth == 2 and current is not None:
                    if kind == "VTIMEZONE":
                        tzid = next((l for l in current if l.upper().startswith("TZID")),
                                    str(len(timezones)))
                        timezones.setdefault(tzid, current)
                    else:
                        comps.append(current)
                    current = None
                depth -= 1
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//fastmail-backup//EN",
           f"X-WR-CALNAME:{calname}"]
    for block in list(timezones.values()) + comps:
        out.extend(block)
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


def export(kind, user, password, out_dir, log, base_url=None):
    """Export all calendars or address books. Returns item count."""
    cfg = KINDS[kind]
    dav = Dav(user, password, cfg["what"])
    home = _find_home(dav, kind, user, base_url or cfg["base"])
    collections = _collections(dav, kind, home)
    if not collections:
        log(f"  no {kind} found")
    os.makedirs(out_dir, exist_ok=True)

    used, total = set(), 0
    for url, name, ident in collections:
        base = safe_name(name)
        if base.lower() in used:
            base = safe_name(f"{name} ({ident})")
        used.add(base.lower())

        items = _items(dav, kind, url, log)
        log(f"  {name}: {len(items)} items")
        item_dir = os.path.join(out_dir, base)
        os.makedirs(item_dir, exist_ok=True)

        seen = set()
        for href, text in items:
            fname = safe_name(urllib.parse.unquote(href.rstrip("/").rsplit("/", 1)[-1]))
            if not fname.lower().endswith(cfg["ext"]):
                fname += cfg["ext"]
            while fname.lower() in seen:
                fname = "_" + fname
            seen.add(fname.lower())
            write_text(os.path.join(item_dir, fname), text)

        combined = os.path.join(out_dir, base + cfg["ext"])
        if kind == "calendars":
            write_text(combined, merge_ics([t for _, t in items], name))
        else:
            write_text(combined, "".join(t.strip() + "\n" for _, t in items))
        total += len(items)
    return total
