# KKBot

Telegram 群组入群验证 + AI 广告守卫机器人。

- 新成员入群限制发言，完成 Web 验证后解除
- 可选 AI / 启发式广告检测（Ollama 或 OpenAI 兼容接口）
- 群管指令：警告、封禁、解封等
- Admin WebUI（Telegram Login Widget）
- Prometheus `/metrics`（可选）

## 功能概览

| 模块 | 说明 |
|------|------|
| 入群验证 | 限制新成员 → 私聊/按钮获取链接 → Web 页完成验证 |
| 广告守卫 | LLM + 规则热重载；支持投票复核、评分跳过 |
| 关键词回复 | 命中关键词/正则自动回复；规则文件热重载、按群冷却 |
| 管理指令 | `/warn` `/ban` `/unban` `/sb` `/id` `/re` `/up` 等 |
| 管理后台 | `/admin`，仅 `ALLOWED_CHAT_IDS` 中群的管理员可登录 |

## 依赖

- Python 3.11+
- Redis（评分等运行时状态）
- （可选）Ollama 或任意 OpenAI 兼容 LLM 端点

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env：至少填写 TELEGRAM_BOT_TOKEN、TELEGRAM_BOT_USERNAME、VERIFY_BASE_URL

mkdir -p data
python -m app.main
```

默认 Web 监听 `WEB_HOST`/`WEB_PORT`（见 `.env.example`）。

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
| `AD_GUARD_MIN_LENGTH` | `0` | 已废弃，仅为兼容旧配置保留 |

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

### 日志

| 变量 | 默认 | 说明 |
|------|------|------|
| `LOG_LEVEL` | `INFO` | 日志级别（`DEBUG`/`INFO`/`WARNING`/`ERROR`），启动时会打印一份脱敏的有效配置快照 |

## 关键词自动回复

设置 `KEYWORD_REPLY_ENABLED=true` 后，群消息命中 `config/keyword_replies.json` 中的规则时自动回复（独立于广告守卫，即使 `AD_GUARD_ENABLED=false` 也生效）。

规则可直接在 Admin WebUI（`/admin` → 关键词回复）在线编辑，保存后立即生效；也可以手动修改 `config/keyword_replies.json`，支持热重载：

```json
{
  "cooldown_seconds": 60,
  "rules": [
    { "keywords": ["群规", "规则"], "match": "any", "reply": "📜 群规请查看置顶消息。" },
    { "keywords": ["新人", "进群"], "match": "all", "reply": "👋 请先完成验证。" },
    { "pattern": "(?i)(怎么|如何)(加入|验证)", "reply": "🔐 请点击群内验证按钮。" }
  ]
}
```

- `keywords` 为包含匹配，`match: "any"` 任一命中 / `"all"` 全部命中；默认不区分大小写（`case_sensitive: true` 可改）
- `pattern` 为正则匹配，与 `keywords` 同时配置时 `pattern` 优先
- 命令（`/` 开头）与编辑消息不触发；同一群同一规则在冷却期内只回复一次

## Admin WebUI

1. 设置 `VERIFY_BASE_URL` 为你的公网域名
2. 设置 `ALLOWED_CHAT_IDS`
3. 在 [@BotFather](https://t.me/BotFather) 执行 `/setdomain`，绑定同一域名
4. `ADMIN_WEB_ENABLED=true` 后重启，访问 `{VERIFY_BASE_URL}/admin`

## HTTPS（可选）

将证书放到 `ssl/`，并在 `.env` 中设置：

```env
SSL_CERT_FILE=ssl/your.crt
SSL_KEY_FILE=ssl/your.key
```

也可在前面挂 Nginx / Caddy / Cloudflare 反代，此时可把 `ADMIN_BEHIND_PROXY=true`。

## 项目结构

```
app/
  main.py              # 入口
  bot.py / web.py      # Bot 与 FastAPI
  routers/             # 验证、广告、管理指令
  bot_components/      # 验证、评分、权限等
  keyword_replies.py   # 关键词回复引擎（热重载 + 后台校验/保存）
  templates/           # 验证页与后台 UI
  storage.py           # SQLite
config/
  ad_guard_rules.json  # 广告规则（支持热重载）
  keyword_replies.json # 关键词回复规则（热重载，可在后台编辑）
test_fixes.py          # 回归测试（.venv 下运行：python test_fixes.py）
```

## 许可

MIT — 见 [LICENSE](LICENSE)。
