# Telegram-group-guard-bot

Telegram 群组入群验证 + AI 广告守卫 + 关键词自动管理机器人。

- 新成员入群限制发言，完成 Web 验证后解除
- 可选 AI / 启发式广告检测（Ollama 或 OpenAI 兼容接口）
- 关键词自动回复（支持 HTML / MarkdownV2 / 纯文本）
- 关键词自动删除（命中规则后自动删除消息）
- 群管指令：警告、封禁、解封等
- Admin WebUI（Telegram Login Widget）在线管理规则
- Prometheus `/metrics`（可选）

## 功能概览

| 模块 | 说明 |
|------|------|
| 入群验证 | 限制新成员 → 私聊/按钮获取链接 → Web 页完成验证 |
| 广告守卫 | LLM + 规则热重载；支持投票复核、评分跳过 |
| 关键词回复 | 命中关键词/正则自动回复；支持 HTML / MarkdownV2；规则文件热重载 |
| 关键词删除 | 命中关键词/正则自动删除用户消息；Admin WebUI 在线配置 |
| 管理指令 | `/warn` `/ban` `/unban` `/sb` `/id` `/re` `/up` 等 |
| 管理后台 | `/admin`，仅 `ALLOWED_CHAT_IDS` 中群的管理员可登录 |

## 依赖

- Python 3.11+
- Redis（评分等运行时状态）
- （可选）Ollama 或任意 OpenAI 兼容 LLM 端点

## 快速开始

### 首次部署

```bash
# 1. 克隆仓库
git clone https://github.com/MOSSDATA-NETWORK/tg-group-guard.git
cd tg-group-guard

# 2. 创建虚拟环境
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 复制并编辑配置文件
cp .env.example .env
# 编辑 .env，至少填写以下必填项：
#   TELEGRAM_BOT_TOKEN      # 从 @BotFather 获取
#   TELEGRAM_BOT_USERNAME   # 机器人用户名（不含 @）
#   VERIFY_BASE_URL         # 外网可达的验证页基址，如 https://bot.example.com
#   ALLOWED_CHAT_IDS        # 授权群 ID（逗号分隔）

# 5. 创建数据目录
mkdir -p data config

# 6. 启动服务
python -m app.main
```

### 设置 Bot 命令菜单

