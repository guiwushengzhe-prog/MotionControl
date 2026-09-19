"""看看有哪些账号，给谁开管理员。

    python -m cloud.tools.set_admin                       # 列出所有账号
    python -m cloud.tools.set_admin --grant a@b.com       # 开
    python -m cloud.tools.set_admin --revoke a@b.com      # 收回

在仓库根目录跑。和 make_invite 一样是命令行工具而不是接口：第一个管理员必须从
某个地方来，而"能开管理员的接口"本身需要一个管理员先存在。有 shell 就够了，也
少一个对外暴露的面。

管理员现在只多一件事：读得到 /api/v1/feedback。那张表里有别人留下的邮箱和 QQ，
所以这个开关不是装饰。开之前想一下这个账号的密码有多结实。
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from cloud.app.db import SessionLocal, engine
from cloud.app.models import User, UserEmail


async def listing() -> int:
    async with SessionLocal() as db:
        users = (await db.execute(select(User).order_by(User.created_at))).scalars().all()
        print(f"共 {len(users)} 个账号")
        for user in users:
            rows = (await db.execute(
                select(UserEmail).where(UserEmail.user_id == user.id))).scalars().all()
            mail = ", ".join(row.email for row in rows) or "(没有邮箱)"
            mark = "管理员" if user.is_admin else "普通"
            print(f"  [{mark}] {user.display_name}  {mail}")
    await engine.dispose()
    return 0


async def change(email: str, admin: bool) -> int:
    wanted = email.strip().lower()
    async with SessionLocal() as db:
        row = (await db.execute(
            select(UserEmail).where(UserEmail.email == wanted))).scalars().first()
        if row is None:
            print(f"没有这个邮箱的账号：{wanted}")
            await engine.dispose()
            return 1
        user = await db.get(User, row.user_id)
        if user.is_admin == admin:
            print(f"{user.display_name} 本来就是{'管理员' if admin else '普通账号'}，没改动")
            await engine.dispose()
            return 0
        user.is_admin = admin
        await db.commit()
        print(f"{user.display_name}（{wanted}）已{'设为管理员' if admin else '取消管理员'}")
    await engine.dispose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="查看账号、开关管理员")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--grant", metavar="邮箱", help="把这个账号设为管理员")
    group.add_argument("--revoke", metavar="邮箱", help="取消这个账号的管理员")
    args = parser.parse_args()

    if args.grant:
        return asyncio.run(change(args.grant, True))
    if args.revoke:
        return asyncio.run(change(args.revoke, False))
    return asyncio.run(listing())


if __name__ == "__main__":
    raise SystemExit(main())
