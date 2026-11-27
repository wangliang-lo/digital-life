# MemBench 评测工具

这个目录里的脚本用于复现「记忆系统在 MemBench 上各层瓶颈的定位实验」。

**只跑记忆/评测，不启动助手的任何服务。**

## 四步

```bash
# 1. 依赖（只有 numpy）
pip install -r _bench/requirements-bench.txt

# 2. 设置 key（必需）
export DEEPSEEK_API_KEY=sk-...        # Linux/macOS
$env:DEEPSEEK_API_KEY="sk-..."        # Windows PowerShell

# 3. 准备数据（MemBench 数据包，约 160MB，不随仓库分发）
#    把 membench-data.zip 放到 _exp/membench-ref/data/ ，或解压到任意目录后用 --data 指定
python -m _bench.get_data --zip /path/to/membench-data.zip

# 4. 跑（ingest + eval + 汇总表）
python -m _bench.run_all --n 300 --file ThirdAgentDataLowLevel.json --embed --raw-content
```

`--data` 可指向 ZIP、解压后的 `Membenchdata` 目录、或其中的 `data/` 目录。

## 选项

| 参数 | 说明 |
|---|---|
| `--n` | 取前 n 道题 |
| `--file` | 数据文件名（默认 `ThirdAgentDataHighLevel.json`）|
| `--model` | `deepseek-chat`（默认）/ `gpt-6.2-sol` / `claude-opus-5-5` |
| `--no-compress` | **不调交大压缩接口**，直接把用户原文当记忆。没有压缩接口时用这个 |
| `--compare` | 传入另一份 `ckpt-eval-*.json`，输出配对救回/弄坏 + McNemar |
| `--gold` | gid/memory_id 标注，用于算检索召回；不给就显示 N/A |

## 环境变量

| 变量 | 作用 |
|---|---|
| `DEEPSEEK_API_KEY` | **必需**。作答模型 key |
| `MEMBENCH_HOME` | 数据根目录，默认 `<repo>/_exp` |
| `MEMBENCH_RUN_DIR` | 缓存与 checkpoint 目录，默认 `<MEMBENCH_HOME>/bench` |
| `CLAUDE_KEY` / `KIMI` | 只有换到对应作答模型时才需要 |

## 单跑的脚本

`_bench/` 下这些是独立分析脚本，各自读 `ckpt-osf300.json` 或 `hide-options.json`：

| 脚本 | 做什么 |
|---|---|
| `hide_options.py` | 不给选项让模型自算答案值，再看它能不能选对（测「基准有多少分是选择题送的」）|
| `permute_options.py` | 旋转选项位置，区分「认内容」和「位置偏好」|
| `tost_equivalence.py` | TOST 等价性检验（替代 p 值）|
| `synthetic_counting.py` | 受控合成数据，测模型本身会不会数 |
| `knowledge_update_attrib.py` | 知识更新题的错误归因 |
| `gold_audit2.py` | 金标审计（槽位向量匹配）|
| `label_c_arm.py` | 人工盲标工具 |
| `claim_check.py` | 主张核实器（不属于评测，是助手侧的一个小模块）|

## 诚实的限制

- **`--model` 只有 `deepseek-chat` 是完整打通的。** `gpt-6.2-sol` / `claude-opus-5-5` 走的是同一个中转站接口（`ask_claude.py`），**需要你自己有可用的中转 key**；没有的话会在发请求前报错，不会用 DeepSeek 跑完再贴上别的模型的标签。
- **压缩模式需要交大的 `sjtu_llm` 模块**，这个模块不随仓库分发。没有它就用 `--no-compress`。
- **数据包不随仓库分发**（约 160MB）。`get_data.py` 只负责检查和解压，不提供下载地址。
- **`used_memories` 不是召回率的分母。** 有标注时用 `--gold` 算召回；有基线 checkpoint 时用 `--compare` 算配对结果。

## 已知的行为细节（改代码前先读）

- **缓存 fallback 必须按 n 的数值排序。** 用字母序 `sorted()` 会让 `n10` 排在 `n300` 前面，于是「跑 300 题」实际只用了 10 条记忆——结果看起来完整、能算统计量，但全错。
- **有 ckpt 会自动续跑。** 想从头跑要先删 `ckpt-eval-*.json`。
- **多进程并发会互相覆盖 `ckpt-eval-*.json`。** 一次只跑一个。
- **`--no-compress` 的检索召回比压缩模式低很多**（实测 51.6% vs 84%）。所以它的绝对准确率不能和压缩模式直接比，但同一缓存内的组间比较是有效的。
