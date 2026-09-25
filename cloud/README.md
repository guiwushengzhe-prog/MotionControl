# MotionControl 云端服务

配置的上传、版本、回滚、下载和分享。桌面端和云端共用 `motioncontrol_shared/` 里的同一套校验器 —— 云端**没有**自己的一份配置规则。

## 依赖方向

```
desktop → motioncontrol_shared ← cloud
```

`cloud → desktop` 是禁止的，而且不是靠自觉：`output_backend.py` 第一行就是 `import ctypes.wintypes`，在 Linux 上直接抛异常，所以任何越界的 import 会在 Linux CI 上以真实导入失败的方式暴露出来。

## 本地跑起来

全部命令都在**仓库根目录**执行（`motioncontrol_shared` 在那里）。

```bash
pip install -r cloud/requirements.txt

export MC_DB_URL="sqlite+aiosqlite:///cloud/cloud.db"   # 相对仓库根目录
export MC_SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"

python -m alembic -c cloud/alembic.ini upgrade head   # 建表，独立一步
python -m cloud.tools.seed_games                      # 导入 200 个游戏
python -m cloud.tools.make_invite --note "自己用"      # 拿一个邀请码

cd cloud/web && npm install && npm run build && cd ../..   # 网站，构建一次即可
python -m uvicorn cloud.app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/> 就是网站，<http://127.0.0.1:8000/api/v1/docs> 是接口文档。`/api/v1/health` 会告诉你表建好了没：

```json
{"ok": true, "schema": "ready", "env": "dev", "games": 200, "profiles": 0}
```

## 三个 version 是三件事，不要混用

| 名字 | 含义 | 什么时候变 |
|---|---|---|
| `/api/v1` | HTTP 协议版本 | 接口不兼容地改了 |
| `schema_version` | 配置文件自身的结构，如 `motioncontrol.profile_selection.v2` | 配置格式变了 |
| `revision_no` | 某个配置的第几次修订，从 1 开始 | 用户每改一次 |

把任何两个合并，以后就没法单独改其中一个。

## 几个不打算改的设计

**版本不可变。** `profile_versions` 的行只写不改。编辑产生新行，回滚也产生新行（内容从旧行复制）。所以一个版本 ID 永远指向同一串字节，分享链接才敢给别人。

**存的是规范化后的字节，不是 JSON 对象。** 下载的文件必须和上传时记录的 `canonical_sha256` 对得上。存字节这件事天然成立；存 JSONB 则要依赖"每次重新序列化都逐字节相同"这个假设永远不破。`LargeBinary` 在 SQLite 和 PostgreSQL 上都是原生类型，不需要方言分支。以后想按内容检索，再加一个派生的 JSONB 列 —— 那是加法。

**并发修改报 409，绝不静默覆盖。** 客户端提交时要带上 `base_version_id`，也就是它编辑时所基于的版本。对不上就拒绝，并告诉它当前是哪个版本。数据库上还有 `uq_profile_revision` 唯一约束兜底，防止两个请求抢到同一个修订号。

**限流计数在请求事务之外提交。** 这条容易写错而且后果严重：请求抛异常时事务会回滚，如果计数在同一个事务里，密码错误导致的回滚会把计数一起抹掉 —— 于是限流只统计成功的尝试，等于完全不限制密码爆破。

**会话是服务端不透明会话，不是 JWT。** 需要能即时注销，而 JWT 要做到这点本来就得维护一张同样的表；这里也没有互不信任的服务需要联邦。Cookie 的值从不入库，入库的是它的 SHA-256。

**启动时不自动迁移。** 两个 worker 同时启动会互相竞争，而且自动跑的迁移是没人审过的迁移。`alembic upgrade head` 是独立的、明确的一步。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MC_DB_URL` | 本地 SQLite 文件 | 换 PostgreSQL 只改这一个值 |
| `MC_SECRET_KEY` | 开发时随机生成 | **生产必须设置**，否则每次重启所有人掉线 |
| `MC_ENV` | `dev` | `prod` 时关掉 `/docs`、启用 `__Host-` Cookie 和 HSTS |
| `MC_SITE_ORIGIN` | `http://127.0.0.1:8000` | 对外地址 |
| `MC_SESSION_DAYS` | `30` | 会话有效期 |
| `MC_MAX_BUNDLE_BYTES` | `262144` | 单份配置上限（实测约 6 KB，留了 40 倍余量） |
| `MC_ARGON2_MEMORY_KIB` | `65536` | 内存紧张的机器上调到 `32768` |

## 部署注意

**用 Caddy，不要装 nginx。** 计划书里原本写的是 nginx + certbot，但目标服务器上 Caddy 已经占着 80/443 并在服务 `guiwu-aware.icu`，再装 nginx 会抢端口。Caddy 自带自动 HTTPS，不需要 certbot。加一段即可：

```
motioncontrol.guiwu-aware.icu {
	reverse_proxy 127.0.0.1:8000
}
```

**uvicorn 只绑 `127.0.0.1`。** 公网唯一入口是 Caddy 的 443。绑定地址就是安全边界 —— 和桌面端 8765/8766 分面是同一个道理。

**`--forwarded-allow-ips=127.0.0.1`，绝不写 `"*"`。** 不配这条，每个请求看起来都来自 127.0.0.1，限流的分桶就全部失效。

## 网站

Vue 3 + TypeScript + Vite，页面有：登录/注册、我的配置、版本历史与回滚、公开浏览、官方动作库。

## 官方动作库

`/api/v1/pose-library` 列出官方发布的动作（名字、怎么做、火柴人示范、星级），`/api/v1/pose-library/<id>` 发一个动作的完整文件和签名。动作文件在仓库的 `cloud/official_poses/` 里，随部署上来，接口只读；签了名、签完没再改过的才发布（`python tools/sign_pose_library.py`）。云端不验签名本身，只核对内容和签名记录对得上——验签是电脑端的事，它只信自己内置的公钥。详见 [official_poses/README.md](official_poses/README.md)。

构建产物 `cloud/web/dist` **由 FastAPI 自己服务**，不交给 Caddy。这样页面和接口同源，
所以这个项目里没有任何 CORS 配置 —— 根本没有跨源的东西要放行，会话 Cookie 也不需要
SameSite 例外。

改前端时用 `cd cloud/web && npm run dev`（5173 端口，`/api` 代理到 8000）；
改完 `npm run build`，后端重启即生效。

vue-router 用 history 模式，所以 `/config/<id>` 是真实 URL，刷新和分享都要能打开。
后端有一条兜底路由把非 `/api` 的路径交给单页应用，那条路由带路径包含检查——
它从 URL 取路径去找文件，没有检查就会把 `settings.py` 交出去。

## 测试

```bash
python -m pytest cloud/tests/ -q
```

测试跑的是**本机真实的三个配置文件**（`config/` 下那三份），不是手写的玩具样本 —— 云端要能接住桌面实际写出来的东西，用简化样本测等于没测。测试前会先跑一遍 Alembic 迁移，所以迁移和模型对不上会在这里红，而不是等到上线那天。
