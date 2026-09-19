"""应用配置：全部通过环境变量注入，本地开发可用 .env。"""
from __future__ import annotations

import secrets

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 单张图片允许解码的最大像素数（5000 万）。Pillow 默认只对超过 ~8900 万像素告警，
# 而超大图解码会瞬间吃掉几百 MB 内存——这里改成硬上限，缩略图与压缩共用。
MAX_DECODE_PIXELS = 50_000_000

# 迁移相关默认值（.env 里写了键但留空时回落到这里，见下面的校验器）
DEFAULT_MIGRATIONS_LOCK_TIMEOUT = 120.0
DEFAULT_MIGRATIONS_SKIP_DESTRUCTIVE = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 数据库：默认本地 SQLite（仅开发用）；compose 部署注入 PostgreSQL
    database_url: str = "sqlite+aiosqlite:///./data/picsystem.db"

    # 上游 chatgpt2api
    upstream_base_url: str = "http://localhost:3000"
    upstream_api_key: str = ""

    # 会话
    jwt_secret: str = ""
    jwt_expire_minutes: int = 60 * 24 * 7
    cookie_name: str = "pic_session"
    cookie_secure: bool = False

    # 初始管理员（仅首次初始化使用）
    admin_username: str = "admin"
    admin_password: str = ""

    # 站点
    site_name: str = "PicSystem"

    # 文件存储
    data_dir: str = "./data"
    max_upload_mb: int = 20

    # 对话上下文携带的最大历史消息数
    chat_history_limit: int = 40

    # 单个 API 密钥同时在途请求上限
    max_inflight_per_key: int = 6

    # 上游请求超时（秒）：连接 / 读（生图与 PPT 轮询较长）
    upstream_connect_timeout: float = 10.0
    upstream_read_timeout: float = 300.0

    # 数据库迁移（详见 docs/MIGRATIONS.md）
    # 多实例部署时等待迁移锁的上限（秒）
    migrations_lock_timeout_seconds: float = DEFAULT_MIGRATIONS_LOCK_TIMEOUT
    # 置 true 时跳过标记为破坏性的迁移（删表 / 删列），谨慎使用
    migrations_skip_destructive: bool = DEFAULT_MIGRATIONS_SKIP_DESTRUCTIVE

    # `.env` 里写了键但没填值（`KEY=`）时回落到默认值：否则空字符串会让 pydantic
    # 直接校验失败、应用启动即崩——对「取消注释照着填」的运维习惯来说这个坑代价太大。
    @field_validator("migrations_lock_timeout_seconds", mode="before")
    @classmethod
    def _lock_timeout_blank(cls, value):
        if isinstance(value, str) and not value.strip():
            return DEFAULT_MIGRATIONS_LOCK_TIMEOUT
        return value

    @field_validator("migrations_skip_destructive", mode="before")
    @classmethod
    def _skip_destructive_blank(cls, value):
        if isinstance(value, str) and not value.strip():
            return DEFAULT_MIGRATIONS_SKIP_DESTRUCTIVE
        return value


settings = Settings()

# 开发模式下未配置密钥时生成临时密钥（重启后所有会话失效，仅用于本地开发）
EPHEMERAL_SECRET = False
if not settings.jwt_secret:
    settings.jwt_secret = secrets.token_hex(32)
    EPHEMERAL_SECRET = True
