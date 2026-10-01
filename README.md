# 让 flomo 里的想法，随时找得回来

「上周写的游泳笔记里，我还提到了谁？」 「这个月我都在想什么？」

如果你习惯往 flomo 里随手记，这些问题应该不用靠翻半天时间线来回答。flomo Local Vault 会从 Mac 上的 flomo 桌面端制作一致的本地快照，把笔记、标签、引用关系、历史版本和可选的附件整理进一个资料库。接上只读 MCP server 后，Claude Code、Codex 等支持 MCP 的 agent 就能直接搜索笔记、顺着 @ 关系往下读，也能查看本地附件的路径。

资料库按次保存快照，新的同步发布后，MCP 会自动读到最新版。你也可以只导出文字，先把找笔记这件事跑通。

这是非官方的 macOS 工具。仓库里没有个人笔记、Notion 页面 ID 或凭据；它是一份独立的公开源码副本，不会替换你已有的本地安装。

## 你能拿它做什么

| 想做的事 | 对应能力 |
| --- | --- |
| 找回某个想法 | 按关键词、日期和标签搜索笔记；多个关键词同时匹配。 |
| 接着一条笔记往下读 | 查看它引用了谁、谁引用了它，以及双向引用；最多展开两层。 |
| 看看最近在写什么 | 浏览笔记、常用标签、月份和一天中不同时段的记录分布。 |
| 回到原始内容 | 查看笔记全文、flomo 链接、附件状态和本地文件路径。 |
| 少操心同步 | 可选的每日定时任务会更新本地资料库；Notion 功能也可以单独配置。 |

MCP 只负责读已经发布的资料库，不会替你修改笔记，也不会悄悄触发同步。

## 开始使用

需要一台装有 `/Applications/flomo.app`、且已登录 flomo 桌面端的 Mac，以及 Python 3.11 或更新版本。附件会占用额外磁盘空间；只想先试文字，可以用 `--media none`。定时任务使用的 Helper 还需要一套可正常运行的 macOS Python 安装。

双击 `install.command`，或者在终端运行它。安装后先检查环境，再做一次文字同步：

```bash
flomo-vault doctor
flomo-vault sync --media none
flomo-vault status
```

默认资料库在 `~/Documents/Flomo Vault/`，最新快照通过 `current` 访问。只想导出指定日期之后的笔记，并下载图片，可以这样运行：

```bash
flomo-vault sync --since 2024-01-01 --media new --media-types image
```

安装脚本会创建项目自己的虚拟环境，注册 `flomo-vault` 和 `flomo-vault-mcp` 命令，并链接可选的 agent skill。Notion 和每日定时任务需要你另行启用。

flomo Local Vault 依赖桌面端当前的 IndexedDB 数据结构。flomo 更新后，如果底层格式变了，解析器也可能需要跟着调整。

## 接入 Claude Code 或 Codex

`flomo-vault-mcp` 是一个本地运行的 stdio MCP server。它读取 `current` 指向的快照；你下次同步发布新快照后，下一次工具调用就能读到新数据，不用重启。第一次接入前，先运行上面的 `flomo-vault sync --media none`。

`install.command` 会安装 MCP SDK，并把命令链接到 `~/.local/bin`。如果你通过 `pip install .` 安装仓库，也会得到 `flomo-vault-mcp` 入口。先用 `command -v flomo-vault-mcp` 找到命令的绝对路径；如果终端找不到，就使用项目目录下的 `.venv/bin/flomo-vault-mcp`。MCP 客户端启动时的 `PATH` 可能和终端不同。

### Claude Code

把下面的命令路径换成你查到的绝对路径：

```bash
claude mcp add --scope user --transport stdio flomo-vault -- /absolute/path/to/flomo-vault-mcp
claude mcp get flomo-vault
```

如果同步时用了自定义位置，比如 `flomo-vault sync --vault /another/path`，请直接用下面这条添加命令，指向同一个资料库根目录：

```bash
claude mcp add --scope user --env FLOMO_VAULT_ROOT=/another/path --transport stdio flomo-vault -- /absolute/path/to/flomo-vault-mcp
```

### Codex

在 `~/.codex/config.toml` 中加入以下配置，并替换命令路径：

```toml
[mcp_servers.flomo-vault]
command = "/absolute/path/to/flomo-vault-mcp"
```

使用自定义资料库时，再加上这段：

```toml
[mcp_servers.flomo-vault.env]
FLOMO_VAULT_ROOT = "/another/path"
```

`FLOMO_VAULT_ROOT` 指向包含 `current`、`snapshots` 和 `media` 的目录。没设置时，MCP 默认读取 `~/Documents/Flomo Vault/`。

### 七个只读工具

