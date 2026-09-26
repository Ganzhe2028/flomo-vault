# flomo Local Vault

Unofficial macOS tool for exporting flomo desktop data from a consistent local snapshot. It publishes memo text, links, tags, history and optional media to a local vault. Optional Notion integrations reconcile a read-only mirror and archive memo text under a separate page tree.

This repository contains no personal memo data, Notion page IDs, or credentials. It is a separate public-source copy; it does not replace an existing local installation.

## Requirements

- macOS with `/Applications/flomo.app` and a signed-in desktop client.
- Python 3.11+; the scheduled Helper needs a macOS Python installation with a usable base executable.
- Enough disk space for your own media; `sync --media none` exports text without downloading media.

The parser depends on the current flomo desktop IndexedDB layout and may need updating when flomo changes it. This is not an official flomo export tool.

## Install and first sync

Double-click `install.command`, or run it from Terminal. Then:

```bash
flomo-vault doctor
flomo-vault sync --media none
flomo-vault status
```

The default output is `~/Documents/Flomo Vault/current`. To export a date range with images:

```bash
flomo-vault sync --since 2024-01-01 --media new --media-types image
```

The installer creates a local virtual environment and registers a command plus the optional Codex skill. It does not configure Notion or install the daily schedule automatically.

## Optional Notion configuration

Copy `config.example.json` to `~/.config/flomo-vault/config.json` and edit the dates and IDs for your own workspace. This file stays outside the repository. `daily_since` controls the local image/text range and read-only Notion comparison; `archive_since` controls the separate text-only archive. Empty Notion IDs disable those integrations.

For a read-only mirror, put its data source ID in `notion_data_source_id`, share that data source with a **Read content** connection, then run `flomo-vault notion setup`. The token is entered without echo and stored in macOS Keychain, not in config. The mirror must expose `Link` (URL) and `Created At` (date). Matching is exact: `Link.memo_id == local memo_uid`; titles and dates are never used as fallback IDs.

For the separate text archive, put the desired root page ID in `notion_text_root_page_id`, share only that page with a separate connection having **Read/Insert/Update content**, then run `flomo-vault notion-text setup`. The first sync initializes an empty local mapping. Memo pages are created beneath year/month/week pages. `notion-text bootstrap` is only for migrating an existing archive with an external mapping file.

```bash
flomo-vault daily --json
flomo-vault notion-text status --json
```

`daily` always publishes the local snapshot before Notion reconciliation. Missing or delayed Notion data does not roll back a successful local export. Exit codes: `0` complete or normal mirror lag, `2` local export exists but media/Notion needs attention, `1` local export failed and the previous `current` remains available.

## Optional daily schedule

`flomo-vault schedule install` registers a macOS LaunchAgent for 08:00 local time and checks for a missed run at login. When flomo is in the foreground, the scheduled run defers rather than interrupting writing. The installer creates a dedicated background Helper app; grant Full Disk Access to that Helper only if macOS requires it. `flomo-vault schedule status` inspects it; `flomo-vault schedule uninstall` removes it.

The scheduled Helper copies the base Python executable from the installation used by `install.command`. Manual `sync` does not depend on the Helper.

## Data and privacy

The vault contains the user's private memo content and must never be committed or shared as an issue attachment. The repository's `.gitignore` excludes generated mapping and data files, but an ignore rule is not a substitute for checking `git status` before publishing. Notion tokens remain in Keychain; signed media URLs and access tokens are not written to the vault. The Notion mirror is read-only, while the separate text archive writes only to the configured root page.

Run tests with `PYTHONPATH=src python3 -m unittest discover -s tests`.

## License

Apache-2.0. See [LICENSE](LICENSE).
