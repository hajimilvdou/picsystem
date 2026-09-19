"""应用配置：全部通过环境变量注入，本地开发可用 .env。"""
from __future__ import annotations

import secrets

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    migrations_lock_timeout_seconds: float = 120.0
    # 置 true 时跳过标记为破坏性的迁移（删表 / 删列），谨慎使用
    migrations_skip_destructive: bool = False


settings = Settings()

# 开发模式下未配置密钥时生成临时密钥（重启后所有会话失效，仅用于本地开发）
EPHEMERAL_SECRET = False
if not settings.jwt_secret:
    settings.jwt_secret = secrets.token_hex(32)
    EPHEMERAL_SECRET = True
