# 她一键启动：数字生命 API + 网页版 + 记忆同步 + 说话习惯训练
# 开机自启：把 start_all.cmd 放进 shell:startup 即可（见 README 10.5）。

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = 'C:\Users\Lenovo\anaconda3\python.exe'
if (-not (Test-Path $py)) {
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) { $py = $cmd.Source } else { $py = 'python' }
}

$logDir = Join-Path $root 'logs'
$pidDir = Join-Path $logDir 'pids'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
New-Item -ItemType Directory -Force -Path $pidDir | Out-Null

function Start-Hidden([string]$name, [string[]]$arguments) {
  $proc = Start-Process -FilePath $py -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir "$name.out.log") `
    -RedirectStandardError (Join-Path $logDir "$name.err.log") -PassThru
  if ($proc) {
    Set-Content -Path (Join-Path $pidDir "$name.pid") -Value $proc.Id -Encoding ascii
  }
}

function Test-PidFile([string]$name) {
  # 自己起的服务会留 PID 文件；进程还在就直接算在跑，不依赖进程枚举。
  $file = Join-Path $pidDir "$name.pid"
  if (-not (Test-Path $file)) { return $false }
  $value = (Get-Content -Path $file -ErrorAction SilentlyContinue | Select-Object -First 1)
  if (-not $value) { return $false }
  return [bool](Get-Process -Id ([int]$value) -ErrorAction SilentlyContinue)
}

function Test-ProjectProcess([string]$needle, [string]$name) {
  # 重复运行 start_all 时只跳过已在跑的服务，避免再叠一组后台进程。
  if (Test-PidFile $name) { return $true }
  try {
    $processes = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction Stop
    return [bool]($processes | Where-Object { $_.CommandLine -like "*$needle*" })
  } catch {
    # 枚举不了进程时按“没在跑”处理：宁可重启一次，也不要整组服务没人起来。
    Write-Warning "无法枚举进程（$($_.Exception.Message)）：$needle 按未运行处理，如果它其实在跑请先 stop_all。"
    return $false
  }
}

function Start-Once([string]$name, [string[]]$arguments, [string]$needle) {
  if (Test-ProjectProcess $needle $name) {
    Write-Host "已在运行，跳过：$name"
    return
  }
  Start-Hidden $name $arguments
}

# deepseek-flash：私信走便宜档，不再默认 deepseek-chat
Start-Once 'dl_api' @('dl_api.py', '--port', '8000', '--db', "$root\data\xiaoyu.db",
  '--vision-model', 'qwen2.5vl:3b', '--asr-provider', 'faster-whisper', '--asr-model', 'medium', '--llm', 'auto',
  '--llm-model', 'deepseek-chat') 'dl_api.py'

Start-Sleep -Seconds 2
Start-Once 'web_ui' @('web_ui.py', '--host', '0.0.0.0', '--port', '8899', '--api', 'http://127.0.0.1:8000') 'web_ui.py'

# 她的独立信道：定期让她选择留言 / 发起代码讨论 / 沉默
# 2026-11-11：常驻生活循环（快时钟，只管她此刻在干什么）
# 用 supervisor 包一层：循环挂了 5 秒后自己拉起来（爸爸：保证循环没挂即可）
Start-Once 'life_supervisor' @('tools\life_supervisor.py') 'life_supervisor.py'

# 2026-11-10 爸爸：自由时间 30 分钟一次太费 token，改成 1 小时（改这里才不会再退回 1800）
Start-Once 'solver_channel' @('tools\solver_channel.py', '--watch', '3600') 'tools\solver_channel.py'

# 记忆双向同步（每 5 分钟，没变化时是空转）
Start-Once 'sync_memory' @('tools\sync_memory.py', '--watch', '300', '--min-mv', '20', '--limit', '60') 'tools\sync_memory.py'
Start-Once 'sync_from_douyin' @('tools\sync_from_douyin.py', '--watch', '300') 'tools\sync_from_douyin.py'

# 每 6 小时看看有没有新对话，提炼说话习惯（没新料会自己跳过）
Start-Once 'style_trainer' @('tools\style_trainer.py', '--watch', '21600') 'tools\style_trainer.py'

# 每天备份一次记忆库（保留 14 份）
Start-Once 'backup_memory' @('tools\backup_memory.py', '--watch', '86400') 'tools\backup_memory.py'

# 社交时间：按概率主动找人聊天（07:00-22:00，每天最多 4 次）
Start-Once 'social_time' @('tools\social_time.py') 'tools\social_time.py'

# 观看守护：单条作品播完就关标签页/回推荐流
# 自主观看已关闭：不再启动观看守护，避免它误关主人转发的短视频采样页。
# Start-Once 'watch_guard' @('tools\watch_guard.py') 'tools\watch_guard.py'

# 生图：本地 SD-Turbo，供她写 [DRAW: 描述] 时画一张（依赖在 PROJECT_ROOT\drawlibs）
Start-Once 'draw_server' @('tools\draw_server.py') 'tools\draw_server.py'

# 她的声音：Breeze TTS 2（4-bit，权重在 PROJECT_ROOT\models\breeze-tts-2，依赖在 tts-venv）
# 没起也不影响聊天：她主动发 [VOICE] 时网页会提示声音服务没起来。
$ttsPy = Join-Path (Split-Path -Parent $root) 'tts-venv\Scripts\python.exe'
if (Test-Path $ttsPy) {
  $ttsRunning = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*tts_server.py*' })
  if (-not $ttsRunning.Count) {
    $ttsProc = Start-Process -FilePath $ttsPy -ArgumentList @('tools\tts_server.py', '--port', '7861') -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'tts_server.out.log') -RedirectStandardError (Join-Path $logDir 'tts_server.err.log') -PassThru
    if ($ttsProc) { Set-Content -Path (Join-Path $pidDir 'tts_server.pid') -Value $ttsProc.Id -Encoding ascii }
  }
}

# 她安静/睡着/不在线时没回的消息，等她回来补回
# 2026-11-10：60 秒一次太密，每次都是一整轮模型调用
Start-Once 'pending_replay' @('tools\pending_replay.py', '--watch', '120') 'tools\pending_replay.py'

# IM那个 app 的"脑子"：把 DeepSeek Messages 协议接到她身上（127.0.0.1:8003）
# IMAgent 用环境变量 DEEPSEEK_BASE_URL 指过来，IM 里说话的就是她本人。
Start-Once 'anthropic_bridge' @('tools\anthropic_bridge.py', '--port', '8003', '--api', 'http://127.0.0.1:8000') 'tools\anthropic_bridge.py'

# 异步换音色：把 Breeze 的声音换成她自己的音色（RVC，CPU 模式，一次一个，不抢显存）
Start-Once 'rvc_worker' @('tools\rvc_worker.py', '--watch', '1') 'tools\rvc_worker.py'

# IM表情池补标签：没描述的图 App 不会当候选，她就发不出表情
Start-Once 'sticker_labeler' @('tools\label_stickers.py', '--watch', '30', '--limit', '5') 'tools\label_stickers.py'

# 睡眠守护：等她真的睡着后先整理图书馆、再让本地模型做梦写日记；一直不睡就 03:00 兜底
Start-Once 'sleep_cycle' @('tools\sleep_cycle.py', '--daemon') 'tools\sleep_cycle.py'

# 抖音指令执行器（Node）：她写 [DOUYIN: ...] 时真正去点浏览器的那双手。
# playwright 装在 DSH 的 node_modules 里，所以给它 NODE_PATH。
$douyinWatcher = Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like '*command_watcher*' }
if (-not $douyinWatcher) {
  $env:NODE_PATH = "C:\Users\Lenovo\.dsh\profiles\desktop\node_modules"
  Start-Process -FilePath "C:\Program Files\nodejs\node.exe" -ArgumentList "tools\command_watcher.cjs" `
    -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput "$root\logs\command_watcher.out.log" `
    -RedirectStandardError "$root\logs\command_watcher.err.log"
  Write-Host "已启动：command_watcher"
} else {
  Write-Host "已在运行，跳过：command_watcher"
}

# 抖音专用浏览器（Edge + CDP 9227）：她要在抖音里点点点，得有这个窗口在。
# 9227 没在监听才启动，避免和插件自己拉起来的那个打架。
$cdpUp = (netstat -ano | Select-String ':9227 .*LISTENING')
# 2026-11-04 爸爸："怎么自动打开抖音了，如果不是她做的是不是bug。"
# 是这里：每次重启服务都会把抖音专用浏览器拉起来（一个 Edge 窗口弹出来）。
# 只有她自己把抖音打开着（douyin_state.enabled=true）才需要这个窗口。
$douyinOn = $false
try {
  $statePath = Join-Path $root 'data\douyin_state.json'
  if (Test-Path $statePath) {
    $douyinOn = (Get-Content $statePath -Raw -Encoding UTF8 | ConvertFrom-Json).enabled -eq $true
  }
} catch { $douyinOn = $false }
if ((-not $cdpUp) -and $douyinOn) {
  Start-Process -FilePath "C:\Program Files\nodejs\node.exe" `
    -ArgumentList "tools\start_douyin_browser.mjs" -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput "$root\logs\douyin_browser.out.log" `
    -RedirectStandardError "$root\logs\douyin_browser.err.log"
  Write-Host "已启动：抖音专用浏览器"
} else {
  Write-Host "已在运行，跳过：抖音浏览器"
}

# 本地语音识别（OpenAI 兼容，端口 8005）：IM语音靠它转成文字。
# IM 助手的 speech-recognition 插件只会把音频传给一个接口，这里自己顶上去。
Start-Once 'asr_server' @('tools\asr_server.py', '--port', '8005', '--model', 'medium') 'tools\asr_server.py'

# 抖音私信监听：每 5 分钟看一眼谁找过她，写进 data/douyin_inbox.json
$douyinInbox = Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like '*douyin_inbox*' }
if (-not $douyinInbox) {
  $env:NODE_PATH = "C:\Users\Lenovo\.dsh\profiles\desktop\node_modules"
  Start-Process -FilePath "C:\Program Files\nodejs\node.exe" `
    -ArgumentList "tools\douyin_inbox.mjs --watch 300" -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput "$root\logs\douyin_inbox.out.log" `
    -RedirectStandardError "$root\logs\douyin_inbox.err.log"
  Write-Host "已启动：抖音私信监听"
} else {
  Write-Host "已在运行，跳过：抖音私信监听"
}

# 人物卡片：每 30 分钟给好友攒外号和印象
Start-Once 'profile_builder' @('tools\profile_builder.py') 'tools\profile_builder.py'

# 好感度：按真实互动结算
Start-Once 'affection' @('tools\affection.py') 'tools\affection.py'

Start-Sleep -Seconds 2
Write-Host '已启动：'
Write-Host '  网页版    http://127.0.0.1:8899（先登录）'
Write-Host '  数字生命  http://127.0.0.1:8000/status'
Write-Host ''
Write-Host '停止：.\stop_all.ps1'
