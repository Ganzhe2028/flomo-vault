# Lightweight trigger checks

1. “同步一下 flomo，附件也补齐。”
   - Good: runs `sync --media new`, then reports changes and gaps.
   - Failure: only gives manual instructions, or reads the live database directly.

2. “看看我过去三个月在 flomo 里关于运动的想法。”
   - Good: syncs first, reads `current/memos.jsonl`, filters the period and topic, excludes deleted notes.
   - Failure: asks the user to export one by one or ignores the manifest status.

3. “今天只要快速更新文字，不管图片。”
   - Good: runs `sync --media none` and clearly says this was a text/index update.
   - Failure: starts a full attachment download.

4. “帮我整理 Downloads 文件夹。”
   - Good: this skill should not trigger.
   - Failure: mentions or syncs flomo without relevance.

5. “Notion 的 Flomo Database 里这条 memo @ 了谁？”
   - Good: reads Notion without modifying it, extracts `memo_id` from `Link`, joins to local `flomo_uid`, and answers from `outgoing_memo_uids`.
   - Failure: uses the Notion page UUID as memo identity or invents Notion relations.

6. “给自动同步的 Notion Flomo 模板补一个 UID 字段。”
   - Good: respects the stored read-only constraint and explains that UID joining happens locally through `Link.memo_id`.
   - Failure: updates the Notion schema or rows.

7. “同步并校对 flomo。”
   - Good: runs `daily --json`, reports local memo/image outcome and Notion matched/pending counts, and does not modify Notion.
   - Failure: runs only local `sync`, asks the model to guess matches, or requires a manual export.

8. “Notion Token 过期了，但现在仍要校对。”
   - Good: uses one read-only connector query, sends normalized rows to `reconcile --stdin`, and lets the deterministic program decide all statuses.
   - Failure: writes Notion, exposes credentials, or performs fuzzy matching by title/date.

9. “现在是定时任务触发，但我正在 flomo 里写东西。”
   - Good: does not quit flomo, records a foreground deferral, and lets the 10-minute LaunchAgent interval retry later.
   - Failure: interrupts the foreground app, copies the live database, or marks the run complete.
