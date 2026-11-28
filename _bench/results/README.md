# results/ —— 逐题数据

全部是 `_bench/run_all.py` 或本文档提到的脚本产出的逐题结果，**可以直接复核文档里的每一个数字**。

## 文件说明

| 文件 | 内容 |
|---|---|
| `ckpt-A.json.gz` … `ckpt-D.json.gz` | 八组干预的四个条件（A=原样 / B=时间正序 / C=加那句话 / D=两者都有），各 300 题 |
| `ckpt-osf300.json.gz` | DeepSeek 基线（历史，无来源日志）|
| `ckpt-gptfix.json.gz` / `ckpt-opusfix.json.gz` | 换模型后的 300 题（路由已修，每题都走目标模型）|
| `ckpt-deepseekprov.json.gz` | DeepSeek 基线重跑（带来源日志，与上面两个同口径）|
| `prov2-gpt.jsonl.gz` / `prov2-opus.jsonl.gz` / `prov2-deepseek.jsonl.gz` | **逐题来源记录**：请求模型、服务端自报模型名、是否退回、完整 prompt、模型原始输出 |
| `holm-correction.json` | 全部 8 个配对比较的原始 p 与 Holm 校正 p |
| `free-form.json` | 自由回答实验（300 题，含判定器和原始回答）|
| `permute-options.json` | 选项位置旋转实验（300 题，含内容跟随/字母坚持）|
| `hide-options.json` | 隐藏选项实验（300 题，含自算值与最终字母）|

## 怎么复核

```bash
# 解压
gunzip -k results/*.gz

# 重算 Holm 校正
python _bench/holm_correction.py

# 重算配对比较（需要改脚本里的文件路径）
python -c "见文档 §3 的 McNemar 公式"
```

## ckpt 的字段

```json
{
  "gid": "t1-post_processing/items",   // 题号
  "pred": "D",                          // 模型选的字母
  "truth": "D",                         // 金标
  "correct": 1,                         // 是否对
  "used_memories": ["（2024-10-01 08:00）..."],  // 实际喂进 prompt 的记忆
  "category": "post_processing/items",
  "rank": 1,                            // 金标证据在检索结果里的名次
  "recall": 1.0
}
```

## 来源记录的字段

```json
{
  "requested_model": "gpt-6.2-sol",
  "served_model": "gpt-6.2-sol",        // 服务端自报的模型名
  "fallback": false,
  "temperature_dropped": false,
  "prompt": "完整 prompt",
  "raw": "模型原始输出"
}
```

**注意**：`served_model` 是**服务端自报**的，不是核对过的实际请求体。

## 已知的不对称

- **DeepSeek 历史基线（`ckpt-osf300.json`）没有来源日志**，因为它是在加审计机制之前跑的。
- `ckpt-deepseekprov.json` 是后来补跑的，两者差距见文档 §3。
