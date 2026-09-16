# funflix-api

`funflix` 的 HTTP API 服务：依赖 [funflix](https://github.com/farfarfun/funflix)
提供的数据模型与流水线，把它暴露成一个可常驻运行的服务进程，被
[funflix-web](https://github.com/farfarfun/funflix-web) 反代消费。

数据采集、解析、校验、数据库迁移等领域逻辑都在 `funflix` 里，通过 `funflix` 自己
的 CLI（`funflix collect` / `parse` / `verify` / `worker` / `db upgrade` 等）管理，
本仓库不重复实现，也不依赖它们。

## 快速开始

```bash
funbuild install   # 本地构建并安装（生产发布用 funbuild build）

# 前台启动（开发用；默认端口 18810）
funflix-api run --reload
# 接口文档 http://127.0.0.1:18810/docs

# 后台常驻、状态查询、停止、重启（生产用，本地一样适用）
funflix-api start
funflix-api status
funflix-api stop
funflix-api restart
```

## 命令行

| 命令 | 说明 |
| --- | --- |
| `funflix-api run` | 前台启动 API 服务，Ctrl-C 停止；`--reload` 开发用 |
| `funflix-api start` | 后台启动 API 服务：拉一个子进程跑 `run` |
| `funflix-api stop` | 停止后台服务（`SIGTERM` 优雅退出） |
| `funflix-api restart` | 先 `stop` 再 `start` |
| `funflix-api status` | 查看后台服务是否在跑、PID、安装的版本号 |

各命令默认监听 `127.0.0.1:18810`，`--host`/`--port`/`--config` 可覆盖；`--config`
缺省时读 `${XDG_CONFIG_HOME:-~/.config}/farfarfun/funflix-api/config.toml`（不存在
就用默认值，不算错误）。`start` 写的 PID 文件（`server.pid`）和日志（`server.log`）
都放在同一个配置目录下，跟 `--config` 默认路径统一管理。

若配置了 `FUNFLIX_WORKER_ENABLED=true`，服务进程会在启动时额外拉起一个进程内
后台 worker（周期性采集 → 解析 → 校验），行为与 `funflix worker` 等价。

## 配置

配置项与 `funflix` 共用同一套 `FUNFLIX_` 前缀环境变量（见
[funflix README](https://github.com/farfarfun/funflix#配置)），其中与本服务直接
相关的有：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FUNFLIX_DATABASE_URL` | `sqlite+aiosqlite:///./funflix.db` | 切 PG 改成 `postgresql+asyncpg://...` |
| `FUNFLIX_SESSION_SECRET` | 随机（每次重启变化） | 会话 cookie 签名密钥；多进程/需跨重启保留会话的部署必须固定配置 |
| `FUNFLIX_SESSION_MAX_AGE` | `2592000`（30 天） | 会话 cookie 有效期（秒） |
| `FUNFLIX_SESSION_COOKIE_SECURE` | `false` | 会话 cookie 是否加 Secure 标记；确认部署链路全程 HTTPS 后再打开 |
| `FUNFLIX_REGISTRATION_ENABLED` | `false` | 是否开放自助注册；默认关闭，账号由 `funflix user create` 创建 |
| `FUNFLIX_LOG_LEVEL` | `INFO` | |
| `FUNFLIX_WORKER_ENABLED` | `false` | 是否在服务进程内额外起一个后台 worker |

## 接口

### 采集源

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/sources` | 登记采集源，只给 `url` 即可自动识别类型与标识 |
| `GET` | `/api/v1/sources` | 列表 |
| `GET` | `/api/v1/sources/supported` | 当前支持的源类型 |
| `PATCH` | `/api/v1/sources/{id}` | 改配置；回拨 `cursor_message_id` 即可重采历史 |
| `POST` | `/api/v1/sources/{id}/collect` | 立即采集一次 |
| `DELETE` | `/api/v1/sources/{id}` | 删除（已采文本保留） |

### 原始文本

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/raw` | 提交一条；命中 `content_hash` 返回 `duplicated=true` |
| `POST` | `/api/v1/raw/bulk` | 批量提交 |
| `GET` | `/api/v1/raw` | 按状态 / 来源翻页，不返回全文 |
| `GET` | `/api/v1/raw/{id}` | 详情，含全文 |

### 查询

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/media` | 搜索 / 浏览作品，支持 `keyword`、`media_type`、`year`、`valid_only` 与翻页 |
| `GET` | `/api/v1/media/{id}` | 作品详情，含全部网盘资源与标签 |
| `GET` | `/api/v1/resources` | 按 `provider` / `check_status` 翻页看链接 |
| `GET` | `/api/v1/resources/{id}` | 单条资源 |
| `GET` | `/api/v1/stats` | 流水线各环节记录数与分布（`funflix status` 的 HTTP 版）|
| `GET` | `/healthz` | 健康检查：探一次数据库连通性 |

### 鉴权

`/sources`、`/raw`、`/resources`、`/stats` 整个「运维」区都要求登录（基于会话
cookie）；`/media` 与 `/media/{id}` 保持开放，面向使用者。

先用 `funflix` 的 CLI 建一个账号（自助注册默认关闭，见上面的
`FUNFLIX_REGISTRATION_ENABLED`）：

```bash
funflix user create funflix --password funflix
```

再走登录接口拿会话 cookie：

```bash
curl -c cookies.txt -X POST localhost:18810/api/v1/auth/login \
     -H "Content-Type: application/json" \
     -d '{"username": "funflix", "password": "funflix"}'

curl -b cookies.txt -X POST localhost:18810/api/v1/sources \
     -d '{"url": "https://t.me/s/某频道"}'
```

## 开发

```bash
uv sync
uv run pytest
```

测试依赖本地未发布的 `funflix` 时，先把它装成可编辑依赖：

```bash
uv pip install -e ../funflix
```
