"""玩家反馈的收件口。

这个软件是发网盘和 GitHub Release 的，绝大多数用的人不会有账号——注册还要邀请
码。所以提交不要求登录：真正会卡住的新手，恰恰是最不可能为了说一句"我这儿打不
开"去走一遍注册流程的人。把门槛设在那里，等于没有反馈入口。

滥用靠限流和长度上限挡，不靠账号。读取要 is_admin，因为里面会有别人留下的联系
方式。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import select

from ..deps import (
    DbSession,
    MaybeUser,
    client_ip,
    enforce_rate_limit,
    rate_limit_bucket,
)
from ..models import Feedback
from ..schemas import FeedbackOut, FeedbackRequest
from ..security import keyed_digest

router = APIRouter(prefix="/feedback", tags=["feedback"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def submit(payload: FeedbackRequest, request: Request,
                 db: DbSession, user: MaybeUser) -> dict:
    """收下一条反馈。不需要登录。

    一小时 10 条：真心写反馈的人一次写完就走，连着发十条的基本只有脚本。这个
    数字宁可宽一点——把一个真的遇到问题的人挡在外面，比收几条垃圾糟糕得多。
    """
    await enforce_rate_limit(rate_limit_bucket(request, "feedback"),
                             limit=10, window_seconds=3600)

    row = Feedback(
        kind=payload.kind,
        message=payload.message,
        contact=payload.contact.strip(),
        app_version=payload.app_version.strip(),
        user_id=user.id if user else None,
        # 和限流同一个做法：只存带盐的摘要。同一个人刷屏看得出来，但这张表
        # 泄漏了也不等于泄漏一串 IP。
        ip_hash=keyed_digest(client_ip(request)),
    )
    db.add(row)
    await db.commit()
    # 不回 id：提交的人拿它没用，而它是一个能猜的东西。
    return {"ok": True}


@router.get("", response_model=list[FeedbackOut])
async def listing(db: DbSession, user: MaybeUser,
                  limit: int = Query(default=50, ge=1, le=200),
                  unhandled: bool = Query(default=False)) -> list[FeedbackOut]:
    """读反馈。只有管理员能读——里面有别人留下的联系方式。"""
    if user is None or not user.is_admin:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    query = select(Feedback).order_by(Feedback.created_at.desc())
    if unhandled:
        query = query.where(Feedback.handled_at.is_(None))
    rows = (await db.execute(query.limit(limit))).scalars().all()
    return [
        FeedbackOut(
            id=row.id,
            created_at=row.created_at,
            kind=row.kind,
            message=row.message,
            contact=row.contact,
            app_version=row.app_version,
            handled_at=row.handled_at,
            reply=row.reply,
        )
        for row in rows
    ]