首次启动后，在 [@BotFather](https://t.me/BotFather) 执行 `/setcommands`，粘贴以下内容：

```
start - 显示机器人信息
id - 查询用户信息
warn - 警告目标成员
ban - 封禁目标成员
unban - 解封目标成员
sb - 删除消息并封禁（Spam+Ban）
re - 转发目标消息到当前群
up - 提升目标成员为管理员
```

> 机器人启动时会自动同步命令列表到 Telegram，但 `/setcommands` 可以自定义菜单显示文本。

### 更新教程

**方式一：管理后台一键更新（推荐）**

打开 `/admin` →「系统设置」页，顶部版本卡片会显示当前版本与 GitHub 最新版本；
发现新版本时展示中文更新日志，点击「立即更新并重启」即可自动更新并重启服务，
页面会在服务恢复后自动刷新。

- git 克隆部署：执行 `git pull --ff-only` + 安装依赖；本地有未提交改动时会提示手动处理。
- 非 git 部署（直接上传代码）：自动下载 Release 源码包覆盖代码，`data/`、`.env`、`ssl/` 会保留。
- 更新前自动备份 `data/` 与代码快照到 `backups/`（保留最近 5 份），更新异常可一键回滚。

> 发布新版本时请在 GitHub Release 正文中用中文写更新说明，后台会直接展示；同时维护根目录 `CHANGELOG.md`。

**方式二：手动更新**

```bash
# 1. 拉取最新代码
git pull origin main

# 2. 更新依赖（每次更新都建议执行，可能有新依赖）
pip install -r requirements.txt

# 3. 检查 .env.example 是否有新增配置项
diff .env.example .env
# 如有新增项，按需要添加到 .env

# 4. 重启服务
# 使用 systemd / pm2 / screen 等管理进程的用户，重启对应服务即可
```

> 更新不会丢失数据：`data/verifications.sqlite3`、配置文件和规则文件均会保留。建议在重大更新前备份 `data/` 目录。

## 配置参数

最小可运行配置只需 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_BOT_USERNAME`、`VERIFY_BASE_URL` 三项，其余均有默认值。完整注释见 [`.env.example`](.env.example)。

### 必填

| 变量 | 默认 | 说明 |
|------|------|------|
| `TELEGRAM_BOT_TOKEN` | — | BotFather 签发的 Token |
| `TELEGRAM_BOT_USERNAME` | — | 机器人用户名（不含 `@`），用于生成验证深链 |

### Web 服务

| 变量 | 默认 | 说明 |
|------|------|------|
| `VERIFY_BASE_URL` | `http://localhost:8000` | 外网可达的验证页基址，生成 `/verify/{token}` 链接 |
| `WEB_HOST` | `0.0.0.0` | 监听地址；设为 `dual` 或 `::` 时同时监听 IPv4+IPv6 |
| `WEB_PORT` | `8000` | 监听端口 |
| `SSL_CERT_FILE` / `SSL_KEY_FILE` | 空 | HTTPS 证书/私钥，需同时设置；只设一个则回退纯 HTTP |
| `SSL_CA_FILE` | 空 | 可选 CA 链文件 |

### 存储

| 变量 | 默认 | 说明 |
|------|------|------|
| `DATABASE_PATH` | `data/verifications.sqlite3` | SQLite 路径（验证记录、警告、封禁/广告日志、合格名单） |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 连接串（广告扣评分的权威存储，启动时不可达会直接报错） |
| `REDIS_SCORE_PREFIX` | `kkbot:adscore` | 评分 key 前缀 |

### 入群验证

| 变量 | 默认 | 说明 |
|------|------|------|
| `VERIFICATION_TIMEOUT_SECONDS` | `600` | 验证 token 有效期；超时未验证自动踢出 |
| `CLEANUP_INTERVAL_SECONDS` | `300` | 过期验证清理任务的运行间隔 |
| `ALLOWED_CHAT_IDS` | 空 | 授权群 ID（逗号分隔）；留空不限制，后台登录也依赖此列表 |

### 消息生命周期

| 变量 | 默认 | 说明 |
|------|------|------|
| `MESSAGE_TTL_SECONDS` | 空 | 提示消息自动删除延迟（秒）；留空或 0 表示保留 |

### 广告守卫

| 变量 | 默认 | 说明 |
|------|------|------|
| `AI_ENABLED` | `true` | AI 功能总开关 |
| `AD_GUARD_ENABLED` | `false` | 广告守卫开关；开启但缺 LLM 端点配置时启动直接报错（防静默失效） |
| `AD_GUARD_PROVIDER` | `ollama` | `ollama` 或 `openai`（旧值 `hunyuan` 自动归一化为 `openai`） |
| `AD_GUARD_THRESHOLD` | `0.8` | 广告判定置信度阈值。提高 → 拦截更保守；降低 → 更激进但误判更多 |
| `AD_GUARD_BAN` | `false` | 命中广告且评分低于阈值时是否封禁（否则仅删除/踢出） |
| `AD_GUARD_SCORE_SKIP_THRESHOLD` | `3` | 通过检测的有效发言满 N 次后永久免检（SQLite 持久） |
| `AD_GUARD_SCORE_BAN_THRESHOLD` | `-10` | 评分 ≤ 此值触发低分清人；应为负数 |
| `AD_VOTE_DURATION_SECONDS` | `30` | 广告复核投票持续时长 |
| `AD_GUARD_LLM_CONCURRENCY` | `4` | LLM 检测并发上限（保护本地 Ollama） |
| `AD_GUARD_RULES_FILE` | `config/ad_guard_rules.json` | 启发式规则文件，支持热重载 |
| `AD_GUARD_MIN_LENGTH` | `0` | 消息长度低于该值时跳过 LLM 检测（0 = 全检）；启发式规则不受限制 |

> 判定策略速览：本地规则命中或 LLM 置信度 ≥ 0.95 → 跳过投票直接封禁；低于阈值 → 放行；中间档 → 群内限时投票，"不是广告"票必须严格多于"广告"票才放行（平票/0 票维持原判）。未封禁时管理员可通过按钮复核"立即封禁 / 这不是广告"，复核恢复会退还误扣评分。

### Ollama（`AD_GUARD_PROVIDER=ollama` 时必填）

| 变量 | 默认 | 说明 |
|------|------|------|
| `OLLAMA_ENDPOINT` | 空 | 如 `http://127.0.0.1:11434` |
| `OLLAMA_MODEL` | `qwen3:0.6b` | 模型名 |
| `OLLAMA_TIMEOUT_SECONDS` | `30` | 请求超时，实际下限 30 秒 |

### OpenAI 兼容端点（`AD_GUARD_PROVIDER=openai` 时必填）

| 变量 | 默认 | 说明 |
|------|------|------|
| `OPENAI_ENDPOINT` | 空 | 任意 OpenAI 协议兼容端点（OneAPI / vLLM / 混元等） |
| `OPENAI_MODEL` | `gpt-4o-mini` | 模型名 |
| `OPENAI_API_KEY` | 空 | API Key |
| `OPENAI_TIMEOUT_SECONDS` | `30` | 请求超时，实际下限 30 秒；旧名 `HUNYUAN_*` 仍兼容读取 |

### 警告 / 代理 / 可观测

| 变量 | 默认 | 说明 |
|------|------|------|
| `WARN_LIMIT` | `3` | 本月警告达此次数自动封禁 |
| `TELEGRAM_PROXY` | 空 | 访问 Bot API 的 socks5/http 代理 |
| `ENABLE_METRICS` | `false` | Prometheus `/metrics` 端点（需 `pip install prometheus_client`） |

### Admin WebUI

| 变量 | 默认 | 说明 |
|------|------|------|
| `ADMIN_WEB_ENABLED` | `true` | `/admin` 后台开关（Telegram Login Widget 登录） |
| `ADMIN_SESSION_TTL_SECONDS` | `28800` | 登录会话有效期（8 小时），下限 300 |
| `ADMIN_MAX_SESSIONS_PER_USER` | `5` | 每用户并发会话数，超出挤掉最旧 |
| `ADMIN_RATE_LIMIT_PER_MIN` | `60` | 每 IP 每分钟 `/admin` 请求上限 |
| `ADMIN_BEHIND_PROXY` | `false` | 仅在确有 Nginx/Caddy/CF 反代时设 `true`；直连部署设为 `true` 会被伪造 `X-Forwarded-For` 绕过限流 |
| `ADMIN_AUTH_AGE_SECONDS` | `300` | Telegram 登录签名有效窗口，官方上限 86400 |

### 关键词自动回复

| 变量 | 默认 | 说明 |
|------|------|------|
| `KEYWORD_REPLY_ENABLED` | `false` | 总开关；独立于广告守卫，可在 Admin WebUI 在线编辑规则 |
| `KEYWORD_REPLY_RULES_FILE` | `config/keyword_replies.json` | 规则文件，支持热重载 |
| `KEYWORD_REPLY_COOLDOWN_SECONDS` | `60` | 同一群同一规则的默认冷却秒数；规则文件中的 `cooldown_seconds` 可覆盖 |

### 关键词自动删除

| 变量 | 默认 | 说明 |
|------|------|------|
| `KEYWORD_DELETION_ENABLED` | `false` | 总开关；命中规则后自动删除用户消息 |
| `KEYWORD_DELETION_RULES_FILE` | `config/keyword_deletions.json` | 规则文件，支持热重载；可在 Admin WebUI 在线编辑 |

### 版本检查与自助更新

| 变量 | 默认 | 说明 |
|------|------|------|
| `UPDATE_CHECK_ENABLED` | `true` | 定时与 GitHub Release 比对版本号，有新版本时在「系统设置」页提示 |
| `UPDATE_CHECK_INTERVAL_SECONDS` | `21600` | 检查间隔（秒），默认 6 小时，下限 300 |
| `GITHUB_REPO` | `MOSSDATA-NETWORK/tg-group-guard` | 比对目标仓库（`owner/repo`），fork 后改成自己的 |
| `GITHUB_TOKEN` | 空 | 可选；GitHub API 令牌，避免匿名限流（60 次/小时） |

### 日志

| 变量 | 默认 | 说明 |
|------|------|------|
| `LOG_LEVEL` | `INFO` | 日志级别（`DEBUG`/`INFO`/`WARNING`/`ERROR`），启动时会打印一份脱敏的有效配置快照 |

## 管理员指令

所有指令均需在**群聊**中使用，且执行者需具备对应权限。机器人启动时会自动将命令列表同步到 Telegram，输入 `/` 即可看到命令菜单。

> 指令中的「回复消息」指在 Telegram 中长按/右键目标消息并选择「回复」后再发送指令。

| 指令 | 权限 | 用法 | 说明 |
|------|------|------|------|
| `/start` | 群聊中仅管理员 | `/start` | 显示机器人信息。私聊中可用于验证入口。 |
| `/id` | 管理员 | `/id` 或回复消息后发送 | 查询用户信息。不回复时查自己，回复时查目标用户。输出包含：用户ID、群ID、广告合格进度、广告扣分、本月警告次数、用户DC、群组DC。 |
| `/warn` | 管理员 | `/warn` 回复消息，或 `/warn 原因` | 警告目标成员。可选附加原因。当本月累计警告达到 `WARN_LIMIT` 次时自动封禁。 |
| `/ban` | 管理员 | `/ban` 回复消息，或 `/ban <用户ID>` | 封禁目标成员。支持通过回复消息或提供用户ID两种方式指定对象。封禁后清零广告评分与合格进度。 |
| `/unban` | 管理员 | `/unban` 回复消息，或 `/unban <用户ID>` | 解封目标成员。支持通过回复消息或提供用户ID两种方式指定对象。 |
| `/sb` | 管理员 | `/sb` 回复消息 | **删除并封禁**（Spam + Ban）。先删除目标消息，再封禁发送者。 |
| `/re` | 管理员 | `/re` 回复消息 | 将目标消息转发到当前群。转发后自动删除指令消息。 |
| `/up` | 仅群主 | `/up` 回复消息 `[头衔]`，或 `/up <用户ID> <头衔>` | 提升目标成员为管理员并设置头衔。若已是管理员则仅更新头衔。默认头衔为「管理员」，最长16个字符。 |

### 权限说明

- **管理员**：群内的 `administrator` 或 `creator`（群主）。
- **仅群主**：仅 `creator` 可使用，普通管理员无法执行 `/up`。
- 管理员之间**不可互操作**：无法警告、封禁或删除其他管理员的消息。
- 所有指令操作（警告、封禁、解封）均会记录到 SQLite，可在 Admin WebUI 中查看历史。

## 关键词自动回复

设置 `KEYWORD_REPLY_ENABLED=true` 后，群消息命中 `config/keyword_replies.json` 中的规则时自动回复（独立于广告守卫，即使 `AD_GUARD_ENABLED=false` 也生效）。

规则可直接在 Admin WebUI（`/admin` → 关键词回复）在线编辑，保存后立即生效；也可以手动修改 `config/keyword_replies.json`，支持热重载：

```json
{
  "cooldown_seconds": 60,
  "rules": [
    { "keywords": ["群规", "规则"], "match": "any", "reply": "📜 群规请查看置顶消息。" },
    { "keywords": ["新人", "进群"], "match": "all", "reply": "👋 请先完成验证。" },
    { "pattern": "(?i)(怎么|如何)(加入|验证)", "reply": "🔐 请点击群内验证按钮。" },
    { "keywords": [" bold "], "reply": "<b> bold text </b>", "parse_mode": "HTML" },
    { "keywords": [" markdown "], "reply": "* bold text *", "parse_mode": "MarkdownV2" }
  ]
}
```

- `keywords` 为包含匹配，`match: "any"` 任一命中 / `"all"` 全部命中；默认不区分大小写（`case_sensitive: true` 可改）
- `pattern` 为正则匹配，与 `keywords` 同时配置时 `pattern` 优先
- `parse_mode` 可选 `"HTML"` / `"MarkdownV2"`，留空则为纯文本。支持加粗、链接、按钮等富文本格式
- 命令（`/` 开头）与编辑消息不触发；同一群同一规则在冷却期内只回复一次

## 关键词自动删除

设置 `KEYWORD_DELETION_ENABLED=true` 后，群消息命中 `config/keyword_deletions.json` 中的规则时自动删除该消息。

规则可在 Admin WebUI（`/admin` → 关键词删除）在线编辑，保存后立即生效：

```json
{
  "rules": [
    { "keywords": ["垃圾广告"], "match": "any" },
    { "pattern": "(?i)(加群|扫码|转账).*" }
  ]
}
```

- 与关键词回复独立的配置系统和开关
- 规则结构与关键词回复类似，但**不需要 `reply` 字段**
- 命中后自动删除消息，不发送任何回复

## Admin WebUI

1. 设置 `VERIFY_BASE_URL` 为你的公网域名
2. 设置 `ALLOWED_CHAT_IDS`
3. 在 [@BotFather](https://t.me/BotFather) 执行 `/setdomain`，绑定同一域名
4. `ADMIN_WEB_ENABLED=true` 后重启，访问 `{VERIFY_BASE_URL}/admin`

后台包含：统计概览、进群/广告/封禁日志、关键词回复与删除规则编辑器，以及「系统设置」页：

- **系统设置**：全部配置参数可视化编辑，保存后立即生效并持久化到 `data/admin_overrides.json`
  （优先级高于 `.env`，重启后仍会应用）；标注「需重启」的项保存后可一键重启服务。
- **按群差异化配置**：广告守卫开关/判定阈值/封禁、关键词回复与删除开关、消息自动删除 TTL、
  警告上限共 7 项可对单个群覆盖，持久化到 `data/chat_overrides.json`，未覆盖项跟随全局。
- **版本更新**：定时与 GitHub Release 比对版本号，有新版本时展示中文更新日志，一键更新并自动重启。
  git 克隆部署走 `git pull`；直接上传代码的部署会自动改用「下载 Release 源码包覆盖」方式，
  `data/`、`.env`、`ssl/` 均会保留。更新前自动备份 `data/` 与代码（`backups/`，保留最近 5 份），
  更新后如发现异常可在版本卡片一键「回滚到更新前版本」。
- **关停服务**：危险操作区的红色按钮，双重确认后关停整个进程（systemd/pm2 守护下会被自动拉起）。
- **管理通知**：配置修改、更新/回滚/关停等操作会向所有 `ALLOWED_CHAT_IDS` 群发送 Telegram 通知。
- **防误关提醒**：关闭/离开后台页面时浏览器会弹出确认提示，可在页面右上角「离开提醒」开关关闭。
- 密钥类字段（Bot Token、API Key）只显示「已设置」，留空保存即保持原值不变。

## HTTPS（可选）

将证书放到 `ssl/`，并在 `.env` 中设置：

```env
SSL_CERT_FILE=ssl/your.crt
SSL_KEY_FILE=ssl/your.key
```

也可在前面挂 Nginx / Caddy / Cloudflare 反代，此时可把 `ADMIN_BEHIND_PROXY=true`。

## 监控（可选）

设置 `ENABLE_METRICS=true` 后，Bot 在 Web 端口暴露 Prometheus `/metrics` 端点（消息处理、验证结果、LLM 延迟/并发、Redis 降级等）。

`monitoring/` 目录附带一个现成的 **Grafana 仪表盘模板**（`grafana-dashboard.json`，13 个面板），直接导入你已有的 Grafana 即可使用，另附 Prometheus 抓取配置片段和推荐告警规则，详见 [monitoring/README.md](monitoring/README.md)。

## 项目结构

```
app/
  main.py                 # 入口
  bot.py / web.py         # Bot 与 FastAPI
  routers/                # 验证、广告、管理指令
  bot_components/         # 验证、评分、权限等
  keyword_replies.py      # 关键词回复引擎（热重载 + 后台校验/保存）
  keyword_deletions.py    # 关键词删除引擎（热重载 + 后台校验/保存）
  templates/              # 验证页与后台 UI
  storage.py              # SQLite
config/
  ad_guard_rules.json     # 广告规则（支持热重载）
  keyword_replies.json    # 关键词回复规则（热重载，可在后台编辑）
  keyword_deletions.json  # 关键词删除规则（热重载，可在后台编辑）
monitoring/
  grafana-dashboard.json  # Grafana 仪表盘模板（导入即用）
test_fixes.py             # 回归测试（.venv 下运行：python test_fixes.py）
```

## 许可

MIT — 见 [LICENSE](LICENSE)。
