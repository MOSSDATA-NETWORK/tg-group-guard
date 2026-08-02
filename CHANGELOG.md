# 更新日志

本文件记录各版本的主要变更，发布 GitHub Release 时请把对应版本的中文说明复制到 Release 正文，
管理后台「系统设置 → 版本更新」会直接展示 Release 正文作为更新日志。

## v1.0.1

- 修复：`AD_GUARD_MIN_LENGTH` 配置不生效的死配置问题，低于最小长度的消息不再送入
  LLM 广告检测，节省 API 配额并避免短消息误判。
- 修复：Prometheus 指标大面积未接线 —— `telegram_group_guard_bot_messages_total`、
  `telegram_group_guard_bot_verification_total` 现在按处理结果正常计数，评分 Redis
  降级时 `telegram_group_guard_bot_score_redis_degraded` 会正确置 1，告警规则不再形同虚设。
- 修复：Windows 部署下更新/回滚后自重启不可靠（`os.execv` 无法平滑替换进程），
  改为子进程接力重启，避免进程僵死与端口占用。
- 修复：低分提醒文案「今日评分」与永久评分的实际语义不符，已改为「累计评分」。
- 修复：验证 token 锁释放瞬间的竞态条件，避免等待者与新请求并发绕过串行化。
- 修复：更新源码包解压时的路径穿越校验不严谨（同前缀兄弟目录可能误判通过）。
- 清理：`app/bot.py` 中重复的导入与重复的初始化调用。
- 变更：Prometheus 指标前缀统一为 `telegram_group_guard_bot_`（6 个指标）；
  Redis 评分键前缀由 `kkbot:adscore` 更名为 `telegram_group_guard_bot:adscore`，
  升级后旧前缀下的历史评分不再读取、成员评分从零累计（可用 `REDIS_SCORE_PREFIX`
  环境变量改回旧前缀以保留历史数据）。
- 新增：`monitoring/` Grafana 监控仪表盘模板（13 个面板，导入你已有的 Grafana
  即用），附预览图、Prometheus 抓取配置片段与 3 条推荐告警规则，
  详见 `monitoring/README.md`。

## v1.0.0

- 新增「系统设置」页：全部配置参数可在管理后台查看与修改，保存后立即生效；
  少数需要重启的参数会明确标注，保存后可一键重启。
- 新增按群差异化配置：广告守卫开关/阈值/封禁、关键词回复与删除开关、
  消息自动删除 TTL、警告上限共 7 项可按群覆盖，未覆盖项跟随全局。
- 新增版本检查：定时与 GitHub Release 比对版本号，发现新版本时在设置页提示，
  展示中文更新日志，支持一键更新（git pull 或下载源码包）并自动重启；
  非 git 部署同样可用，data/、.env、ssl/ 会被保留。
- 新增更新前自动备份（data/ 与代码快照，保留最近 5 份）与一键回滚。
- 新增「关停服务」危险操作按钮（双重确认，防误操作）。
- 配置修改、更新/回滚/关停等管理操作会向所有授权群发送 Telegram 通知。
- 新增防误关提醒：关闭或离开管理后台页面时浏览器会弹出确认提示，
  可在页面右上角开关该提醒。
- 管理后台新增「关键词删除」规则编辑器。
