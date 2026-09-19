# 追新机器人 (Zhuixin Bot)

一个面向 Telegram 的全自动化影视追新、排期日历对齐与智能打捞机器人。

## ✨ 核心特性

- 📅 **多源日历排期对齐**：对接 TMDB、Bangumi 及主流流媒体日历，实时掌握院线与追剧排期。
- 🔍 **智能缺集雷达探测**：自动比对网盘实盘物理集数与官方总集数，精准定位缺集、断更剧集。
- ⚡ **无缝转存入库联动**：与媒体转存系统深度协同，发现新资源或高规格版本时自动触发打捞与转存。
- 🏷 **智能状态流转与分流**：
  - 连载/缺集剧集存放于 `未完结追新` 目录；
  - 满集收齐且官方 Ended 的剧集自动晋升至 `影视转存总目录` 并加注【完结】标记。
- 🛡 **全自动凭证轮换**：支持网盘 Access Token 自动按需刷新与多源容灾重试。

## 🚀 快速启动

### 1. 配置环境变量

复制环境变量模版并填入配置：

```bash
cp .env.example .env
vim .env
```

### 2. 使用 Docker Compose 部署

```bash
docker compose up -d --build
```

### 3. 查看运行日志

```bash
docker compose logs -f zhuixin-bot
```

## 🛠 技术栈

- **Python 3.11+**
- **Aiogram 3.x** (Telegram Bot 框架)
- **Asyncpg & SQLite** (双引擎数据存储与状态持久化)
- **Aiohttp / Httpx** (异步 HTTP 客户端)
- **Docker & Docker Compose** (容器化快速编排)
