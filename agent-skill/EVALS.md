# Lightweight trigger checks

1. “同步一下 flomo，附件也补齐。”
   - Good: runs `sync --media new`, then reports changes and gaps.
   - Failure: only gives manual instructions, or reads the live database directly.

2. “看看我过去三个月在 flomo 里关于运动的想法。”
   - Good: when MCP is configured, calls `get_vault_status` then `search_memos` with dates and topic; otherwise reads the manifest and JSONL. Excludes deleted notes. Syncs if the user asks for fresh data.
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

10. “我上周三那条关于游泳的 memo 还 @ 过别的吗？”
    - Good: when MCP is configured, calls `search_memos` to locate the memo and `get_memo_context` to inspect outgoing, incoming, and bidirectional links.
    - Failure: answers from memory or misses incoming links.

11. “我 flomo 里这个月写了多少条，主要是什么 tag？”
    - Good: calls `get_vault_status` to check the snapshot selection, then `get_stats` and `list_tags`; explains if the snapshot covers only a bounded range.
    - Failure: counts JSONL rows manually when MCP is configured, or calls a bounded snapshot an all-time total.

12. “把这条 memo 帮我改了。”
    - Good: explains that the MCP server is read-only and does not provide an edit tool.
    - Failure: attempts to modify the vault files through MCP or directly.
