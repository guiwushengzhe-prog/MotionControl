#!/usr/bin/env bash
# 在本机的仓库根目录跑，把当前代码部署上去。
#
#     bash cloud/deploy/push.sh
#     bash cloud/deploy/push.sh --host aliyun          # 换一台机器
#     bash cloud/deploy/push.sh --skip-web             # 只改了后端，不重建网站
#
# 顺序是刻意的：先构建、先打包、先上传，最后才动正在跑的服务。任何一步失败，
# 线上那份还是原样。数据库迁移排在重启之前——反过来的话，新代码会对着旧表结构
# 启动，看着正常，第一个真实请求才崩。
#
# 网站在本机构建，只传产物。服务器只有 1.6 GB 内存，让 vite 在上面跑是拿几百兆
# 换一个 117 KB 的文件。

set -euo pipefail

HOST=aliyun
APP_DIR=/opt/motioncontrol
SKIP_WEB=0

while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --skip-web) SKIP_WEB=1; shift ;;
        *) echo "不认识的参数：$1" >&2; exit 1 ;;
    esac
done

cd "$(dirname "$0")/../.."
REPO=$(pwd)
[ -d "$REPO/motioncontrol_shared" ] || { echo "不在仓库根目录？" >&2; exit 1; }

echo "==> 本地测试"
# 传上去之前先确认它是好的。装一个跑不起来的版本，代价是线上停摆。
python -m pytest cloud/tests/ -q || { echo "测试没过，不部署" >&2; exit 1; }

if [ "$SKIP_WEB" -eq 0 ]; then
    echo "==> 构建网站"
    (cd cloud/web && npm run build)
fi
[ -f cloud/web/dist/index.html ] || {
    echo "cloud/web/dist 里没有构建产物，先跑一次不带 --skip-web 的" >&2
    exit 1
}

echo "==> 打包"
STAMP=$(date +%Y%m%d-%H%M%S)
TARBALL="/tmp/motioncontrol-cloud-$STAMP.tar.gz"
# 打进去的东西：服务本身、共享校验器、200 个游戏配置（种子要用）。
# 挡在外面的：node_modules(65 MB)、字节码、任何数据库文件。
tar czf "$TARBALL" \
    --exclude='__pycache__' \
    --exclude='node_modules' \
    --exclude='*.pyc' \
    --exclude='*.db' \
    --exclude='*.db-journal' \
    --exclude='.vite' \
    cloud motioncontrol_shared game_profiles
echo "    $(du -h "$TARBALL" | cut -f1)"

echo "==> 上传"
scp -q "$TARBALL" "$HOST:/tmp/motioncontrol-cloud.tar.gz"
rm -f "$TARBALL"

echo "==> 在服务器上安装"
ssh "$HOST" "APP_DIR='$APP_DIR' bash -s" <<'REMOTE'
set -euo pipefail
cd "$APP_DIR"

echo "    解包"
# --overwrite 而不是先删：删掉再解压之间服务是半坏的，覆盖则是逐文件替换。
tar xzf /tmp/motioncontrol-cloud.tar.gz -C "$APP_DIR" --overwrite
rm -f /tmp/motioncontrol-cloud.tar.gz
chown -R root:root "$APP_DIR/cloud" "$APP_DIR/motioncontrol_shared" "$APP_DIR/game_profiles"

echo "    依赖"
venv/bin/pip install --quiet --upgrade -r cloud/requirements.txt

echo "    数据库迁移"
# 排在重启前面。反过来的话新代码会对着旧表结构启动，第一个真实请求才崩。
# 用服务账号跑，否则 SQLite 文件会变成 root 所有，服务随后写不进去。
set -a; . /etc/motioncontrol-cloud.env; set +a
sudo -u mccloud --preserve-env=MC_DB_URL,MC_SECRET_KEY,MC_ENV \
    venv/bin/python -m alembic -c cloud/alembic.ini upgrade head

echo "    同步游戏库"
sudo -u mccloud --preserve-env=MC_DB_URL,MC_SECRET_KEY,MC_ENV \
    venv/bin/python -m cloud.tools.seed_games

echo "    systemd 单元"
install -m 644 cloud/deploy/motioncontrol-cloud.service \
    /etc/systemd/system/motioncontrol-cloud.service
systemctl daemon-reload

echo "    重启服务"
systemctl restart motioncontrol-cloud.service
REMOTE

echo "==> 检查"
# 起来要几秒。失败的话下面的 health 会说话，不用盲等。
sleep 4
HEALTH=$(ssh "$HOST" 'curl -s --max-time 8 http://127.0.0.1:8000/api/v1/health' || true)
echo "    $HEALTH"

case "$HEALTH" in
    *'"ok":true'*)
        echo
        echo "部署完成。"
        echo "  https://motioncontrol.guiwu-aware.icu/"
        ;;
    *)
        echo
        echo "服务没有正常应答。看日志：" >&2
        echo "  ssh $HOST 'journalctl -u motioncontrol-cloud -n 50 --no-pager'" >&2
        exit 1
        ;;
esac
