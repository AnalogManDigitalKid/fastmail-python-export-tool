#!/usr/bin/env python3
"""
fastmail-backup: export Fastmail mail, calendars, contacts and aliases.

Each run writes a full, dated snapshot:
    MailExport/<yyyy-MM-dd_HHmm>/       .eml files in your folder structure
    CalendarExport/<yyyy-MM-dd_HHmm>/   .ics per calendar + one per event
    ContactExport/<yyyy-MM-dd_HHmm>/    .vcf per address book + one per contact
    AliasExport/<yyyy-MM-dd_HHmm>/      identities.csv, masked-emails.csv
    logs/export-<yyyy-MM-dd_HHmm>.log

Everything is read-only on the Fastmail side. Configure in fastmail-backup.toml.
Requires Python 3.11+ (standard library only).

Usage:
    python fastmail_backup.py
    python fastmail_backup.py --only mail,contacts
    python fastmail_backup.py --list-folders
    python fastmail_backup.py --config path/to/other.toml
"""

import argparse
import datetime
import os
import sys
import traceback

if sys.version_info < (3, 11):
    sys.exit("fastmail-backup needs Python 3.11 or newer.")

import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dav        # noqa: E402
import imap_mail  # noqa: E402
import jmap       # noqa: E402
from common import AuthError  # noqa: E402

COMPONENTS = ("mail", "calendars", "contacts", "aliases")
OUT_DIRS = {"mail": "MailExport", "calendars": "CalendarExport",
            "contacts": "ContactExport", "aliases": "AliasExport"}
MODES = ("separate", "single", "api")
SEPARATE_KEYS = {"mail": "mail_app_password", "calendars": "caldav_app_password",
                 "contacts": "carddav_app_password"}


class ConfigError(Exception):
    pass


class Log:
    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8")

    def __call__(self, msg=""):
        print(msg, flush=True)
        self.f.write(msg + "\n")
        self.f.flush()


# ----------------------------------------------------------------- config ---

def load_config(path):
    if not os.path.exists(path):
        raise ConfigError(f"Config not found: {path}\n"
                          "Copy fastmail-backup.example.toml to fastmail-backup.toml and fill it in.")
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Config file has a syntax error: {e}")


def plan(cfg, only=None):
    """Work out, for each enabled component, how it's exported and with which secret.
    Returns a list of (component, method, secret). Raises ConfigError listing every problem."""
    auth = cfg.get("auth", {})
    mode = auth.get("mode", "separate")
    toggles = cfg.get("components", {})
    user = cfg.get("account", {}).get("user", "").strip()
    errors = []

    if mode not in MODES:
        raise ConfigError(f"auth.mode must be one of {', '.join(MODES)} (got '{mode}')")
    enabled = [c for c in COMPONENTS if toggles.get(c, True)]
    if only is not None:
        unknown = set(only) - set(COMPONENTS)
        if unknown:
            raise ConfigError(f"--only: unknown component(s): {', '.join(sorted(unknown))}")
        enabled = [c for c in COMPONENTS if c in only]
    if not enabled:
        raise ConfigError("No components enabled. Turn at least one on under [components].")

    def secret(key, comp):
        v = (auth.get(key) or "").strip()
        if not v:
            errors.append(f"{comp} is enabled but auth.{key} is empty")
        return v

    steps = []
    for c in enabled:
        if c == "aliases":
            steps.append((c, "jmap", secret("api_token", c)))
        elif mode == "api" and c in ("mail", "contacts"):
            steps.append((c, "jmap", secret("api_token", c)))
        elif mode == "api" and c == "calendars":
            pw = (auth.get("caldav_app_password") or auth.get("app_password") or "").strip()
            if not pw:
                errors.append("calendars can't use the API token (Fastmail's API has no calendar "
                              "scope). Set auth.caldav_app_password, or components.calendars = false")
            steps.append((c, "caldav", pw))
        else:
            method = {"mail": "imap", "calendars": "caldav", "contacts": "carddav"}[c]
            key = "app_password" if mode == "single" else SEPARATE_KEYS[c]
            steps.append((c, method, secret(key, c)))

    if any(m != "jmap" for _, m, _ in steps) and not user:
        errors.append("account.user is required for app-password exports")
    if errors:
        raise ConfigError("Config problems:\n  - " + "\n  - ".join(errors))
    return steps


