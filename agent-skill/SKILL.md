---
name: flomo-local-vault
description: Sync and read a local flomo vault on macOS; optionally reconcile a read-only Notion mirror and maintain a separate text-only archive.
compatibility: macos
---

# flomo Local Vault

Use the installed `flomo-vault` command. For a full configured daily sync, run `flomo-vault daily --json`. For text only, run `flomo-vault sync --media none --json`. For a bounded export, pass `--since YYYY-MM-DD` and optionally `--until YYYY-MM-DD`; for images only, pass `--media-types image`.

Never read a live IndexedDB directly. The command makes a consistent snapshot and preserves the previous published vault on failure. Scheduled runs defer if flomo is frontmost.

Read `~/Documents/Flomo Vault/current/manifest.json` first, then `memos.jsonl`. Exclude `is_deleted` memos unless requested. Use `memo_uid` as the stable identity. Local `links.jsonl` is canonical for directed `@` relationships.

If the user's config contains a Notion mirror data source, treat it as read-only. Match `Link.memo_id` to local `memo_uid`; never guess from titles or dates. The separate text archive has its own token and configured root page and may write year/month/week/memo pages there only. Never print tokens, signed URLs, or memo bodies in routine status messages.

Report the local memo count, changes, Notion matched/pending counts, and any relevant media failures. If Notion is not configured, say so without treating it as a failed local export.
