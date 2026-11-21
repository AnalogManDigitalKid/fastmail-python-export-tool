# fastmail-backup

Back up your Fastmail account (mail, calendars, contacts and aliases) to local files, with a full dated snapshot on every run. Read-only on the Fastmail side, standard-library Python, works on Windows, macOS and Linux.

> [!CAUTION]
> **Keep this entire folder inside encrypted storage, such as a [VeraCrypt](https://veracrypt.io/) container.**
>
> The config file holds your **app passwords and/or API token in plain text**, and the exports are **your entire mailbox, calendar and address book** in readable form. Anyone who can read this folder can read your email.
>
> - Mount the container, run the backup, then dismount it.
> - Never put the config or exports on unencrypted disks, in cloud sync folders (OneDrive, Dropbox, Google Drive, iCloud) or in git.
> - Use a strong, unique password for the container, and keep it somewhere you can reach even if you lose access to your email.

## What it exports

| Component | Output | Format |
| --- | --- | --- |
| **Mail** | `MailExport/<date>/` | One `.eml` per message, in your folder structure. Opens in any mail client. |
| **Calendars** | `CalendarExport/<date>/` | One combined `.ics` per calendar (ready to import anywhere) plus one `.ics` per event. |
| **Contacts** | `ContactExport/<date>/` | One combined `.vcf` per address book plus one `.vcf` per contact. |
| **Aliases** | `AliasExport/<date>/` | `identities.csv` (every address you can send from) and `masked-emails.csv`. |

Each run also writes `logs/export-<date>.log`.

- **Read-only:** mail folders are opened read-only, so nothing is changed on the server, not even read/unread status.
- **Toggles:** each component can be switched on or off.
- **Exclusions:** Trash is skipped by default; you can exclude any other folder.
- **Resilient:** if one component fails, the rest still run, and the summary lists what failed.

## Requirements

- [Python 3.11+](https://www.python.org/downloads/). No packages to install.
- A Fastmail plan with app password / API access (the Basic plan doesn't include IMAP, CalDAV or CardDAV).

On Windows: `winget install Python.Python.3.12`

## Setup

1. Create and mount an encrypted container (see the caution above).
2. Copy the repo's files into it:
   ```
   fastmail_backup.py   imap_mail.py   dav.py   jmap.py   common.py
   run-backup.bat       fastmail-backup.example.toml
   ```
3. Copy `fastmail-backup.example.toml` to `fastmail-backup.toml`.
4. Pick an [authentication mode](#authentication-modes), create the [credentials](#creating-credentials) it needs, and fill them into `fastmail-backup.toml`.
5. Check it can log in and see your folders:
   ```
   python fastmail_backup.py --list-folders
   ```
6. Run the backup (see [Usage](#usage)).

## Authentication modes

Set `mode` under `[auth]`. Every mode uses an API token for aliases, since that's the only way Fastmail exposes them.

| | `separate` (default) | `single` | `api` |
| --- | --- | --- | --- |
| **Mail** | IMAP, `mail_app_password` | IMAP, `app_password` | API, `api_token` |
| **Calendars** | CalDAV, `caldav_app_password` | CalDAV, `app_password` | CalDAV, `caldav_app_password` or `app_password` * |
| **Contacts** | CardDAV, `carddav_app_password` | CardDAV, `app_password` | API, `api_token` |
| **Aliases** | API, `api_token` | API, `api_token` | API, `api_token` |
| **Credentials** | 3 app passwords + 1 token | 1 app password + 1 token | 1 token (+ 1 app password for calendars) |

\* Fastmail's API has no calendar scope, so calendars always need an app password with CalDAV access. In `api` mode, either add one or set `calendars = false`.

**Which should I use?**

- **`separate`**: least privilege. Each credential can only reach one thing, so a leaked password exposes less, and you can revoke one without touching the others. Recommended.
- **`single`**: fewer passwords to manage. One app password reaches mail, calendars and contacts.
- **`api`**: the fewest credentials. Two differences to be aware of:
  - Mail files are named by Fastmail's message ID rather than IMAP UID. The contents are identical.
  - Contacts come from the API in JSContact format and are **converted** to vCard. The original JSON is saved next to each `.vcf` (`<book>.jscontact.json`), so nothing is lost, but the `.vcf` from CardDAV is a closer match to what Fastmail stores. If exact vCards matter to you, use `separate` or `single`.

## Creating credentials

### App passwords

Fastmail > **Settings > Privacy & Security > Manage app passwords and access > New app password**. Give each a recognisable name (e.g. "Backup - mail"), choose its access, and click **Generate password**.

| Mode | Create | Access to choose |
| --- | --- | --- |
| `separate` | `mail_app_password` | **Mail** (IMAP/POP/SMTP) |
| | `caldav_app_password` | **Calendars (CalDAV)** |
| | `carddav_app_password` | **Contacts (CardDAV)** |
| `single` | `app_password` | **Mail, Contacts & Calendars** |
| `api` (calendars) | `caldav_app_password` | **Calendars (CalDAV)** |

### API token

Fastmail > **Settings > Privacy & Security > API tokens > New API token**.

- **Protocol:** JMAP
- **Read-only access:** **on**. The script never needs to change anything.
- **Scopes:**

| Mode | Email | Email submission | Contacts and contact groups | Masked Email |
| --- | :---: | :---: | :---: | :---: |
| `separate` / `single` (aliases only) | ✅ | ✅ | | ✅ |
| `api` | ✅ | ✅ | ✅ | ✅ |

Email submission is what lists your sending addresses (aliases), and Fastmail requires Email to grant it. Masked Email is optional: leave it off if you don't use masked addresses, and the script will skip that file.

## Configuration

All options are documented in [`fastmail-backup.example.toml`](fastmail-backup.example.toml). The main ones:

```toml
[account]
user = 'you@yourdomain.com'

[components]          # turn exports on/off
mail      = true
calendars = true
contacts  = true
aliases   = true

[mail]
exclude_folders = ['Trash', 'Spam']   # top-level names or full paths like 'Archive/2019'

[auth]
mode = 'separate'     # 'separate' | 'single' | 'api'
mail_app_password    = '...'
caldav_app_password  = '...'
carddav_app_password = '...'
api_token            = '...'
```

The script checks the whole config before starting and lists every problem at once, for example a component that's enabled but missing its credential.

## Usage

**Windows:** double-click `run-backup.bat`.

**Any OS:**

```bash
python fastmail_backup.py                      # everything enabled in the config
python fastmail_backup.py --only mail          # just mail, this run only
python fastmail_backup.py --only contacts,aliases
python fastmail_backup.py --list-folders       # show which mail folders would be exported
python fastmail_backup.py --config other.toml  # use a different config file
```

Exit codes: `0` success, `1` one or more components failed, `2` config problem.

## Output

```
MailExport/2026-11-06_1230/
    INBOX/...eml
    Archive/2024/...eml
CalendarExport/2026-11-06_1230/
    Personal.ics          <- whole calendar, import this
    Personal/<event>.ics
ContactExport/2026-11-06_1230/
    Personal.vcf          <- whole address book, import this
    Personal/<contact>.vcf
    Personal.jscontact.json   (api mode only: original data)
AliasExport/2026-11-06_1230/
    identities.csv
    masked-emails.csv
logs/export-2026-11-06_1230.log
```

Every run is a complete snapshot, so the container grows with each one. Delete old snapshots now and then.

## Restoring

- **Mail:** open `.eml` files in any mail client (Thunderbird, Outlook, Apple Mail), or drag them into a folder of an IMAP account to upload them back to Fastmail or elsewhere.
- **Calendars:** import the combined `<calendar>.ics` in Fastmail (Settings > Calendars > Import), Google Calendar, Outlook, etc.
- **Contacts:** import the combined `<book>.vcf` in Fastmail (Contacts > Import), Google Contacts, Outlook, etc.
- **Aliases:** the CSVs are a reference list for recreating aliases. Fastmail has no bulk alias import.

Test a restore now and then so you know the backups actually work.

## Not covered

- **Filters & rules.** Fastmail doesn't expose them to IMAP, DAV or the API. Export them manually from **Settings > Filters & Rules > Export**, which gives a `.json` file you can import back later. A `RulesExport/` folder is already in `.gitignore` for this.
- **Fastmail Files** (WebDAV storage), **settings**, and **Trash** (excluded by default; remove it from `exclude_folders` to include it).

## Labels

If your account uses labels instead of folders, a message with several labels appears in each label's folder. Those are copies of the same message, not missing or duplicated mail.

## Security notes

- Everything is read-only: IMAP folders are opened with `EXAMINE` and fetched with `BODY.PEEK`, DAV only uses `PROPFIND`/`REPORT`/`GET`, and the API token should be read-only.
- Credentials are only sent to Fastmail over TLS.
- Prefer `separate` mode and read-only tokens, and revoke credentials you stop using (each one shows its last-used time in Fastmail's settings).

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `Config not found` | Copy `fastmail-backup.example.toml` to `fastmail-backup.toml`. |
| `... is enabled but auth.X is empty` | Fill in that credential, or turn the component off under `[components]`. |
| `IMAP login failed` | Check `user` and that the app password has Mail access. |
| `CalDAV/CardDAV login failed (401)` | Check the app password has Calendars / Contacts access. |
| `API token rejected` | Check the token, or that it hasn't been deleted. |
| `token doesn't have the 'X' scope` | Create a new token with the scopes in the table above. |
| A folder you expected is missing | Run `--list-folders` and check `exclude_folders`. |
| `needs Python 3.11 or newer` | Install a newer Python (`winget install Python.Python.3.12`). |
