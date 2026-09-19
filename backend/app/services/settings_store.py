"""站点设置存取（settings 表，JSON 值）。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Setting

DEFAULT_SETTINGS: dict[str, object] = {
    "feature_chat_enabled": True,
    "feature_image_enabled": True,
    "feature_search_enabled": True,
    "feature_ppt_enabled": True,
    "registration_enabled": True,
    "site_name": "PicSystem",
    "announcement": "",
    # 上游覆盖配置；为空则使用环境变量
    "upstream_base_url": "",
    "upstream_api_key": "",
    # 风控阈值
    "user_rate_limit_per_minute": 20,  # 单用户每分钟功能调用上限（网页端）
    "login_max_failures": 5,           # 连续登录失败达到该次数即临时锁定
    "login_lock_minutes": 15,          # 登录锁定时长（分钟）
    "register_max_per_ip_day": 10,     # 单 IP 每日注册上限
    # 存储与清理
    "storage_quota_mb_default": 100,   # 单用户存储上限（MB），0 = 不限；用户级可覆盖
    "file_retention_hours": 24,        # 产物保留小时数，0 = 永久保存；24 即一天后自动删除
    "log_retention_hours": 168,        # 调用/风控日志保留小时数（默认 7 天）
    "audit_retention_days": 90,        # 审计日志保留天数
    # 注册风控
    "register_max_per_fp_total": 2,    # 同一设备指纹最多注册的账号总数
    "registration_require_approval": False,  # 注册是否需要管理员人工审核
    # 免责协议：用户 agreement_version 低于此值时须重新阅读同意
    "agreement_version": 1,
    "agreement_text": "",
    # 公告弹窗：notice_text 非空且用户 notice_version 低于此值时弹一次
    "notice_version": 1,
    "notice_text": "",
    # 内容安全钩子：命中关键词即拦截（仅记命中词，不留存原文）
    "content_filter_enabled": True,
    "content_filter_keywords": "",
    # 每日签到
    "checkin_enabled": False,
    "checkin_pool": "permanent",       # permanent / temporary
    "checkin_valid_days": 1,           # 限时有效天数（1=当天 24 点，按下方时区）
    "checkin_valid_hours": 0,          # 附加小时
    "checkin_timezone": "Asia/Shanghai",
    "checkin_chat": 5,
    "checkin_image": 0,
    "checkin_search": 2,
    "checkin_ppt": 0,
    # 并发
    "max_inflight_default": 2,         # 单用户同时在途请求默认上限
}

# 内置默认免责协议（agreement_text 留空时生效；管理端可一键填入后二次修改）
DEFAULT_AGREEMENT_TEXT = """# 用户使用协议与免责声明

**最后更新：2026 年**

## 一、服务性质

1. 本服务（以下简称"本站"）为**非盈利性公益技术服务**，仅供用户个人学习、研究与技术交流使用，不向用户收取任何费用，也不提供任何商业担保。
2. 本站基于开源项目构建，通过接口转换为用户提供人工智能对话、绘图、搜索、文档生成等能力的**技术演示与体验**。
3. 本站不是内容的提供者，所有生成内容均由人工智能模型即时产生，不代表本站观点。

## 二、使用规范

用户承诺仅将本服务用于合法、正当的学习与交流目的，**严禁**利用本服务：

1. 生成、复制、传播任何违反国家法律法规的内容，包括但不限于危害国家安全、煽动颠覆、恐怖主义、极端主义、民族仇恨、暴力血腥、淫秽色情、赌博诈骗、侵犯知识产权、侵犯他人隐私或名誉的内容；
2. 从事任何非法侵入、攻击、干扰他人网络或系统的行为；
3. 以任何自动化手段批量滥用、转售、倒卖本服务或账号；
4. 冒用他人身份、传播虚假信息或从事任何欺诈活动；
5. 其他一切违法、违规或违背公序良俗的用途。

## 三、责任声明

1. **用户行为自负**：用户使用本服务所产生的一切行为及后果（包括但不限于生成、保存、传播的内容），由**账号使用者本人独立承担全部法律责任**，与本站及本站维护者无关。
2. **账号责任**：账号仅限本人使用。因用户主动泄露、共享账号导致的任何后果，由账号注册人承担。
3. **内容准确性**：人工智能生成内容可能包含错误、偏见或过时信息，仅供学习参考，不构成任何专业建议（包括但不限于法律、医疗、金融建议），用户应自行甄别核实。
4. **服务可用性**：本站为公益性质，不承诺服务的连续性、稳定性与数据持久性；因上游服务变动、技术故障等导致的中断或数据损失，本站不承担赔偿责任，但会尽合理努力提前告知。
5. **违规处置**：本站有权在发现或合理怀疑用户违反本协议时，**不经事先通知暂停或终止账号**，并保留向有关部门报告违法行为的权利。

## 四、隐私与数据

1. 本站仅存储提供服务所必需的最小数据（账号信息、使用配额、调用记录的接口与状态信息），**不记录**用户生成内容的具体文本用于审查之外的目的。
2. 登录设备信息（IP、浏览器类型）用于账号安全保护（登录设备管理、异常登录风控），不会用于其他用途。
3. 本站不会向任何第三方出售或提供用户数据，法律法规另有规定的除外。

## 五、协议变更与接受

1. 本站可能适时修订本协议并更新版本号，协议更新后用户需重新阅读并同意方可继续使用。
2. **滚动阅读本协议全文并点击"同意并继续"，即表示用户已充分理解并自愿接受本协议全部条款。**

如不同意本协议的任何内容，请立即停止使用本服务。
"""


async def get_setting(session: AsyncSession, key: str):
    row = await session.get(Setting, key)
    if row is None:
        return DEFAULT_SETTINGS.get(key)
    return row.value


async def get_all_settings(session: AsyncSession) -> dict:
    result = await session.execute(select(Setting))
    data = dict(DEFAULT_SETTINGS)
    for row in result.scalars():
        data[row.key] = row.value
    return data


async def set_settings(session: AsyncSession, values: dict) -> None:
    for key, value in values.items():
        if key not in DEFAULT_SETTINGS:
            continue
        row = await session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value


async def feature_flags(session: AsyncSession) -> dict[str, bool]:
    all_settings = await get_all_settings(session)
    return {
        "chat": bool(all_settings.get("feature_chat_enabled", True)),
        "image": bool(all_settings.get("feature_image_enabled", True)),
        "search": bool(all_settings.get("feature_search_enabled", True)),
        "ppt": bool(all_settings.get("feature_ppt_enabled", True)),
    }