| 工具 | 用来做什么 |
| --- | --- |
| `get_vault_status` | 先看当前快照的时间、范围、同步状态和数量。 |
| `search_memos` | 搜索笔记正文，可按日期和标签筛选。 |
| `get_memo` | 读取单条笔记全文、附件信息与转写文本。 |
| `get_memo_context` | 沿着笔记之间的引用关系读，最多两层。 |
| `list_memos` | 按时间浏览笔记。 |
| `list_tags` | 看各标签下有多少条未删除笔记。 |
| `get_stats` | 看月份、时段、标签、链接和附件统计。 |

搜索、浏览和读取笔记时，已删除的笔记默认不会出现；明确传入 `include_deleted=true` 才会返回。`get_memo_context` 的这个开关只对中心笔记生效，邻居始终排除已删除笔记。`list_tags` 和 `get_stats` 也始终只统计未删除笔记。

统计描述的是**当前快照**。如果你同步时限定了日期或附件类型，先看 `get_vault_status` 返回的 `selection`，再判断这些数字覆盖了哪些内容。附件的 `local_path` 会解析到资料库的 `media` 目录下；要确认文件是否已下载，还需查看 `download_status`。

## 把这些问题直接丢给 agent

接好 MCP 后，你可以像平时聊天一样提问。下面的笔记内容都是举例，换成你自己写过的词就行。

1. **先看看资料库新不新。**「我现在读到的是哪次同步？覆盖了哪些日期？附件下载齐了吗？」agent 会调用 `get_vault_status`，看到快照时间、同步状态和 `selection`。准备做月度回顾时，先问这一句很值：它能提醒你当前资料库是否只包含某个时间段。
2. **从一个模糊印象捞回笔记。**「找找同时提到‘游泳’和‘肩膀’的笔记，只看去年、标签是‘运动’的。」`search_memos` 在正文里做不区分大小写的文字搜索；用空格隔开的词必须同时出现，还能按创建日期和标签筛选。结果按新到旧排列。
3. **翻翻最近写过什么。**「按时间倒序给我最近 20 条笔记，再看下一页。」`list_memos` 适合随手浏览，也能只看某个日期范围或标签，或改成从旧到新。搜索和列表默认每页 20 条，单页最多 100 条；返回值会告诉你总数和后面还有没有。
4. **把一条笔记读完整。**「打开刚才找到的那条，正文、原始 flomo 链接和附件都给我看看。」`get_memo` 返回完整笔记，还会带上附件名称、类型、下载状态、本地路径和已有的音频转写。只有路径还不等于文件已经下载，记得看 `download_status`。
5. **顺着 @ 找到一串想法。**「这条笔记 @ 过谁？谁又 @ 过它？有没有互相引用？再往外追一层。」`get_memo_context` 会分开列出向外、向内和双向引用；`depth=2` 可以再看一层。邻居先给 500 字以内的摘要，想读全文再用 `get_memo` 打开。关系太多时最多返回 50 个邻居，并标出截断。
6. **看看自己最常写什么。**「我最常用哪些标签？每个标签下有多少条笔记？」`list_tags` 按未删除笔记统计标签，并按数量排序。想找一个很久没回头看的主题，这里是个好入口。
7. **给这个月做个小复盘。**「这个月写了几条？我通常几点记？最常用的标签、笔记间的链接和附件占比呢？」`get_stats` 给出最近 24 个月的逐月数量、一天 24 小时的分布、前 20 个标签、链接数量和有附件的笔记占比。它统计的是当前快照里的未删除笔记。

单个工具能查到一块信息，连起来用更有意思：

- **把零散想法串成线。**「找到我关于跑步的笔记，挑出最早那条，再看看它后来被哪些笔记引用。」agent 可以先用 `search_memos` 定位，再用 `get_memo_context` 追引用，最后用 `get_memo` 读关键笔记全文。
- **做一份有出处的回顾。**「整理我这个月关于阅读的记录，每个结论都给出对应的 flomo 链接。」先用 `get_vault_status` 确认范围，再用 `list_memos`、`search_memos` 和 `get_memo` 取原文；整理和写作由你使用的 agent 完成。
- **找回某条旧笔记的附件。**「那条讲录音灵感的笔记，附件还在本地吗？有没有转写？」先搜索，再用 `get_memo` 查看 `download_status`、`local_path` 和 `transcript`。
- **查找已删除的记录。**「把那条删掉的游泳笔记也找出来，但别把其他已删除笔记混进统计。」搜索或读取时显式设置 `include_deleted=true`；`list_tags` 和 `get_stats` 仍只统计未删除笔记。它能帮你读到快照中保留的记录，不能替你恢复到 flomo。

## 想接 Notion？按需开启

