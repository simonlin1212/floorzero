"""数据源身份声明（User-Agent）。

⚠️ **绝不能硬编码作者的联系方式。**
SEC / 众议院 / 参议院都要求请求带可识别的 User-Agent（含联系方式）。
若把某一个人的邮箱写死在代码里再开源，会有两个后果：

1. 该邮箱随仓库公开暴露；
2. **每个自部署用户的流量都以那个人的身份发出** ——
   谁把 SEC 打到限流/封禁，都记在他账上。

所以：**由部署者自己配置，没配就直接报错**（而不是塞个占位值蒙混过关 ——
那会让用户在毫不知情的情况下被上游限流，最难排查）。

配置方式：
    export FZ_CONTACT="你的名字 your@email.com"

（global-stock-data v2.0.1 已经踩过一次「fail-fast 提示被自己的
 `except Exception` 吃掉」的坑，所以这里用独立的异常类型，
 且下游必须让它冒泡，不能归进 DataNotAvailable。）
"""
from __future__ import annotations

import os


class ContactNotConfigured(RuntimeError):
    """未配置联系方式 —— **配置错误，必须冒泡**，不是「没数据」。"""


_HINT = (
    "未配置 FZ_CONTACT。SEC 与国会披露站点要求请求带可识别的 User-Agent"
    "（含联系方式），否则会被限流或封禁。\n"
    "  请设置环境变量后重启：\n"
    '    export FZ_CONTACT="Your Name your@email.com"\n'
    "  （这是给上游站点识别你自己的身份，不会发送到任何第三方。）"
)


def user_agent(product: str = "FloorZero/0.1") -> str:
    """构造合规 UA。未配置联系方式时**直接抛错**。"""
    contact = (os.environ.get("FZ_CONTACT") or "").strip()
    if not contact:
        raise ContactNotConfigured(_HINT)
    if "@" not in contact:
        raise ContactNotConfigured(
            f"FZ_CONTACT 需要包含可联系的邮箱，当前值：{contact!r}\n{_HINT}")
    return f"{product} ({contact})"
