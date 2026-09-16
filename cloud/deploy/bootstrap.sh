#!/usr/bin/env bash
# 服务器上跑一次，把机器准备好。之后每次更新用 push.sh，不用再跑这个。
#
#     sudo bash bootstrap.sh
#
# 它做四件事：建一个专用用户、建两个目录、生成密钥、装好 systemd 单元。
# 每一步都是幂等的——重复跑不会覆盖已有的密钥，也不会清掉数据库。

set -euo pipefail

APP_DIR=/opt/motioncontrol
DATA_DIR=/var/lib/motioncontrol
ENV_FILE=/etc/motioncontrol-cloud.env
SERVICE_USER=mccloud
UNIT=/etc/systemd/system/motioncontrol-cloud.service
SITE_ORIGIN=${SITE_ORIGIN:-https://motioncontrol.guiwu-aware.icu}
# 这台机器的 /etc/pip.conf 指向 mirrors.cloud.aliyuncs.com——那是阿里云中国内地
# 的内网镜像，而这台是海外节点（8.220.x），路由不到，pip 会卡满重试然后失败。
# 这里显式指定源，绕开系统配置而不是去改它：改系统配置影响这台机器上所有人的
# pip，超出部署脚本该管的范围。要换源就 PIP_INDEX_URL=... 传进来。
export PIP_INDEX_URL=${PIP_INDEX_URL:-https://pypi.org/simple/}

if [ "$(id -u)" -ne 0 ]; then
    echo "要用 root 跑：sudo bash bootstrap.sh" >&2
    exit 1
fi

echo "==> 检查 python3"
python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version' || {
    echo "需要 Python 3.11 以上（代码用了 X | None 这类写法）" >&2
    exit 1
}
python3 -c 'import venv' 2>/dev/null || {
    echo "缺 python3-venv，先装：apt install python3-venv" >&2
    exit 1
}

echo "==> 服务账号 $SERVICE_USER"
# 系统账号：不能登录、没有家目录。它只用来跑一个进程。
if id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "    已存在"
else
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "    已创建"
fi

echo "==> 目录"
mkdir -p "$APP_DIR" "$DATA_DIR"
# 代码归 root，服务只读；数据归服务，它只能写这一个地方。
chown root:root "$APP_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chmod 755 "$APP_DIR"
chmod 750 "$DATA_DIR"

echo "==> 密钥与环境变量"
if [ -f "$ENV_FILE" ]; then
    echo "    $ENV_FILE 已存在，保持不动"
    echo "    （覆盖它会让所有人的登录状态立刻失效，所以这里不覆盖）"
else
    # 密钥在这台机器上生成，不经过任何别的地方。
    SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
    cat > "$ENV_FILE" <<EOF
# MotionControl 云端服务的环境变量。这个文件是 600，只有 root 读得到。
MC_ENV=prod
MC_SECRET_KEY=$SECRET
MC_SITE_ORIGIN=$SITE_ORIGIN
MC_DB_URL=sqlite+aiosqlite:///$DATA_DIR/cloud.db
# 这台机器内存紧，Argon2 从默认的 64 MiB 降到 32 MiB。
# 换了内存更大的机器可以改回 65536。
MC_ARGON2_MEMORY_KIB=32768
EOF
    chown root:root "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "    已生成，密钥没有打印出来也不需要你记"
fi

echo "==> Python 虚拟环境"
if [ -x "$APP_DIR/venv/bin/python" ]; then
    echo "    已存在"
else
    python3 -m venv "$APP_DIR/venv"
    "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
    echo "    已创建"
fi

echo "==> systemd 单元"
if [ -f "$APP_DIR/cloud/deploy/motioncontrol-cloud.service" ]; then
    install -m 644 "$APP_DIR/cloud/deploy/motioncontrol-cloud.service" "$UNIT"
    systemctl daemon-reload
    systemctl enable motioncontrol-cloud.service >/dev/null
    echo "    已安装并设为开机启动"
else
    echo "    还没有代码，先跑一次 push.sh 再回来跑这一步"
fi

cat <<'DONE'

==> 准备好了。接下来：

  1. 在本机（仓库根目录）跑：  bash cloud/deploy/push.sh
     它会构建网站、打包、上传、迁移数据库、重启服务。

  2. 把 cloud/deploy/caddy-snippet.txt 的内容加到 /etc/caddy/Caddyfile 末尾，
     然后：  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
             systemctl reload caddy

  3. DNS 加一条 A 记录：motioncontrol.guiwu-aware.icu -> 8.220.206.237
     （Caddy 是等到有人第一次访问才去签证书的，DNS 没生效会签失败）

  4. 建第一个邀请码：
       cd /opt/motioncontrol
       sudo -u mccloud venv/bin/python -m cloud.tools.make_invite --note "自己用"

DONE
