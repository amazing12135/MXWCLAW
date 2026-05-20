"""多渠道适配器。

提供 BaseChannel 抽象基类及微信/QQ/Email 三个具体频道实现。
"""

from mxwbot.channel.base import (
    BaseChannel,
    ChannelAuthError,
    ChannelError,
    ChannelFatalError,
    ChannelTransientError,
)
from mxwbot.channel.email import EmailChannel
from mxwbot.channel.qq import QQChannel, QQConfig
from mxwbot.channel.weixin import WeChatChannel, WeixinConfig

__all__ = [
    "BaseChannel",
    "ChannelAuthError",
    "ChannelError",
    "ChannelFatalError",
    "ChannelTransientError",
    "EmailChannel",
    "QQChannel",
    "QQConfig",
    "WeChatChannel",
    "WeixinConfig",
]
