# ClawBot — 微信 AI 生活日志助手

微信个人 AI 助手，通过自然语言对话自动记录生活事件到 Obsidian Vault，管理待办任务与定时提醒，每半小时 AI 签到追踪状态。**聊天即记录，零摩擦日志。**

## 架构

```
微信APP ──iLink Bot API──► ClawBotClient ──JSON-RPC/stdin-stdout──► OpenCode ACP ──► Obsidian Vault
  (用户)    HTTPS长轮询      (Python asyncio)   Agent Comm Protocol    (opencode)      (Markdown文件)
```

## 核心功能

- **自然语言记录**：体重、运动、饮食、用药、快递……聊天中随口说就自动写入生活日志
- **待办管理**：新增/完成/跳过/放弃/重排，自动推进链 + 汇总回复
- **定时提醒**：从日志解析未完成提醒，最小堆精确定时推送，支持 LLM 文案润色
- **时间轴签到**：每半小时 AI 主动推送（已填概括 / 空格催填），7am 自动唤醒，2am 强制休眠
- **图片多模态**：微信发图 → qwen3.6-plus 描述 → 自动转记录
- **Agent 模式**：白名单用户可执行终端命令、文件操作，高危权限需微信二次确认

## 技术栈

| 层级 | 技术 |
|------|------|
| 语言 | Python 3 / asyncio |
| 微信接入 | iLink Bot API (ilinkai.weixin.qq.com)，HTTP 长轮询 |
| AI 协议 | OpenCode ACP (JSON-RPC 2.0 over stdin/stdout) |
| 大模型 | deepseek-v4-flash (文本) / qwen3.6-plus (多模态) |
| HTTP | aiohttp |
| 知识库 | Obsidian Vault (Markdown) |
| 部署 | Windows Service (pywin32)，开机自启 |

## 快速开始

### 1. 环境准备

```bash
pip install -r requirements.txt
# 确保 opencode CLI 在 PATH 中
opencode --version
```

### 2. 配置

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml，至少填写 vault.root 路径
```

### 3. 运行

```bash
# 前台调试
python bot.py

# 或安装为 Windows 服务
python service.py install
net start ClawBot
```

### 4. 使用

1. 启动后终端会显示二维码链接
2. 用微信扫描 → 手机确认登录
3. 开始对话即可

## 项目结构

```
.clawbot/
├── bot.py              # 主入口，消息循环 + 调度器启动
├── service.py          # Windows Service 包装
├── config.py           # 配置加载与严格校验
├── config.yaml         # 运行时配置（不提交）
├── acp/
│   └── opencode_client.py  # ACP 协议客户端 (1300+ 行)
├── wechat/
│   └── client.py       # iLink Bot API 客户端
├── handlers/
│   ├── base.py         # Handler 组合 + 路由管道
│   ├── dispatcher.py   # 统一决策与分发引擎
│   └── coaches/        # 各业务域 coach
├── scheduler/
│   ├── reminders.py    # 提醒调度器（heapq）
│   ├── checkin.py      # 时间轴签到调度器
│   ├── archive.py      # 日志归档
│   └── log_rotation.py # 日志轮转
├── prompts/            # LLM prompt 模板（热更新）
├── utils/              # 工具函数
├── tests/              # 单元测试
└── requirements.txt
```

## 会话模型

5 个独立 ACP session 按业务域分流，减少跨域上下文污染：

| Session | 用途 | 权限 |
|---------|------|------|
| unified | 意图决策 + 回复生成 | auto |
| todo | 待办操作 | auto |
| record | 生活记录写入 | auto |
| remind | 提醒设置 | auto |
| agent | 白名单用户全能力 | confirm |

## License

MIT
