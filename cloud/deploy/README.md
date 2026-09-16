# 部署

目标：`https://motioncontrol.guiwu-aware.icu`，开机自启、崩了自动重启、HTTPS 自动续期。

四个文件：

| 文件 | 在哪跑 | 干什么 |
|---|---|---|
| `bootstrap.sh` | 服务器，**跑一次** | 建专用用户、建目录、生成密钥、装 systemd 单元 |
| `push.sh` | 本机，**每次更新** | 构建网站 → 打包 → 上传 → 迁移数据库 → 重启 |
| `motioncontrol-cloud.service` | 服务器 | systemd 单元，由上面两个脚本安装 |
| `caddy-snippet.txt` | 服务器 | 要手动加到 `/etc/caddy/Caddyfile` 的那段 |

## 第一次

**1. DNS**

在阿里云 DNS 里加一条 A 记录：

```
motioncontrol.guiwu-aware.icu  →  8.220.206.237
```

先做这一步。Caddy 是等到有人**第一次访问**这个域名时才去签证书的，DNS 没生效会签失败。

**2. 上传代码**

本机仓库根目录：

```bash
bash cloud/deploy/push.sh
```

第一次会在最后报错（systemd 单元还没装），正常，继续下一步。

**3. 服务器上初始化**

```bash
ssh aliyun
cd /opt/motioncontrol
sudo bash cloud/deploy/bootstrap.sh
```

`MC_SECRET_KEY` 在这一步由服务器自己生成，写进 `/etc/motioncontrol-cloud.env`（权限 600，只有 root 读得到）。它不会打印出来，你也不需要记——**但也不要删那个文件**，删了所有人的登录状态立刻失效。

**4. 再跑一次 push.sh**

```bash
bash cloud/deploy/push.sh
```

这次会跑完迁移、种子、重启，最后打印 `{"ok":true,...}`。

**5. Caddy**

```bash
ssh aliyun
sudo tee -a /etc/caddy/Caddyfile < /opt/motioncontrol/cloud/deploy/caddy-snippet.txt
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
```

**6. 第一个邀请码**

```bash
cd /opt/motioncontrol
sudo -u mccloud venv/bin/python -m cloud.tools.make_invite --note "自己用"
```

**只显示这一次**，存不下来就再生成一个。

## 以后更新

```bash
bash cloud/deploy/push.sh
```

只改了后端没动网站的话加 `--skip-web`，省掉一次 vite 构建。

顺序是刻意的：**先测试，再构建，再上传，最后才动正在跑的服务**。任何一步失败，线上那份还是原样。数据库迁移排在重启之前——反过来的话，新代码会对着旧表结构启动，看着一切正常，第一个真实请求才崩。

## 出问题时

```bash
ssh aliyun 'journalctl -u motioncontrol-cloud -n 50 --no-pager'
ssh aliyun 'systemctl status motioncontrol-cloud'
ssh aliyun 'curl -s http://127.0.0.1:8000/api/v1/health'
```

`health` 直接说表建好了没：

```json
{"ok": true, "schema": "ready", "env": "prod", "games": 200, "profiles": 0}
```

`"schema": "missing"` 就是迁移没跑成。

**如果服务根本起不来**，第一个要怀疑的是单元文件里的 `MemoryDenyWriteExecute=true`。它禁止内存页同时可写可执行，是很有用的加固，但极少数 C 扩展（这里可能是 argon2 走的 cffi）需要那种页面。日志里会是段错误或 `Operation not permitted`。确认方法：

```bash
sudo systemctl edit motioncontrol-cloud
# 写进去：
#   [Service]
#   MemoryDenyWriteExecute=false
sudo systemctl restart motioncontrol-cloud
```

起来了就说明是它。这条是整个单元里唯一有兼容性风险的加固，其余的对 Python 服务都是安全的。

## 几条不打算改的

**只绑 `127.0.0.1:8000`。** 公网唯一入口是 Caddy 的 443。绑定地址就是安全边界——和桌面端 8765/8766 分面同一个道理。

**`--forwarded-allow-ips=127.0.0.1`，绝不写 `"*"`。** 不配这条，每个请求看起来都来自本机，限流的分桶全部失效，等于没有限流。

**不要装 nginx。** Caddy 已经占着 80/443 服务 `guiwu-aware.icu`，再装一个会抢端口。Caddy 自带自动 HTTPS，不需要 certbot。计划书里原本写的是 nginx + certbot，那是在知道这台机器情况之前写的。

**服务用自己的账号 `mccloud` 跑，不是 root。** 它处理来自公网的上传；万一那条路径上有能被利用的东西，代价应该是一个目录，而不是这台同时跑着 Caddy、游戏面板和 FamilyGuard 的机器。

**代码目录对服务是只读的。** `ProtectSystem=strict` 把整个文件系统设成只读，只有 `/var/lib/motioncontrol`（数据库）通过 `ReadWritePaths` 开了写权限。服务改不了自己的代码。

**网站在本机构建，只传产物。** 服务器只有 1.6 GB 内存，让 vite 在上面跑是拿几百兆换一个 117 KB 的文件。

## 备份

数据库是一个文件：`/var/lib/motioncontrol/cloud.db`。

```bash
ssh aliyun 'sudo -u mccloud sqlite3 /var/lib/motioncontrol/cloud.db ".backup /tmp/cloud-backup.db"'
scp aliyun:/tmp/cloud-backup.db ./cloud-backup-$(date +%Y%m%d).db
```

用 `.backup` 而不是直接 `cp`：SQLite 在写入过程中被复制会得到一个损坏的文件，`.backup` 会正确地加锁。

真正的恢复演练——新建空库、恢复、`alembic current` 检查版本、抽查数据、起服务、实际下载一个配置——**还没做过**。做过一次才算数。
