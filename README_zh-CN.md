# Session Logbook

一个极简、本地、零依赖的 Session 找回工具，把 **Claude Code、Codex、Antigravity、Kimi Code、Devin Local 和 Pi** 的 session 汇聚到一处，帮助你找到并重读过去的工作。

[![CI](https://github.com/chyang-ken/session-logbook/actions/workflows/ci.yml/badge.svg)](https://github.com/chyang-ken/session-logbook/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)

> Other languages: [English](README.md)

你的 agent 在 `~/.claude`、`~/.codex`、`~/.gemini`、`~/.kimi-code`、`~/.pi/agent/sessions` 和 Devin Local 数据库里留下了成百上千份 session 记录。Session Logbook **只读**地扫描它们，让你按时间或文字找到过去的工作、打开完整详情并继续利用，全程不离开本机。

## 为什么

Session 多到一定程度后，逐个手动管理就不再可持续。Session Logbook 聚焦两个长期有用的任务：

- **找回过去的工作** —— 按时间浏览，或搜索你还记得的词。
- **在完整上下文中重读** —— 直接打开 Session Detail，不靠文件名和摘要猜当时做了什么。
- **跨 agent 汇聚** —— Claude Code、Codex、Antigravity、Kimi Code、Devin Local 和 Pi 共用一个本地入口。
- **只读且私密** —— 永不发消息、永不 spawn session、不连网络，只绑 `127.0.0.1`，浏览器资源也从本机提供。

## 快速开始

需要 **Python 3.9+**（仅用标准库 —— 无需 `pip install`）。

```bash
git clone https://github.com/chyang-ken/session-logbook.git
cd session-logbook
python3 server.py          # → http://127.0.0.1:47821
```

打开 <http://127.0.0.1:47821>。首次扫描 10–30 秒（视 session 数量），之后只重读 `mtime` 变动的文件。没有构建步骤、无需 `pip install`、浏览器不会拉 CDN——改 `index.html` 刷新浏览器即生效。

## 把一场 Session 交给另一个 Agent

网页服务不需要启动。只读的 Agent 命令可以接收 Session ID、准确的 JSONL 路径或搜索词：

```bash
# 生成带 [L#] 原文锚点的精简上下文
python3 session_logbook_cli.py context '<session-id-or-path>'

# 下次检查先重复上次读到的最后一行，再返回后续内容
python3 session_logbook_cli.py follow '<session-id-or-path>' --cursor-line 427

# 只搜索最近历史中的真实 User 消息
python3 session_logbook_cli.py search 'payment retry' --role user --since 30d
```

仓库只提供一个 Agent Skill：[`session-logbook`](skills/session-logbook/SKILL.md)。它统一处理
会话交接、后续观察、回原文取证、会话定位和历史挖掘。建议把仓库里的 Skill 以软链接安装，
保证 Skill 与项目命令始终是同一版本：

```bash
mkdir -p ~/.claude/skills ~/.codex/skills
ln -s "$PWD/skills/session-logbook" ~/.claude/skills/session-logbook
ln -s "$PWD/skills/session-logbook" ~/.codex/skills/session-logbook
```

## 功能

Pi 默认读取 `~/.pi/agent/sessions`，支持 `PI_CODING_AGENT_DIR` 和
`PI_CODING_AGENT_SESSION_DIR` 环境变量。读取最后保存的对话分支，并保留压缩前的原始消息；
fork 不会被当作 sub-agent。暂不自动发现扩展专用的子任务目录或逐项目的目录设置。

- **全文搜索** —— 多词 AND、片段高亮，匹配原始标题、自定义标题和 session ID（`ripgrep` 加速，纯 Python 兜底）。
- **跨项目时间流** —— 默认按最近活动倒序，不再需要逐个展开项目。
- **疑似非人类 Session 筛选** —— 默认隐藏尚未确认的单轮 Session；筛选状态始终可见，找不到时可关闭，并手动标记自己参与的 Session。
- **自定义标题** —— 用自己记得住的名字方便下次找回；不修改原始记录，清空即可恢复。
- **完整对话视图** —— 点卡片展开，user/assistant/tool/skill 四色区分；可弹出全屏阅读（`/?session=<id>`）。
- **对话导航** —— 用 `↑ N/M ↓ go to: __` 跳转 user 轮次，点 `latest` 直达最新消息，或用键盘 `j`/`k` 前后移动。
- **卡片预览** —— 首则 user 开场 + 最近 user/assistant 轮次。
- **可选的四区专案视图** —— Starred / Recent / Dusty / Archived，按专案分组。
- **时间衰减** —— N 天没动自动进 🕸 Dusty（UI 切 7/14/21 天）。
- **Star / Archive / Note** —— 轻量整理，持久化到 `~/.session-logbook/state.json`。
- **Files 面板** —— 浏览专案最近改动文件，或按文件名模糊查找（`fd` 驱动）。
- **可下载的锚点稿** —— 导出带原文行号锚点的紧凑 transcript，方便喂给 agent 分析。
- **一个只读 Agent 入口** —— 已知 Session 可直接交接，也能只取新增内容、按原文行取证，或在有限历史范围内检索。

## 延伸阅读

| 你想… | 去 |
|---|---|
| **试用** | 上面的「快速开始」 |
| **理解设计与边界** | [`docs/philosophy.md`](docs/philosophy.md) |
| **让 Agent 使用 Session 历史** | [`skills/session-logbook/SKILL.md`](skills/session-logbook/SKILL.md) |
| **改 UI** | [`docs/design-system.md`](docs/design-system.md) |
| **参与贡献** | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| **报 bug / 提需求** | [开 issue](https://github.com/chyang-ken/session-logbook/issues) |
| **报告安全问题** | [`SECURITY.md`](SECURITY.md) |

## 它刻意不做

发消息 · spawn Session · 管理 Agent 客户端里的 Session 是否继续存活或归档 · 多用户认证 · 实时推送（SSE/WebSocket）· 跨机同步。它是只读的找回工具，不是 Agent 客户端或生命周期管理器。理由见 [`docs/philosophy.md`](docs/philosophy.md)。

## 许可

[MIT](LICENSE)
