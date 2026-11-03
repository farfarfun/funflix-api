# funflix-api

`funflix` 的 HTTP API 服务：依赖 [funflix](https://github.com/farfarfun/funflix)
提供的数据模型与流水线，把它暴露成一个可常驻运行的服务进程，被
[funflix-web](https://github.com/farfarfun/funflix-web) 反代消费。

数据采集、解析、校验、数据库迁移等领域逻辑都在 `funflix` 里，通过 `funflix` 自己
的 CLI（`funflix collect` / `parse` / `verify` / `worker` / `db upgrade` 等）管理，
本仓库不重复实现，也不依赖它们。

## 快速开始

```bash
scripts/setup.sh install-dev   # 从当前源码构建并安装

# 前台启动（开发用；默认端口 18810）
funflix-api server run --reload
# 接口文档 http://127.0.0.1:18810/docs

# 后台常驻、状态查询、停止、重启（生产用，本地一样适用）
funflix-api server start
funflix-api server status
funflix-api server stop
funflix-api server restart
```

## 命令行

| 命令 | 说明 |
| --- | --- |
| `funflix-api server run` | 前台启动 API 服务，Ctrl-C 停止；`--reload` 开发用 |
| `funflix-api server start` | 后台启动 API 服务：拉一个子进程跑 `server run` |
| `funflix-api server stop` | 停止后台服务（`SIGTERM` 优雅退出） |
| `funflix-api server restart` | 先 `stop` 再 `start` |
| `funflix-api server status` | 查看后台服务是否在跑、PID、安装的版本号 |
| `funflix-api upgrade [版本]` | 升级到最新版或指定版本 |
| `funflix-api rollback <版本>` | 回退到指定版本 |
| `funflix-api uninstall` | 停止服务后卸载包 |

各命令默认监听 `127.0.0.1:18810`，`--host`/`--port`/`--config` 可覆盖；`--config`
缺省时读 `~/.farfarfun/funflix/api/config.toml`（不存在
就用默认值，不算错误）。`start` 写的 PID 文件（`server.pid`）和日志（`server.log`）
都放在同一个配置目录下，跟 `--config` 默认路径统一管理。
`--reload` 使用默认配置路径，不支持同时指定自定义 `--config`。

源码仓库的 `scripts/setup.sh` 是上述命令的薄封装。`install-dev` 从当前工作树构建并
安装，`install-prod [版本]` 从包索引首次安装（省略版本且已安装时不升级），`publish`
调用 `funbuild build`；服务动作不区分 dev/prod，只运行当前已安装的 CLI。

若配置了 `FUNFLIX_WORKER_ENABLED=true`，服务进程会在启动时额外拉起一个进程内
后台 worker（周期性采集 → 解析 → 校验），行为与 `funflix worker` 等价。

## 配置

数据库、日志、worker 等领域配置仍由 `funflix` 管理（见
[funflix README](https://github.com/farfarfun/funflix#配置)）；API 的会话与 Casdoor
配置直接从 `~/.farfarfun/funflix/api/config.toml` 读取，不再读取对应的
`FUNFLIX_SESSION_*` / `FUNFLIX_OIDC_*` 环境变量。与 API 相关的共享配置有：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FUNFLIX_DATABASE_URL` | `sqlite+aiosqlite:///~/.farfarfun/funflix/funflix.db` | 切 PG 改成 `postgresql+asyncpg://...`；默认是绝对路径，换目录执行读写的也是同一份库 |
| `FUNFLIX_REGISTRATION_ENABLED` | `true` | 是否开放邀请码注册；启用 SSO 后本地注册入口会关闭 |
| `FUNFLIX_LOG_LEVEL` | `INFO` | |
| `FUNFLIX_WORKER_ENABLED` | `false` | 是否在服务进程内额外起一个后台 worker |

### Casdoor 单点登录

以下以 `id.farfarfun.com`（Casdoor）和 `flix.farfarfun.com`（Funflix）为例；
部署时全部替换成实际的 HTTPS 域名。

1. 在 Casdoor 为 Funflix 创建独立的 Application，启用 OIDC 授权码登录，将
   **Redirect URLs** 精确设为
   `https://flix.farfarfun.com/api/v1/auth/oidc/callback`，不要使用通配符。
   记下该 Application 的 Client ID 和 Client Secret；按需关联邮箱、短信、微信
   Provider。Client Secret 只交给后端，不写入前端。
2. 确认统一的 Casdoor `admin` 账号能够通过这个 Application 登录，尤其要核对
   Application 所属 Organization。记下该账号的用户 ID（实际 ID Token 的 `sub`）；
   `oidc_admin_subject` 不能填用户名、显示名或邮箱。
3. 打开 `https://id.farfarfun.com/.well-known/openid-configuration`，确认返回的
   `issuer` 是 `https://id.farfarfun.com`。确认 Funflix 域名下的回调路径能转发到 API。

在运行 API 的账号下创建 `~/.farfarfun/funflix/api/config.toml`：

```toml
host = "127.0.0.1"
port = 18810
oidc_issuer = "https://id.farfarfun.com"
oidc_client_id = "Casdoor Application 的 Client ID"
oidc_client_secret = "Casdoor Application 的 Client Secret"
oidc_redirect_uri = "https://flix.farfarfun.com/api/v1/auth/oidc/callback"
oidc_admin_subject = "Casdoor admin 用户的 ID Token sub"
oidc_timeout_seconds = 120
session_secret = "至少 32 字节的固定随机密钥"
session_max_age = 2592000
session_cookie_secure = true
```

可用 `openssl rand -hex 32` 生成 `session_secret`，并固定保存，避免重启后会话失效。
`oidc_timeout_seconds` 是 Casdoor discovery 与换 token 请求的超时秒数，默认 120；
身份服务响应仍慢时可适当调大。
配置文件仅用于 API；`funflix db upgrade` 仍按 `FUNFLIX_DATABASE_URL` / funsecret
连接数据库，必须与 API 使用同一数据库。先升级数据库，再重启服务：

```bash
chmod 600 ~/.farfarfun/funflix/api/config.toml
funflix db upgrade
fundeploy service funflix restart
```

访问 `/api/v1/auth/oidc/config`，应返回 `{"enabled":true}`；Funflix 登录页应显示
“使用统一账号登录”。分别用普通账号和统一 `admin` 账号试登录：普通账号首次登录会
创建无本地密码的 `guest` 用户，`admin` 账号获得 Funflix `admin` 权限。启用 SSO 后
本地密码登录和邀请码注册接口关闭；退出 Funflix 只清除本站会话，不会注销其他
Casdoor 应用的会话。尚未配置 SSO 时，现有本地登录方式保持可用。

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

搜索与浏览的主体是 **`Work`（一部剧）**，季挂在它下面 —— 搜「大主宰」返回
一条「大主宰（4 季 / 1496 资源）」，而不是几百条同名行。非影视类型
（`book` / `comic` / `other`）默认不进结果，要显式传 `media_type`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/works` | 搜索 / 浏览作品，支持 `keyword`、`media_type`、`year`、`valid_only`、`provider` 与翻页 |
| `GET` | `/api/v1/works/{id}` | 作品详情：季列表嵌套，每季带若干资源，标签跨季取并集 |
| `GET` | `/api/v1/media/{id}` | **季**级详情，含该季全部网盘资源与标签 |
| `GET` | `/api/v1/resources` | 按 `provider` / `check_status` 翻页看链接 |
| `GET` | `/api/v1/resources/{id}` | 单条资源 |
| `GET` | `/api/v1/stats` | 流水线各环节记录数与分布（`funflix status` 的 HTTP 版）|
| `GET` | `/healthz` | 健康检查：探一次数据库连通性 |

### 鉴权

`/works`、`/media` 等浏览接口要求登录；`/sources`、`/raw`、`/resources`、
`/stats` 还要求本地 `admin` 角色。认证后的请求使用 Funflix 的会话 cookie。

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

---

## 关于 farfarfun

[farfarfun](https://github.com/farfarfun) 是一个专注于实用工具库的开源组织，
涵盖云存储、数据处理、AI、多媒体与开发工具链等方向。

- 🏠 组织主页：<https://github.com/farfarfun>
- 📦 PyPI：<https://pypi.org/user/niuliangtao/>
- 📧 联系：farfarfun@qq.com

本项目基于 [MIT](LICENSE) 协议开源。
