# NOTES_intent_router

**覆盖什么**：`route`/`_parse`/`snapshot` 共 36 例——空消息与超长（>300 字，含 300 字边界仍走模型）、正常 JSON、被散文包裹的 JSON、垃圾输出（`哈哈`/``/`{bad json`/`null`）、`_llm` 抛 `TimeoutError`/`OSError`/`ValueError`、actions 结构过滤（非 dict、空 type 丢弃，type 转小写去空格，多字段透传）、`actions[:4]` 截断、`snapshot` 空目录/坏 JSON/非 dict JSON 均不抛错且能读出正常数据。`_llm` 全程被 monkeypatch 替身替换，无任何网络；`snapshot` 读的 data 目录靠 monkeypatch 模块 `__file__` 指向 `tmp_path`（源码第 60-61 行是 `Path(__file__).resolve().parent / "data"`，没有路径常量可改），真实 `chat_bridge/data/` 一次都没碰。

**怎么跑**：`cd PROJECT_ROOT && C:\Users\Lenovo\anaconda3\python.exe -m pytest tests/test_intent_router.py -q` → `36 passed`。

**没覆盖什么**：`_llm` 本体（真 HTTP、payload 字段、`_load_key` 的 `.env` 读取）、`ACTIONS_HELP` 文案内容、`dl_api.py` 里对返回动作的执行；`snapshot` 的 board 长度非 9 的畸形数据只手动看过没写成断言（已在下文 bug 3 记录）。

## 读源码发现的真实 bug（未改生产文件）

1. **`_parse` 第 123 行 `re.search(r"\{.*\}", text, re.S)` 是贪婪匹配**——只要模型回复在 JSON 之后还带任何含 `}` 的字符（很常见，比如后面追加一句 `备注：{"x":1}`，或复述了格式示例 `{"actions":[...]}`），正则就会从第一个 `{` 一路吃到最后一个 `}`，`json.loads` 必然 `JSONDecodeError`，`_parse` 返回 `[]`。后果：模型明明判断对了动作，这一句却被静默丢掉，回落到正则兜底，且没有任何日志。已实测：`'{"actions":[{"type":"water"}]} 备注：{"x":1}'` → `[]`，干净输入 → `[{'type':'water'}]`。修法：改成非贪婪 `r"\{.*?\}"` 并循环重试，或按 `response_format: json_object` 的前提直接 `json.loads(text)` 再退回扫描。

2. **`_load_key` 第 45 行跨供应商回退**：`os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")`。挂了 OpenAI key 但没挂 DeepSeek key 的机器上，这个 OpenAI 密钥会被塞进 `Authorization: Bearer` 发给 `https://api.deepseek.com/chat/completions`（第 98/112 行）。后果有两个：一是密钥泄露给第三方域名，二是请求必然 401 → 走 `except` → 永远返回 `[]`，动作识别静默失效。建议只认 `DEEPSEEK_API_KEY`，或按 base_url 选对应环境变量。

3. **`snapshot` 第 73-75 行假设 board 恰好 9 格**：`names[i + 1]` 在 `len(board) > 9` 时抛 `KeyError`，被外层 `except Exception` 吞掉，整块 `棋局` 从状态里消失（模型就看不到棋局了）。`len(board) < 9` 则不报错但"空格"会缺格子。正常落子流程下 board 恒为 9 格，只有 `tictactoe.json` 被别的代码写坏时才触发，优先级低。
