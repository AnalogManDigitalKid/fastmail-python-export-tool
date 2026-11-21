"""Mail, contacts and alias export over Fastmail's JMAP API (API token). Read-only."""

import csv
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from common import AuthError, safe_name, write_bytes, write_text

SESSION_URL = "https://api.fastmail.com/jmap/session"
CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
SUBMISSION = "urn:ietf:params:jmap:submission"
CONTACTS = "urn:ietf:params:jmap:contacts"
MASKED = "https://www.fastmail.com/dev/maskedemail"

SCOPE_NAMES = {MAIL: "Email", SUBMISSION: "Email submission",
               CONTACTS: "Contacts and contact groups", MASKED: "Masked Email"}
QUERY_LIMIT = 500
DOWNLOAD_WORKERS = 4


class JmapError(Exception):
    pass


class MissingScope(JmapError):
    pass


class Jmap:
    def __init__(self, token, session_url=SESSION_URL):
        self.headers = {"Authorization": f"Bearer {token}", "User-Agent": "fastmail-backup"}
        self.session = self._json(session_url)
        self.api_url = self.session["apiUrl"]
        self.download_url = self.session["downloadUrl"]
        core = self.session.get("capabilities", {}).get(CORE, {})
        self.max_get = max(1, min(int(core.get("maxObjectsInGet") or 500), 500))

    def _open(self, url, body=None):
        headers = dict(self.headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError(f"API token rejected ({e.code}). Check the token and its scopes.")
            raise

    def _json(self, url, body=None):
        return json.loads(self._open(url, body))

    def account(self, cap):
        acct = self.session.get("primaryAccounts", {}).get(cap)
        if not acct:
            raise MissingScope(f"the API token doesn't have the '{SCOPE_NAMES.get(cap, cap)}' scope")
        return acct

    def has(self, cap):
        return bool(self.session.get("primaryAccounts", {}).get(cap))

    def call(self, cap, method, args):
        body = {"using": [CORE, cap],
                "methodCalls": [[method, {"accountId": self.account(cap), **args}, "0"]]}
        name, result, _ = self._json(self.api_url, body)["methodResponses"][0]
        if name == "error":
            raise JmapError(f"{method} failed: {result.get('type')} {result.get('description', '')}".strip())
        return result

    def get_all(self, cap, type_, properties=None, query_filter=None):
        """Every object of a type: /query (paged) for ids, then /get in chunks.
        Falls back to /get with ids=null for types that have no /query."""
        try:
            ids, pos = [], 0
            while True:
                args = {"position": pos, "limit": QUERY_LIMIT, "calculateTotal": True}
                if query_filter:
                    args["filter"] = query_filter
                r = self.call(cap, f"{type_}/query", args)
                ids += r["ids"]
                pos += len(r["ids"])
                if not r["ids"] or pos >= r.get("total", pos):
                    break
        except JmapError as e:
            if "unknownMethod" not in str(e):
                raise
            args = {"ids": None}
            if properties:
                args["properties"] = properties
            return self.call(cap, f"{type_}/get", args)["list"]

        out = []
        for i in range(0, len(ids), self.max_get):
            args = {"ids": ids[i:i + self.max_get]}
            if properties:
                args["properties"] = properties
            out += self.call(cap, f"{type_}/get", args)["list"]
        return out

    def download(self, cap, blob_id, name="message.eml", type_="message/rfc822"):
        url = (self.download_url
               .replace("{accountId}", urllib.parse.quote(self.account(cap), safe=""))
               .replace("{blobId}", urllib.parse.quote(blob_id, safe=""))
               .replace("{name}", urllib.parse.quote(name, safe=""))
               .replace("{type}", urllib.parse.quote(type_, safe="")))
        return self._open(url)


# ------------------------------------------------------------------- mail ---

def _mailboxes(j):
    boxes = j.call(MAIL, "Mailbox/get", {"ids": None,
                                         "properties": ["id", "name", "parentId", "role",
                                                        "totalEmails"]})["list"]
    by_id = {b["id"]: b for b in boxes}
    for b in boxes:
        parts, cur, guard = [], b, 0
        while cur and guard < 50:
            parts.append(cur.get("name") or cur["id"])
            cur = by_id.get(cur.get("parentId"))
            guard += 1
        b["path"] = list(reversed(parts))
    return sorted(boxes, key=lambda b: [p.lower() for p in b["path"]])


def _box_excluded(box, excludes):
    if box.get("role") == "trash" and "trash" in excludes:
        return True
    return box["path"][0].lower() in excludes or "/".join(box["path"]).lower() in excludes


def list_mailboxes(j, excludes):
    excludes = {e.lower() for e in excludes}
    return [("/".join(b["path"]), not _box_excluded(b, excludes)) for b in _mailboxes(j)]


def export_mail(j, out_dir, excludes, log):
    """Export every mailbox except excluded ones as .eml files. Returns message count."""
    excludes = {e.lower() for e in excludes}
    total = 0
    with ThreadPoolExecutor(DOWNLOAD_WORKERS) as pool:
        for box in _mailboxes(j):
            label = "/".join(box["path"])
            if _box_excluded(box, excludes):
                log(f"  skipping {label}")
                continue
            emails = j.get_all(MAIL, "Email", properties=["id", "blobId"],
                               query_filter={"inMailbox": box["id"]})
            log(f"  {label}: {len(emails)} messages")
            folder = os.path.join(out_dir, *[safe_name(p) for p in box["path"]])
            os.makedirs(folder, exist_ok=True)

            def fetch(e, folder=folder):
                write_bytes(os.path.join(folder, safe_name(e["id"]) + ".eml"),
                            j.download(MAIL, e["blobId"]))

            for n, _ in enumerate(pool.map(fetch, emails), 1):
                if len(emails) > 1000 and (n % 1000 == 0 or n == len(emails)):
                    log(f"    {n}/{len(emails)}")
            total += len(emails)
    return total


# --------------------------------------------------------------- contacts ---

def _esc(s):
    return (str(s).replace("\\", "\\\\").replace("\n", "\\n")
            .replace(",", "\\,").replace(";", "\\;"))


def _types(obj, extra=()):
    t = list(extra)
    ctx = obj.get("contexts") or {}
    if ctx.get("work"):
        t.append("WORK")
    if ctx.get("private") or ctx.get("home"):
        t.append("HOME")
    if obj.get("pref") == 1:
        t.append("PREF")
    return (";TYPE=" + ",".join(t)) if t else ""


def _date(d):
    if isinstance(d, str):
        return d[:10]
    if isinstance(d, dict):
        if d.get("utc"):
            return d["utc"][:10]
        y, m, dd = d.get("year"), d.get("month"), d.get("day")
        if m and dd:
            return f"{y:04d}-{m:02d}-{dd:02d}" if y else f"--{m:02d}-{dd:02d}"
    return None


def card_to_vcard(c):
    """Convert a JSContact card (RFC 9553) to vCard 3.0. The raw JSON is saved
    alongside, so anything this conversion can't represent is not lost."""
    lines = ["BEGIN:VCARD", "VERSION:3.0"]
    name = c.get("name") or {}
    comps = {}
    for comp in name.get("components") or []:
        comps.setdefault(comp.get("kind"), []).append(comp.get("value", ""))
    j = lambda *kinds: " ".join(v for k in kinds for v in comps.get(k, []))
    given, middle, surname = j("given"), j("given2"), j("surname", "surname2")
    prefix, suffix = j("title"), j("credential", "generation")
    orgs = list((c.get("organizations") or {}).values())
    emails = list((c.get("emails") or {}).values())
    full = (name.get("full") or " ".join(x for x in (prefix, given, middle, surname, suffix) if x)
            or (orgs[0].get("name") if orgs else "") or (emails[0].get("address") if emails else "")
            or "(no name)")
    lines.append("FN:" + _esc(full))
    lines.append("N:" + ";".join(_esc(x) for x in (surname, given, middle, prefix, suffix)))

    if c.get("kind") == "group":
        lines.append("X-ADDRESSBOOKSERVER-KIND:group")
        for uid in c.get("members") or {}:
            lines.append("X-ADDRESSBOOKSERVER-MEMBER:" + (uid if uid.startswith("urn:") else "urn:uuid:" + uid))
    for n in (c.get("nicknames") or {}).values():
        lines.append("NICKNAME:" + _esc(n.get("name", "")))
    for o in orgs:
        units = [u.get("name", "") for u in o.get("units") or []]
        lines.append("ORG:" + ";".join(_esc(x) for x in [o.get("name", "")] + units))
    for t in (c.get("titles") or {}).values():
        lines.append(("ROLE:" if t.get("kind") == "role" else "TITLE:") + _esc(t.get("name", "")))
    for e in emails:
        if e.get("address"):
            lines.append("EMAIL" + _types(e, ["INTERNET"]) + ":" + e["address"])
    feature_map = {"mobile": "CELL", "voice": "VOICE", "fax": "FAX", "pager": "PAGER",
                   "text": "TEXT", "video": "VIDEO"}
    for p in (c.get("phones") or {}).values():
        feats = [feature_map[f] for f in (p.get("features") or {}) if f in feature_map]
        if p.get("number"):
            lines.append("TEL" + _types(p, feats) + ":" + p["number"])

    street_kinds = {"number", "name", "block", "building", "floor", "apartment", "room",
                    "extension", "direction", "landmark", "postOfficeBox", "subdistrict"}
    for a in (c.get("addresses") or {}).values():
        if a.get("components"):
            ac = {}
            street = []
            for comp in a["components"]:
                k, v = comp.get("kind"), comp.get("value", "")
                if k in street_kinds:
                    street.append(v)
                elif k in ("locality", "region", "postcode", "country", "district"):
                    ac.setdefault(k, v)
            street = " ".join(s for s in street if s)
        else:  # older JSContact drafts
            ac = a
            street = a.get("street") or ""
        fields = ["", "", street, ac.get("locality", ""), ac.get("region", ""),
                  ac.get("postcode", ""), ac.get("country", "")]
        if any(fields) or a.get("full"):
            lines.append("ADR" + _types(a) + ":" + ";".join(_esc(x) for x in fields))
            if a.get("full"):
                lines.append("LABEL" + _types(a) + ":" + _esc(a["full"]))
    for link in list((c.get("links") or {}).values()) + list((c.get("onlineServices") or {}).values()):
        if link.get("uri"):
            lines.append("URL:" + link["uri"])
    for ann in (c.get("anniversaries") or {}).values():
        d = _date(ann.get("date"))
        if d:
            lines.append(("BDAY:" if ann.get("kind") == "birth" else "X-ANNIVERSARY:") + d)
    notes = [n.get("note", "") for n in (c.get("notes") or {}).values() if n.get("note")]
    if notes:
        lines.append("NOTE:" + _esc("\n\n".join(notes)))
    if c.get("uid"):
        lines.append("UID:" + c["uid"])
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


def export_contacts(j, out_dir, log):
    """Export address books as .vcf (converted) plus the original JSContact JSON."""
    books = j.call(CONTACTS, "AddressBook/get", {"ids": None})["list"]
    cards = j.get_all(CONTACTS, "ContactCard")
    os.makedirs(out_dir, exist_ok=True)

    groups = {b["id"]: [] for b in books}
    unfiled = []
    for card in cards:
        ids = [i for i, on in (card.get("addressBookIds") or {}).items() if on and i in groups]
        for i in ids:
            groups[i].append(card)
        if not ids:
            unfiled.append(card)

    entries = [(b.get("name") or b["id"], groups[b["id"]]) for b in books]
    if unfiled:
        entries.append(("Unfiled", unfiled))

    used = set()
    for name, items in entries:
        base = safe_name(name)
        while base.lower() in used:
            base = "_" + base
        used.add(base.lower())
        log(f"  {name}: {len(items)} contacts")
        item_dir = os.path.join(out_dir, base)
        os.makedirs(item_dir, exist_ok=True)
        seen, vcards = set(), []
        for card in items:
            v = card_to_vcard(card)
            vcards.append(v)
            fname = safe_name(card.get("uid", card["id"]).replace("urn:uuid:", "")) + ".vcf"
            while fname.lower() in seen:
                fname = "_" + fname
            seen.add(fname.lower())
            write_text(os.path.join(item_dir, fname), v)
        write_text(os.path.join(out_dir, base + ".vcf"), "".join(vcards))
        write_bytes(os.path.join(out_dir, base + ".jscontact.json"),
                    json.dumps(items, indent=2, ensure_ascii=False).encode("utf-8"))
    return len(cards)


# ---------------------------------------------------------------- aliases ---

def _addrs(addrs):
    return "; ".join(f'{a.get("name") or ""} <{a.get("email")}>'.strip() for a in addrs or [])


def _csv(path, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    write_bytes(path, buf.getvalue().encode("utf-8-sig"))  # utf-8-sig so Excel reads it right


def export_aliases(j, out_dir, log):
    """identities.csv (sending addresses) and masked-emails.csv. Returns row count."""
    os.makedirs(out_dir, exist_ok=True)
    count = 0
    if j.has(SUBMISSION):
        ids = sorted(j.call(SUBMISSION, "Identity/get", {"ids": None})["list"],
                     key=lambda i: i.get("email", "").lower())
        _csv(os.path.join(out_dir, "identities.csv"),
             ["email", "name", "reply_to", "bcc", "can_delete"],
             [[i.get("email", ""), i.get("name", ""), _addrs(i.get("replyTo")),
               _addrs(i.get("bcc")), i.get("mayDelete", "")] for i in ids])
        log(f"  identities: {len(ids)}")
        count += len(ids)
    else:
        log("  ! identities skipped: token lacks the 'Email' + 'Email submission' scopes")

    if j.has(MASKED):
        masked = sorted(j.call(MASKED, "MaskedEmail/get", {"ids": None})["list"],
                        key=lambda m: (m.get("state", ""), m.get("forDomain") or "", m.get("email", "")))
        _csv(os.path.join(out_dir, "masked-emails.csv"),
             ["email", "state", "for_domain", "description", "created_at", "last_message_at",
              "created_by"],
             [[m.get("email", ""), m.get("state", ""), m.get("forDomain") or "",
               m.get("description") or "", m.get("createdAt") or "",
               m.get("lastMessageAt") or "", m.get("createdBy") or ""] for m in masked])
        log(f"  masked emails: {len(masked)}")
        count += len(masked)
    else:
        log("  masked emails skipped: token has no 'Masked Email' scope")

    if not j.has(SUBMISSION) and not j.has(MASKED):
        raise MissingScope("the API token has neither 'Email submission' nor 'Masked Email' scope")
    return count
