# omni-bot-sdk 插件：数字生命

把IM 4.x（omni 走「读数据库 + 视觉点按钮」，不 Hook）接到本地 `dl_api`。

> 这条路线是为「IM 3.6.0.18 登录被拒」准备的 Plan B。
> 和 ntchat 方案二选一，不要同时跑：两边都会去操作同一个IM。

---

## 它做什么

| IM 里发生什么 | 插件做什么 |
| --- | --- |
| 私聊文字 | `POST /chat` → 回复文字 |
| 群里 @ 我 / 含唤醒词 | 去掉唤醒词 → `POST /chat` → 回复并 @ 对方 |
| 收到语音 | 用IM自带的「语音转文字」结果；没有就发占位符 |
| 收到图片 | 把本地图片路径一起发给 `/chat`，由 `dl_api` 调视觉模型 |
| 回复带情绪 | 按 `joy/hurt/impulse` 挑一张表情包一起发 |
| 定时 | 每 N 分钟 `POST /proactive`，发到「文件传输助手」 |

自己发的消息直接跳过；omni 自带的 `self-msg-plugin` 还会再挡一层。

---

## 安装

```powershell
# 必须 Python 3.12（omni 的硬要求），单独建环境
conda create -n omni python=3.12 -y
conda activate omni
pip install omni-bot-sdk

cd PROJECT_ROOT\chat_bridge\omni_plugin
pip install -e .
```

装完用下面确认插件被注册：

```powershell
python -c "import importlib.metadata as m; print([e.name for e in m.entry_points(group='omni_bot.plugins')])"
# 应该看到 ['digital-life-plugin', ...]
```

---

## 配置

把 `config.example.yaml` 复制成 omni 的 `config.yaml`，至少要填：

```yaml
dbkey: "从 DbkeyHook 拿到的 64 位密钥"

plugins:
  digital-life-plugin:
    enabled: true
    api_base_url: http://127.0.0.1:8000
    wake_words: [她, 小鱼]
    stickers_dir: C:/aihuman/chat_bridge/stickers
```

常用字段：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `api_base_url` | `http://127.0.0.1:8000` | dl_api 地址 |
| `wake_words` | `[她, 小鱼]` | 群聊唤醒词 |
| `api_timeout` | `120` | 大模型慢，别太小 |
| `private_enabled` / `group_enabled` | `true` | 分开关 |
| `at_reply` | `true` | 群里回复时 @ 对方 |
| `user_id_strategy` | `sender` | `sender`/`room`/`fixed`，决定记忆隔离粒度 |
| `stickers_enabled` / `stickers_dir` | `true` / `stickers` | 和 chat_bridge 共用表情目录 |
| `sticker_chance` | `0.25` | 附表情的概率 |
| `proactive_enabled` | `true` | 主动聊天 |
| `proactive_interval_minutes` | `45` | 间隔 |
| `proactive_target` | `文件传输助手` | 发给谁 |
| `proactive_quiet_hours` | `["23:30","08:00"]` | 安静时段 |
| `fallback_on_error` | `""` | 数字生命挂了要不要回一句 |

---

## 启动顺序

```powershell
# 0) IM 4.x 已登录；omni 需要重启IM后再启动，否则拿不到图片解密密钥

# 1) 数字生命 API（Python 3.10 环境）
python dl_api.py --vision-model gemma3:4b

# 2) omni（Python 3.12 环境）
cd omni_plugin
python -c "from omni_bot_sdk.bot import Bot; Bot(config_path='config.yaml').start()"
```

---

## 已知限制（都是 omni 的，不是插件的）

- **会抢鼠标键盘**：RPA 执行时必须让电脑闲着，最好单独一台机器；
- **按显示名找会话**：有同名联系人群时可能发错；
- **版本敏感**：官方标的是 4.0.6.33，你在 4.2.0.7 上属于「理论支持」，需要实测；
- **要 DB key**：得先用 DbkeyHook 拿密钥；
- **GPL-V3**：omni 是 GPL 项目，自己用没问题，二次分发要注意；
- 语音只用了IM自带的转写结果（`voicetrans`），对方没开转写时只有占位符。
  想本地 ASR 的话，把 `AudioMessage.path` 也发给 `/chat`（需要确认 omni 给的是不是 silk）。

---

## 出问题先看

1. 插件没加载 → `plugins.<name>.enabled` 是不是 true；entry point 是否注册成功；
2. 消息进来了没回复 → 看 omni 日志里 RPA 队列是否执行、`target` 名字是否和IM 里一致；
3. 回复了但发错人 → 同名会话，改备注或别用 RPA；
4. 数字生命那边 500 → 看 `dl_api` 日志，通常是模型/密钥问题。
