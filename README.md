# Sleep English — 睡觉听英语视频生产工具

独立专注 **sleep 模式**（睡觉听 · AB 短句循环）的项目，从 `colab_listening_b_web` 分离而来。

对标参考：「400 句睡觉听」类频道 —— 每组 A/B 两个日常短句，男声常速 → 女声慢速 → 男声常速 → 女声慢速 → AB 连贯，纯 Pillow 卡片渲染 + 本地 TTS，**主流程零积分**（LLM 脚本生成走 API，画面零生图）。

## 结构

- **intro**（频道名播报 / 片头库视频）→ **N × [A男常速 → A女慢速 → B男常速 → B女慢速 → AB连贯]** → **outro**
- 默认 200 组 = 400 句，约 100 分钟；组间交叉溶解可选

## 启动

```bash
pip install -r requirements.txt
run.bat            # http://localhost:8766
```

## Web 功能

| 页面 | 说明 |
|------|------|
| 控制台 | 一键生成/继续/停止、常用配置面板、批量生成队列 |
| 参数配置 | Sleep 全参数（组数/速率/停顿/配色/背景图/片头/溶解…）+ 实时卡片预览 |
| 主题管理 | 主题库 + AI 生成主题 |
| 批量脚本 | 预生成 sleep 脚本入库（LLM 分批、批次缓存） |
| 运行历史 | 缩略图管理/重生成、4K、混 BGM、重渲、已上传标记、路径复制 |
| 片头库 | 10 秒片头（本地 Pillow 动画 / AI 视频）生成与绑定 |
| 三音色页 | Qwen / MOSS / Kokoro 默认音色与试听 |
| AI 测试 | LLM/生图通道诊断 |

## 命令行

```bash
cd pipeline
python pipeline.py --topic "Asking the Teacher a Question" --cefr A2 --output ../output
python pipeline.py --quick-test --no-4k --sleep-pairs 10   # 复用上次素材零积分冒烟
python pipeline.py --resume                                # 断点续传
```

## 步骤

| 步骤 | 说明 |
|------|------|
| Step 0 | LLM 脚本生成（分批落盘缓存，WBK / Gemini / 自定义 OpenAI 兼容） |
| Step 1 | 跳过（sleep 主流程零 MCP） |
| Step 2 | 本地 TTS（Kokoro / Qwen3-TTS / MOSS-TTS）+ 可选 AI 背景图 + 片头绑定 |
| Step 3 | 跳过（无视频片段） |
| Step 4 | 时间轴 + SRT（sidecar 闭源字幕） |
| Step 4.5 | YouTube 元数据 + AI 缩略图（失败兜底 Pillow 卡片；集数徽章自动递增） |
| Step 5 | Pillow 卡片块合成 + FFmpeg concat |
| Step 5.5 | 可选版权 BGM 混音（sidechain，Content-ID 友好） |
| Step 6 | 可选 4K（ffmpeg lanczos / AI 超分；`sleep_4k_native` 原生 4K） |

## 与原项目的关系

- 原项目 `colab_listening_b_web` 保留全部模式不受影响；本项目是其 sleep 链路的独立副本
- 两项目运行数据/集数徽章/主题防重各自独立计数
- 老运行数据未迁移；`output/sleep/` 下保留一个样例 run 可直接 `--quick-test` 冒烟
