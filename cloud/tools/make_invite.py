"""Mint a registration invite.

    python -m cloud.tools.make_invite --note "给小王" --days 14

Run from the repository root. The code is printed once and only its SHA-256 is
stored, so it cannot be recovered afterwards -- if it is lost, mint another and
the old one simply goes unused.

This is a command-line tool rather than an admin endpoint because the first
account has to come from somewhere: an endpoint that creates invites needs an
authenticated admin, and there is no admin until someone has registered. Shell
access is the bootstrap, and it is one fewer public surface to defend.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta

from cloud.app.db import SessionLocal, engine, utcnow
from cloud.app.models import Invite
from cloud.app.security import new_token, token_digest


async def create(note: str, days: int | None) -> str:
    code = new_token()
    async with SessionLocal() as db:
        db.add(Invite(
            code_hash=token_digest(code),
            note=note[:120],
            expires_at=utcnow() + timedelta(days=days) if days else None,
        ))
        await db.commit()
    # Same reason as in seed_games: dispose or the process will not exit.
    await engine.dispose()
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="生成一个注册邀请码")
    parser.add_argument("--note", default="", help="备注，只给管理员看")
    parser.add_argument("--days", type=int, default=14,
                        help="有效天数，0 表示永不过期")
    args = parser.parse_args()

    code = asyncio.run(create(args.note, args.days or None))
    print("邀请码（只显示这一次）：")
    print(f"  {code}")
    print(f"有效期：{args.days} 天" if args.days else "有效期：永久")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