本地资料库和 MCP 不依赖 Notion。想使用 Notion 功能时，先把 `config.example.json` 复制到 `~/.config/flomo-vault/config.json`，填入自己的日期与 ID。配置文件留在仓库外；留空的 Notion ID 会关闭对应功能。`daily_since` 控制本地图片/文字同步范围及只读校对范围，`archive_since` 控制独立的纯文字归档范围。

**只读镜像校对：** 把 Notion data source ID 填入 `notion_data_source_id`，给连接授予 **Read content** 权限，然后运行 `flomo-vault notion setup`。Token 会在不回显输入后存入 macOS Keychain，不写进配置文件。镜像需要有 `Link`（URL）和 `Created At`（日期）字段。匹配只认 `Link.memo_id == local memo_uid`，不会拿标题或日期猜。

**独立的纯文字归档：** 把目标根页面 ID 填入 `notion_text_root_page_id`，只向这棵页面树授权 **Read/Insert/Update content**，然后运行 `flomo-vault notion-text setup`。首次同步会建立空的本地映射；笔记页面按年、月、周归档。已有归档需要迁移时，才使用 `notion-text bootstrap` 和外部映射文件。

```bash
flomo-vault daily --json
flomo-vault notion-text status --json
```

`daily` 会先发布本地快照，再做 Notion 校对。Notion 数据缺失或暂时延迟，不会撤销已经成功的本地导出。退出码 `0` 表示完成或正常的镜像延迟；`2` 表示本地导出已完成，但附件或 Notion 仍需关注；`1` 表示本地导出失败，上一份 `current` 会保留。

## 想每天自动更新？

运行 `flomo-vault schedule install`，即可注册每天 08:00（本机时间）运行的 macOS LaunchAgent；登录时也会检查是否错过当日任务。如果你正把 flomo 放在前台写东西，定时任务会推迟，避免打断输入。

安装程序会创建专用的后台 Helper。只有 macOS 提示需要时，才给它完整磁盘访问权限。用 `flomo-vault schedule status` 查看状态，用 `flomo-vault schedule uninstall` 移除。Helper 会复制 `install.command` 所用安装中的基础 Python 可执行文件；手动运行 `sync` 不依赖 Helper。

## 数据放在哪里，谁能读到

资料库里有你的私人笔记，不要把它提交到 Git，也不要作为 Issue 附件上传。仓库的 `.gitignore` 会忽略常见生成数据，但发布代码前仍要亲自检查 `git status`。Notion Token 保存在 Keychain；带签名的附件 URL 和访问 Token 不写入资料库。

MCP server 本身只读，运行在本机。你让 agent 查询笔记时，返回的内容会进入所用客户端的上下文；是否继续发送给模型，取决于该客户端的工作方式。Notion 镜像连接只读；独立的文字归档只向你配置的根页面下写入。

## 运行测试

用 `install.command` 安装后，可以运行：

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

如果依赖安装在当前 `python3` 环境中，也可以运行 `PYTHONPATH=src python3 -m unittest discover -s tests`。

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。

## 边界：can / cannot do

**Can do：** 读取当前已发布的本地快照；按文字、日期和标签找笔记；浏览全文与附件信息；追最多两层引用；统计未删除笔记。你明确要求时，也能读取快照中保留的已删除笔记。下一次 `flomo-vault sync` 发布新快照后，MCP 会在下一次调用时跟上。

**Cannot do：**

- **不能替你同步或改笔记。** MCP 没有新建、修改、删除、恢复、`refresh` 或 `sync` 工具，也不会去读正在运行的 flomo 数据库。刚写完但还没同步的内容，它看不到。
- **不能凭意思猜出没写到的词。** `search_memos` 是文字子串匹配，多词按 AND 处理；它没有语义检索、向量搜索或相关性排序。
- **不能读取或解析附件原始文件。** 它返回附件记录、下载状态、本地路径和资料库里已有的转写文本；没有图片识别或音频播放工具。文件没下载时，路径也不能当作可用文件。
- **不能通过这七个工具检索历史版本或草稿，也不能操作 Notion。** 资料库虽然存有 `history.jsonl` 和 `drafts.jsonl`，MCP 暂未开放相应工具；Notion 校对与归档是另外配置的命令行功能。
- **不能把有限范围的快照变成全量统计。** 如果同步时只选了某段日期，搜索和统计都只覆盖这份快照。`get_memo_context` 最多展开两层、返回 50 个邻居；它也不会在邻居列表中显示已删除笔记。
- **不能保证查询内容只停留在本机。** MCP 进程在本机通过 stdio 运行，返回的笔记内容会交给你使用的客户端；客户端如何把内容送给模型，要看你自己的客户端设置。
