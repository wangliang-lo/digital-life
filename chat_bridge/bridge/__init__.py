# -*- coding: utf-8 -*-
"""
IM ⇄ 数字生命 中间件。

    bridge.config    配置加载
    bridge.client    数字生命 HTTP API 客户端
    bridge.pipeline  消息清洗 / 唤醒词 / 去重 / 防自环
    bridge.worker    串行回复队列
    bridge.scheduler 主动聊天定时器
    bridge.control   本地控制 API（给 DSH 调用）
    bridge.app       装配层
    bridge.adapters  IM适配器（ntchat / mock）
"""

__version__ = "1.0.0"