def resolve(path):
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(HERE, path))


# ------------------------------------------------------------------- run ---

def run_step(cfg, comp, method, secret, out_dir, log):
    user = cfg.get("account", {}).get("user", "").strip()
    adv = cfg.get("advanced", {})
    excludes = cfg.get("mail", {}).get("exclude_folders", ["Trash"])

    if method == "imap":
        return imap_mail.export(user, secret, out_dir, excludes, log,
                                host=adv.get("imap_host", imap_mail.HOST),
                                port=int(adv.get("imap_port", imap_mail.PORT)))
    if method in ("caldav", "carddav"):
        kind = "calendars" if method == "caldav" else "contacts"
        return dav.export(kind, user, secret, out_dir, log,
                          base_url=adv.get(f"{method}_url"))
    j = jmap.Jmap(secret, adv.get("jmap_session_url", jmap.SESSION_URL))
    if comp == "mail":
        return jmap.export_mail(j, out_dir, excludes, log)
    if comp == "contacts":
        return jmap.export_contacts(j, out_dir, log)
    return jmap.export_aliases(j, out_dir, log)


def list_folders(cfg, steps):
    mail = next((s for s in steps if s[0] == "mail"), None)
    if not mail:
        raise ConfigError("--list-folders needs the mail component enabled")
    excludes = cfg.get("mail", {}).get("exclude_folders", ["Trash"])
    adv = cfg.get("advanced", {})
    if mail[1] == "imap":
        rows = imap_mail.list_folders(cfg["account"]["user"], mail[2], excludes,
                                      host=adv.get("imap_host", imap_mail.HOST),
                                      port=int(adv.get("imap_port", imap_mail.PORT)))
    else:
        rows = jmap.list_mailboxes(jmap.Jmap(mail[2], adv.get("jmap_session_url", jmap.SESSION_URL)),
                                   excludes)
    for name, keep in rows:
        print(f"{'KEEP' if keep else 'SKIP'}  {name}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Export Fastmail mail, calendars, contacts and aliases")
    ap.add_argument("--config", default=os.path.join(HERE, "fastmail-backup.toml"))
    ap.add_argument("--only", help="comma-separated components to run this time, "
                                   "e.g. mail,contacts (overrides [components])")
    ap.add_argument("--list-folders", action="store_true",
                    help="list mail folders and whether they'd be exported, then exit")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
        only = [s.strip().lower() for s in args.only.split(",")] if args.only else None
        steps = plan(cfg, only)
        if args.list_folders:
            list_folders(cfg, steps)
            return 0
    except (ConfigError, AuthError) as e:
        print(e, file=sys.stderr)
        return 2

    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    out_root = resolve(cfg.get("output_dir", "."))
    log_dir = resolve(cfg.get("log_dir", "logs"))
    os.makedirs(log_dir, exist_ok=True)
    log = Log(os.path.join(log_dir, f"export-{stamp}.log"))

    mode = cfg.get("auth", {}).get("mode", "separate")
    log(f"fastmail-backup run {stamp} (auth mode: {mode})")
    results = []
    for comp, method, secret in steps:
        out_dir = os.path.join(out_root, OUT_DIRS[comp], stamp)
        log(f"\n== {comp} via {method.upper()} -> {out_dir} ==")
        os.makedirs(out_dir, exist_ok=True)
        try:
            n = run_step(cfg, comp, method, secret, out_dir, log)
            log(f"  done: {n} items")
            results.append((comp, True))
        except (AuthError, jmap.JmapError) as e:
            log(f"  ERROR: {e}")
            results.append((comp, False))
        except Exception as e:  # keep going with the other components
            log(f"  ERROR: {type(e).__name__}: {e}")
            log("  " + traceback.format_exc().strip().replace("\n", "\n  "))
            results.append((comp, False))

    failed = [c for c, ok in results if not ok]
    log("")
    if failed:
        log(f"Finished with errors in: {', '.join(failed)}")
        return 1
    log(f"All exports finished: {', '.join(c for c, _ in results)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
