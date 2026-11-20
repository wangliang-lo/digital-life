# web_ui 路由冒烟测试说明

1. 覆盖面：`test_web_ui_routes.py` 盯六件事——`/api/health` 返回 200 且 ok 为真；`/api/drawn` 对不存在的合法文件名返回 404、对中文名返回 400（`_serve_drawn` 的白名单正则先拦下来）；未登录访问受保护页面（`/`）302 到 `/login`；未登录 `POST /api/chat` 返回 401 且不读请求体；`TTS_URL` 改指 `127.0.0.1:1` 后 `/api/voice` 返回 503、ok=false、error 可读。另加一条：`/chat` 实测在登录检查**之前**就返回页面，未登录是 200 不是 302——这是 web_ui 现有行为，断言按真实情况写。
2. 跑法：`cd PROJECT_ROOT && C:\Users\Lenovo\anaconda3\python.exe -m pytest tests/test_web_ui_routes.py -q`（当前 7 passed，约 2 秒，不启服务、不联网）。
3. 没覆盖到：不验证登录成功后的 `/`、`/api/chat` 正常链路和真实 HTTP 状态行/响应体（handler 是 `object.__new__` 造的，`send_response`/`wfile` 都是 mock）；`/api/voice` 只测故障分支，真正合成音频需要 TTS 服务；cookie 签名校验（`_session` 的真实实现）被整个 mock 掉了。
