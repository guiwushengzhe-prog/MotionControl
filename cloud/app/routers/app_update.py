"""把电脑端的程序本体发给已经装了的人。

发布包 174 MB，其中运行时和模型占了 99%，几乎永不变。真正会改的 app/ 只有 2 MB
出头。没有这条通道，每修一个小问题都要让所有人重下 174 MB。

服务端只做两件事：报清单、发文件。校验、暂存、换包、退回都在电脑端，因为那些是
关于"这台机器上的那份程序"的决定，服务器不该也不能替它做。

复用 ModelShare：它本来就是"把一个目录当清单发出去"，电脑端发模型给手机用的就是
它。逐文件 sha256、整体 digest、断了能续，这些都是现成的。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse

from motioncontrol_shared.model_share import ModelShare

router = APIRouter(prefix="/app-update", tags=["app-update"])

# 部署时由 push.sh 放上来。不存在就是"没有可发的更新"，不是错误——自建的人多半
# 不会去准备这个目录。
BUNDLE_DIR = Path(__file__).resolve().parent.parent.parent / "app_bundle"

_share = ModelShare("pc-app", BUNDLE_DIR if BUNDLE_DIR.is_dir() else None)

# 手机 App 本身（APK）。网页部分随电脑热更，原生部分只能换安装包；手机在 App 里
# 验签、下载，再交给系统安装器，装不装由人确认。同样是可选的：没有这个目录，
# 手机得到的就是"没有新版"。由 push.sh --apk 放上来（tools/build_apk_bundle.py）。
ANDROID_DIR = Path(__file__).resolve().parent.parent.parent / "apk_bundle"

_android = ModelShare("android-apk", ANDROID_DIR if ANDROID_DIR.is_dir() else None)


@router.get("/pc")
async def manifest() -> dict:
    """这一份里有哪些文件、各自多大、各自的 sha256，以及整体签名。"""
    return _share.manifest()


@router.get("/pc/file")
async def one_file(path: str = Query(..., max_length=400)) -> FileResponse:
    """发一个文件。

    路径不靠清洗来防穿越：只有出现在清单里的路径才发得出去，清单是这个进程自己
    走目录生成的，所以不在那张表里的东西根本不存在。
    """
    resolved = _share.resolve(path)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return FileResponse(resolved)


@router.get("/android")
async def android_manifest() -> dict:
    """新版 APK 的清单：MotionControl.apk 和 apk.json（版本号与更新日志），连同签名。"""
    return _android.manifest()


@router.get("/android/file")
async def android_file(path: str = Query(..., max_length=400)) -> FileResponse:
    """发 APK 或它的说明。同上：只发清单里列出来的路径。"""
    resolved = _android.resolve(path)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return FileResponse(resolved)
