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

# 在 PowerShell 里敲 `bash`，Windows 解析到的是 WSL 的 bash，不是 Git Bash。WSL
# 里没有 Linux 版 python——C:\Python313\python.exe 在它眼里叫 python.exe，`python`
# 找不到；就算绕过去，ssh 也会读 WSL 自己的 ~/.ssh，那里没有 aliyun 这个别名。
# 不拦的话表现是第 35 行一句 "python: command not found"，看不出跟 shell 有关，
# 人会以为 Python 没装。
if grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
    echo "这是 WSL 的 bash，它看不到 Windows 的 python 和 ssh 配置。" >&2
    echo "在 PowerShell 里改用 Git Bash 跑：" >&2
    echo '    & "C:\Program Files\Git\bin\bash.exe" cloud/deploy/push.sh' >&2
    exit 1
fi

# Windows 的 Python 默认按控制台编码往管道里写，这台机器是 GBK；而发布目录名里
# 有中文（build/release/电脑端/…），磁盘上是 UTF-8。不指定的话 bash 拿到的是一串
# GBK 字节，[ -d "$PC_DIR/app" ] 永远为假，于是更新包那一步被整个跳过——而它跳过
# 时打印的是"没有发布包，这次不带更新包"，听着像一句正常提示，没人会去查。
export PYTHONIOENCODING=utf-8

HOST=aliyun
APP_DIR=/opt/motioncontrol
SKIP_WEB=0
PHONE_WEB=""

while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --skip-web) SKIP_WEB=1; shift ;;
        # 同一个仓库开多个工作树之后，stage_release 那个 ../switch/mobile/dist 默认值
        # 指向的是别人那份构建产物。打出来的包能签名、能安装，只是手机上跑的是别人
        # 分支的网页，而没有任何地方会报错。所以这里必须能显式指定。
        --phone-web) PHONE_WEB="$2"; shift 2 ;;
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

echo "==> 电脑端更新包"
# 已经装了的人靠这一份更新，不用重下 174 MB。打包时会签名——电脑端拒绝没签名
# 的包，所以这一步失败就该停下，而不是发一份装不上的东西上去。
PC_DIR=$(python tools/release_paths.py pc_dir)
if [ -d "$PC_DIR/app" ]; then
    python tools/build_app_bundle.py ${PHONE_WEB:+--phone-web "$PHONE_WEB"} || {
        echo "更新包没做成，不部署" >&2; exit 1
    }
    rm -rf cloud/app_bundle
    cp -r build/app_bundle cloud/app_bundle
else
    echo "    没有发布包，这次不带更新包（已经在服务器上的那份保持不变）"
fi

echo "==> 打包并上传"
# 直接管道给 ssh，不落本地临时文件。在 Git Bash 里 /tmp 是一个 Windows 路径，
# 而 scp 是 Windows 的 OpenSSH——它不认识 /tmp/xxx 这种写法，会报
# "error encountered when reading a file"。管道没有这个问题，还少一次落盘。
#
# 打进去的东西：服务本身、共享校验器、200 个游戏配置（种子要用）。
# 挡在外面的：node_modules(65 MB)、字节码、任何数据库文件。
tar czf - \
    --exclude='__pycache__' \
    --exclude='node_modules' \
    --exclude='*.pyc' \
    --exclude='*.db' \
    --exclude='*.db-journal' \
    --exclude='.vite' \
    cloud motioncontrol_shared game_profiles \
  | ssh "$HOST" 'cat > /tmp/motioncontrol-cloud.tar.gz'
ssh "$HOST" 'echo "    $(du -h /tmp/motioncontrol-cloud.tar.gz | cut -f1)"'

echo "==> 在服务器上安装"
# 远端用退出码 90 表示"还没初始化"，那不是失败。set -e 会在非 0 时立刻结束整个
# 脚本，所以这里要显式关掉它来拿到退出码，否则下面的判断永远执行不到。
set +e
ssh "$HOST" "APP_DIR='$APP_DIR' bash -s" <<'REMOTE'
set -euo pipefail

# 第一次跑的时候这个目录还不存在，bootstrap.sh 也还没上来——它就在这个包里。
mkdir -p "$APP_DIR"
cd "$APP_DIR"

echo "    解包"
# --overwrite 而不是先删：删掉再解压之间服务是半坏的，覆盖则是逐文件替换。
tar xzf /tmp/motioncontrol-cloud.tar.gz -C "$APP_DIR" --overwrite
rm -f /tmp/motioncontrol-cloud.tar.gz
chown -R root:root "$APP_DIR/cloud" "$APP_DIR/motioncontrol_shared" "$APP_DIR/game_profiles"

# 还没初始化过：代码已经上来了，bootstrap.sh 现在就在 cloud/deploy/ 下。
# 这不是失败，所以干净退出，别让调用方以为出了错。
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    echo
    echo "    代码已上传，但这台机器还没初始化过。接下来跑一次："
    echo "      ssh <这台机器> 'cd $APP_DIR && sudo bash cloud/deploy/bootstrap.sh'"
    echo "    然后再跑一遍 push.sh。"
    exit 90
fi

echo "    依赖"
# 这台机器的 /etc/pip.conf 指向 mirrors.cloud.aliyuncs.com——那是阿里云中国内地
# 的内网镜像，而这台是海外节点（8.220.x），路由不到，pip 会卡满重试然后失败。
# 这里显式指定源，绕开系统配置而不是去改它：改系统配置影响这台机器上所有人的
# pip，超出部署脚本该管的范围。要换源就 PIP_INDEX_URL=... 传进来。
export PIP_INDEX_URL=${PIP_INDEX_URL:-https://pypi.org/simple/}
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
REMOTE_STATUS=$?
set -e

case "$REMOTE_STATUS" in
    0)  ;;
    90) exit 0 ;;   # 代码传上去了，等着跑 bootstrap.sh
    *)  echo "远端安装失败（退出码 $REMOTE_STATUS）" >&2; exit 1 ;;
esac

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
