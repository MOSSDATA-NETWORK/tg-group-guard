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

## 关键配置

| 变量 | 说明 |
|------|------|
| `TELEGRAM_BOT_TOKEN` | BotFather 签发的 Token |
| `TELEGRAM_BOT_USERNAME` | 机器人用户名（不含 `@`） |
| `VERIFY_BASE_URL` | 外网可达的验证页基址，如 `https://verify.example.com` |
| `ALLOWED_CHAT_IDS` | 授权群 ID（逗号分隔）；后台登录也依赖此列表 |
| `REDIS_URL` | Redis 连接串 |
| `AD_GUARD_ENABLED` | 是否启用广告守卫 |
| `AD_GUARD_PROVIDER` | `ollama` 或 `openai` |
| `ADMIN_WEB_ENABLED` | 是否启用 `/admin` |

完整说明见 [`.env.example`](.env.example)。

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
  templates/           # 验证页与后台 UI
  storage.py           # SQLite
config/
  ad_guard_rules.json  # 广告规则（支持热重载）
```

## 许可

MIT — 见 [LICENSE](LICENSE)。
