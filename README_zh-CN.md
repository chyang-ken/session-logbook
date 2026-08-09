# Session Logbook

一个极简、本地、零依赖的面板，把你所有 AI 编程 agent 的 session —— **Claude Code、Codex、Antigravity** —— 汇聚到一页里浏览与整理。

[![CI](https://github.com/chyang-ken/session-logbook/actions/workflows/ci.yml/badge.svg)](https://github.com/chyang-ken/session-logbook/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)

> Other languages: [English](README.md)

你的 agent 在 `~/.claude`、`~/.codex`、`~/.gemini` 下留下了成百上千份 session 逐字稿。Session Logbook **只读**地扫描它们、铺到一页上，让你 star / archive / 加备注 / 搜索 / 重读 —— 全程不离开本机。

## 为什么

你并行跑很多 agent、很多 worktree、横跨很多专案。一份扁平的 session 文件列表根本没法用。这个面板给这堆文件加上结构：

- **一页四区** —— ⭐ Starred / 🔥 Recent / 🕸 Dusty / 📦 Archived，按时间自动降权，主工作面只留"活着的"。
- **多 agent** —— Claude Code、Codex、Antigravity 的 session 统一汇聚、按专案分组。
- **只读且私密** —— 永不发消息、永不 spawn session、不连网络，只绑 `127.0.0.1`，浏览器资源也从本机提供。

## 快速开始

需要 **Python 3.9+**（仅用标准库 —— 无需 `pip install`）。

```bash
git clone https://github.com/chyang-ken/session-logbook.git
cd session-logbook
python3 server.py          # → http://127.0.0.1:47821
```

打开 <http://127.0.0.1:47821>。首次扫描 10–30 秒（视 session 数量），之后只重读 `mtime` 变动的文件。没有构建步骤、无需 `pip install`、浏览器不会拉 CDN——改 `index.html` 刷新浏览器即生效。

## 功能

- **一页四区** + 专案分组（按 cwd 后两级；`.worktrees/` 归并到 parent）。
- **时间衰减** —— N 天没动自动进 🕸 Dusty（UI 切 7/14/21 天）。
- **卡片预览** —— 首则 user 开场 + 最近 user/assistant 轮次。
- **完整对话视图** —— 点卡片展开，user/assistant/tool/skill 四色区分；可弹出全屏阅读（`/?session=<id>`）。
- **对话导航** —— 用 `↑ N/M ↓ go to: __` 跳转 user 轮次，点 `latest` 直达最新消息，或用键盘 `j`/`k` 前后移动。
- **全文搜索** —— 多词 AND、片段高亮、session ID 也匹配（`ripgrep` 加速，纯 Python 兜底）。
- **Star / Archive / Note** —— 轻量整理，持久化到 `~/.session-logbook/state.json`。
- **Files 面板** —— 浏览专案最近改动文件，或按文件名模糊查找（`fd` 驱动）。
- **可下载的锚点稿** —— 导出带原文行号锚点的紧凑 transcript，方便喂给 agent 分析。

## 延伸阅读

| 你想… | 去 |
|---|---|
| **试用** | 上面的「快速开始」 |
| **理解设计与边界** | [`docs/philosophy.md`](docs/philosophy.md) |
| **改 UI** | [`docs/design-system.md`](docs/design-system.md) |
| **参与贡献** | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| **报 bug / 提需求** | [开 issue](https://github.com/chyang-ken/session-logbook/issues) |
| **报告安全问题** | [`SECURITY.md`](SECURITY.md) |

## 它刻意不做

发消息 · spawn session · 多用户认证 · 实时推送（SSE/WebSocket）· 跨机同步。CLI 已是 orchestrator —— 这是一个只读的 cockpit，不是 client。理由见 [`docs/philosophy.md`](docs/philosophy.md)。

## 许可

[MIT](LICENSE)
