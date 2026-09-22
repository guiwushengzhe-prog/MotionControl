"""Request and response shapes.

Responses are written out explicitly rather than serialising ORM objects, so
adding a column cannot accidentally publish it. ``password_hash`` and
``ip_hash`` are the obvious cases, but the rule is worth keeping for all of
them: what the API returns should be a decision, not a side effect.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from .security import MIN_PASSWORD_CHARS

# game_bundle 是"一个游戏的全部配置"：按键映射加身体动作，一份。前三种留着是
# 因为已经传上去的那些还得能下载和安装——改成开不了的废文件是最坏的一种升级。
DocType = Literal["profile_selection", "motion_mappings", "voice_mappings", "game_bundle"]
Visibility = Literal["private", "unlisted", "public"]


class RegisterRequest(BaseModel):
    invite_code: str = Field(min_length=8, max_length=128)
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_CHARS, max_length=256)
    display_name: str = Field(min_length=1, max_length=40)

    @field_validator("password")
    @classmethod
    def _not_trivial(cls, value: str) -> str:
        # A length floor is the one check that reliably helps. Composition
        # rules ("must contain a digit") mostly push people towards Password1!,
        # so the rest of the work is done by Argon2 and by rate limiting.
        if value.strip() != value:
            raise ValueError("密码不能以空格开头或结尾")
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    id: str
    display_name: str
    email: str
    is_admin: bool
    created_at: datetime


class GameOut(BaseModel):
    id: str
    name: str


class VersionOut(BaseModel):
    id: str
    revision_no: int
    schema_version: str
    canonical_sha256: str
    size_bytes: int
    parent_version_id: str | None
    restored_from_id: str | None
    note: str
    created_at: datetime


class ProfileOut(BaseModel):
    id: str
    doc_type: DocType
    game_id: str | None
    game_name: str | None
    title: str
    summary: str
    visibility: Visibility
    owner_id: str
    owner_name: str
    current_version: VersionOut | None
    created_at: datetime
    updated_at: datetime


class CreateProfileRequest(BaseModel):
    doc_type: DocType
    title: str = Field(min_length=1, max_length=120)
    # The document itself, exactly as read from disk. It is validated and
    # normalised server-side before anything is stored, so the client cannot
    # decide what counts as valid.
    document: Any
    game_id: str | None = Field(default=None, max_length=80)
    summary: str = Field(default="", max_length=2000)
    visibility: Visibility = "private"
    note: str = Field(default="", max_length=120)


class NewVersionRequest(BaseModel):
    document: Any
    # The version the editor started from. Omitting it means "I did not read
    # the current one", which is refused for an existing profile rather than
    # silently overwriting whatever is there now.
    base_version_id: str | None = None
    note: str = Field(default="", max_length=120)


class RollbackRequest(BaseModel):
    version_id: str
    note: str = Field(default="", max_length=120)


class UpdateProfileRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    summary: str | None = Field(default=None, max_length=2000)
    visibility: Visibility | None = None
    game_id: str | None = Field(default=None, max_length=80)


class InviteOut(BaseModel):
    id: str
    code: str
    note: str
    expires_at: datetime | None


class InviteStatusOut(BaseModel):
    can_create: bool
    next_available_at: datetime
    daily_limit: int
    expires_days: int


FeedbackKind = Literal["bug", "idea", "question", "other"]


class FeedbackRequest(BaseModel):
    kind: FeedbackKind = "other"
    # 4000 字够写清楚一个问题了，再长基本是粘贴了整个日志。上限存在是为了让
    # 一次请求不可能塞满一张表，不是为了拦住话多的人。
    message: str = Field(min_length=1, max_length=4000)
    # 选填，不校验格式：邮箱、QQ、微信都行。强制邮箱只会让一部分人干脆不填。
    contact: str = Field(default="", max_length=120)
    app_version: str = Field(default="", max_length=32)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("说点什么吧")
        return text


class FeedbackOut(BaseModel):
    id: str
    created_at: datetime
    kind: str
    message: str
    contact: str
    app_version: str
    handled_at: datetime | None
    reply: str
