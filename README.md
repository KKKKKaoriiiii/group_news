# 群新闻生成器 (Group News / Fake News Generator)

油腔滑调、极其夸张的群聊"假新闻"生成插件。每天定时整理群友们的日常碎碎念，编造成充满新闻腔调和幽默讽刺的"群内大新闻"，以**报纸风格长图**形式公开发布。

## 工作原理

```
群聊消息 ──→ EventHandler 收集 ──→ 内存缓冲区（按群分组）
                                        │
                              每 N 分钟触发一次
                                        │
                                        ▼
                              LLM 整理 → 增量摘要（持久化存储）
                                        │
                                    检查溢出
                                   ├─ trim → 删最旧条目
                                   └─ publish → 加急发布
                                        │
                              每天定时 / 溢出触发
                                        │
                                        ▼
                    LLM 生成结构化 Markdown 新闻文本
                                        │
                                        ▼
                    解析 → Jinja2 HTML → Playwright 渲染
                                        │
                                        ▼
                           报纸风格长图 → 发送到群 → 清空摘要
```

1. **消息收集**：监听所有群聊消息，过滤私聊，按群分组暂存到内存缓冲区
2. **定时整理**：每隔一段时间（默认 60 分钟），将未处理的消息通过 LLM 整理成摘要，增量追加到持久化存储
3. **溢出控制**：摘要条目超过上限时，按配置选择删除最旧条目或立刻加急发布
4. **新闻生成**：读取累积摘要，LLM 生成结构化 Markdown 文本（含标题/板块/段落/编辑点评）
5. **图片渲染**：解析 Markdown → HTML 模板 → Playwright Chrome 无头渲染 → 报纸风格长图
6. **发布发送**：长图 base64 编码后通过 `send_image` 发送到群，清空摘要

## 新闻风格

- 油腔滑调，极其夸张，充满新闻腔调的幽默讽刺
- 把普通聊天包装成惊天大新闻
- **报纸排版长图**：深色标题栏、渐变背景、花式分隔线、编辑点评块
- 字数不限，LLM 尽情发挥

## 依赖

插件需要 Playwright 的 Chromium 浏览器用于 HTML 渲染：

```bash
uv add playwright
uv run playwright install chromium
```

| 包 | 用途 |
|----|------|
| `playwright` | 无头 Chromium 渲染 HTML 为 PNG 长图 |
| `pillow` | 图片处理 |

## 快速开始

### 1. 启用插件

插件默认启用。如需关闭，编辑 `config/plugins/group_news/config.toml`：

```toml
[plugin]
enabled = true
```

### 2. 配置 LLM 模型

必须指定用于生成新闻的 LLM 模型名称（与 `config/model.toml` 中配置的模型名称一致）：

```toml
[llm]
news_model = "your-model-name"
```

### 3. 调整发布时间（可选）

```toml
[schedule]
publish_hour = 19                 # 每日发布时间（小时，0-23）
publish_minute = 0                # 每日发布时间（分钟，0-59）
summarize_interval_minutes = 60   # 摘要整理间隔（分钟）
```

## 配置参考

### `[plugin]`

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | bool | `true` | 是否启用插件 |
| `allow_tool` | bool | `false` | 是否允许 Chatter 手动调用新闻生成工具 |

### `[schedule]`

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `publish_hour` | int | `19` | 每日发布时间（0-23） |
| `publish_minute` | int | `0` | 每日发布时间（0-59） |
| `summarize_interval_minutes` | int | `60` | 摘要整理间隔（分钟） |

### `[storage]`

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `summary_max_entries` | int | `-1` | 群摘要最大条目数，`-1` 不限制 |
| `overflow_action` | str | `"trim"` | 溢出处理：`"trim"` 删最旧条目 / `"publish"` 立刻加急发布并清空 |

### `[llm]`

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `news_model` | str | `""` | LLM 模型名称（必填） |

## 高级功能

### 手动触发工具

如果设置 `plugin.allow_tool = true`，Chatter 可以主动调用 `generate_group_news` 工具手动生成当前群的新闻文本（不会自动发送，由 Chatter 决定是否发布）。

### 多群独立管理

每个群聊独立维护一份摘要，互不干扰。新闻发布时按群分别生成和发送。

### 摘要溢出控制

当累积摘要条目超过 `summary_max_entries` 时（默认 `-1` 不限制）：

- **`trim`**：自动删除最旧的条目，保持条目数不超过上限
- **`publish`**：立即触发一次加急新闻发布（含完整的长图渲染和发送），发布后清空摘要重新累积

### 数据存储

- 群聊摘要持久化存储在 `data/json_storage/group_news/` 目录下
- 原始消息缓冲区仅在内存中保留，不会落盘

## 视觉测试

运行测试脚本可预输入一段示例新闻文本，本地渲染长图预览：

```bash
uv run pytest test/test_group_news_render.py -v -s
```

输出图片保存在 `test/output/` 目录。

## 注意事项

- 仅收集**群聊**消息，私聊消息不会被采集
- 插件不会持久化完整的聊天记录——原始消息仅在 LLM 整理摘要时临时使用，整理后即丢弃
- 新闻内容由 LLM 生成，可能存在编造、夸大成分，纯属娱乐
- 新闻以长图形式发送，首次运行需下载 Chromium（约 180MB）
- 建议在插件启用前向群成员告知本功能的存在和性质
