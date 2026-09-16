"""Configuration manager: load/save JSON configs, preset management,
and build CLI args for pipeline.py."""
import json
import os
import sys
from pathlib import Path
from typing import Any

# Resolve paths
WEB_ROOT = Path(__file__).parent.parent.resolve()
CONFIGS_DIR = WEB_ROOT / "configs"
DEFAULT_CONFIG_PATH = CONFIGS_DIR / "default.json"

# pipeline 模块目录（自包含：topics.json / pipeline.py 均在本项目内，
# 不再依赖旧的外部 colab_listening_b 目录）
PIPELINE_DIR = WEB_ROOT / "pipeline"

# pipeline 模块目录（style_manager 在其中）——sys.path 注入委托给 paths.ensure_pipeline_on_path
from .paths import ensure_pipeline_on_path

ensure_pipeline_on_path()


# All configurable parameters with defaults, types, and metadata
#
# 参数生效性标注（"modes" 键）：本项目专注 sleep 模式，保留字段仅作
# 兼容占位（缺省=全局生效）。新增参数默认不标注 modes。
PARAM_SPEC = {
    # --- Content ---
    "topic": {"default": "", "type": "text", "group": "content",
              "label": "主题", "help": "留空则从主题库随机选择"},
    "cefr": {"default": "A2", "type": "select", "group": "content",
             "label": "CEFR 等级", "options": ["A1", "A2", "B1", "B2", "C1", "C2"]},
    "structure": {"default": "sleep", "type": "select", "group": "content",
                  "label": "视频结构", "options": {
                      "sleep": "Sleep (睡觉听短句循环)"}},
    "visual_style": {"default": "pixar3d", "type": "select", "group": "content",
                     "label": "画面风格",
                     "options": {"pixar3d": "3D 卡通（皮克斯）"},
                     "help": "仅影响 AI 背景图/缩略图 prompt 的画面风格（sleep 卡片为纯 Pillow 渲染）"},

    # --- Sleep（睡觉听短句循环，仅 sleep 模式）---
    "sleep_pairs": {"default": 200, "type": "number", "group": "sleep",
                    "modes": ["sleep"],
                    "label": "对话组数 (10-400)",
                    "help": "A/B 短对话组数，行数=组数×2（默认 200 组=400 句，对标「400句」参考体量；约 30 秒/组）"},
    "sleep_slow_rate": {"default": 0.8, "type": "number", "group": "sleep",
                        "modes": ["sleep"],
                        "label": "女声慢速速率",
                        "help": "女声慢速朗读速率倍率（0.6-0.95，默认 0.8=八成速；越小越慢，低于 0.6 发音会含糊）；经引擎 rate 机制调速，Kokoro 原生变速不变调"},
    "sleep_male_rate": {"default": 1.0, "type": "number", "group": "sleep",
                        "modes": ["sleep"],
                        "label": "男声速率",
                        "help": "男声常速朗读速率倍率（0.5-1.5，默认 1.0=原速；<1 更慢更催眠，>1 加快）；同样作用于片头/片尾男声播报；经引擎 rate 机制调速不变调"},
    "sleep_gap_short": {"default": 1.0, "type": "number", "group": "sleep",
                        "modes": ["sleep"],
                        "label": "常速后停顿(秒)",
                        "help": "男声常速朗读后的静音气口（默认 1.0 秒）"},
    "sleep_gap_long": {"default": 2.0, "type": "number", "group": "sleep",
                       "modes": ["sleep"],
                       "label": "慢速后停顿(秒)",
                       "help": "女声慢速朗读后的跟读气口（默认 2.0 秒）"},
    "sleep_pair_gap": {"default": 3.0, "type": "number", "group": "sleep",
                       "modes": ["sleep"],
                       "label": "切组停顿(秒)",
                       "help": "AB 连贯朗读后切下一组的停顿（默认 3.0 秒）"},
    "sleep_batch_pairs": {"default": 50, "type": "number", "group": "sleep",
                          "modes": ["sleep"],
                          "label": "LLM 分批组数",
                          "help": "每批生成的对话组数（默认 50，批间落盘可断点续传）"},
    "sleep_use_cache": {"default": True, "type": "checkbox", "group": "sleep",
                        "modes": ["sleep"],
                        "label": "复用批次缓存",
                        "help": "同主题+同 CEFR+同组数重跑时复用已生成批次（秒级出稿、中断续传）；关闭=每次现场重新生成新内容"},
    "sleep_channel_name": {"default": "English with me", "type": "text", "group": "sleep",
                           "modes": ["sleep"],
                           "label": "频道名",
                           "help": "卡片左上/片头卡显示 + 片头 TTS 播报（声学标签）"},
    "sleep_outro_text": {"default": "Thanks for listening. See you next time!", "type": "text", "group": "sleep",
                         "modes": ["sleep"],
                         "label": "片尾结束语",
                         "help": "片尾卡文字 + TTS 播报"},
    "sleep_show_leaves": {"default": True, "type": "checkbox", "group": "sleep",
                          "modes": ["sleep"],
                          "label": "叶片装饰",
                          "help": "卡片角落浅色叶片装饰（关闭更素净）"},
    "sleep_handwrite_font": {"default": "", "type": "text", "group": "sleep",
                             "modes": ["sleep"],
                             "label": "手写体字体路径",
                             "help": "频道名字体 .ttf/.ttc 路径；留空=Inkfree→Segoe Script→雅黑 Bold"},
    "sleep_color_bg_top": {"default": "", "type": "color", "group": "sleep",
                           "modes": ["sleep"], "label": "背景渐变顶部",
                           "help": "hex 如 #eaf4e2；留空用默认配色"},
    "sleep_color_bg_bottom": {"default": "", "type": "color", "group": "sleep",
                              "modes": ["sleep"], "label": "背景渐变底部", "help": "hex"},
    "sleep_color_card": {"default": "", "type": "color", "group": "sleep",
                         "modes": ["sleep"], "label": "卡片底色", "help": "hex"},
    "sleep_color_card_border": {"default": "", "type": "color", "group": "sleep",
                                "modes": ["sleep"], "label": "卡片描边", "help": "hex"},
    "sleep_color_en_a": {"default": "", "type": "color", "group": "sleep",
                         "modes": ["sleep"], "label": "A句英文颜色", "help": "hex（参考同款=深棕）"},
    "sleep_color_en_b": {"default": "", "type": "color", "group": "sleep",
                         "modes": ["sleep"], "label": "B句英文颜色", "help": "hex（参考同款=橙色）"},
    "sleep_color_phonetic": {"default": "", "type": "color", "group": "sleep",
                             "modes": ["sleep"], "label": "音标颜色", "help": "hex（参考同款=橄榄绿）"},
    "sleep_color_zh": {"default": "", "type": "color", "group": "sleep",
                       "modes": ["sleep"], "label": "中文颜色", "help": "hex"},
    "sleep_color_num": {"default": "", "type": "color", "group": "sleep",
                        "modes": ["sleep"], "label": "序号/横条颜色", "help": "hex（参考同款=粉色发光）"},
    "sleep_color_badge_bg": {"default": "", "type": "color", "group": "sleep",
                             "modes": ["sleep"], "label": "角标底色", "help": "hex（参考同款=粉）"},
    "sleep_color_badge_text": {"default": "", "type": "color", "group": "sleep",
                               "modes": ["sleep"], "label": "角标文字色", "help": "hex"},
    "sleep_color_channel": {"default": "", "type": "color", "group": "sleep",
                            "modes": ["sleep"], "label": "频道名颜色", "help": "hex"},
    "sleep_color_leaf": {"default": "", "type": "color", "group": "sleep",
                         "modes": ["sleep"], "label": "叶片颜色", "help": "hex"},
    "sleep_bg_image": {"default": False, "type": "checkbox", "group": "sleep",
                       "modes": ["sleep"],
                       "label": "背景图片",
                       "help": "开启后在渐变背景上低透明度叠加主题相关图片；无固定图时按本期主题 AI 生成 1 张（走「🎨MCP/图片」生图 Provider 通道）"},
    "sleep_bg_image_path": {"default": "", "type": "text", "group": "sleep",
                            "modes": ["sleep"],
                            "label": "背景图固定路径",
                            "help": "本地图片绝对路径；开启背景图后填了=所有视频共用该固定图，留空=按本期主题 AI 生成"},
    "sleep_bg_opacity": {"default": 20, "type": "number", "group": "sleep",
                         "modes": ["sleep"],
                         "label": "背景图不透明度(%)",
                         "help": "0-100，默认 20=图片以 20% 不透明度衬在渐变背景上；建议 10-35 保持文字可读"},
    "sleep_4k_native": {"default": False, "type": "checkbox", "group": "sleep",
                        "modes": ["sleep"],
                        "label": "原生 4K 渲染",
                        "help": "开启后卡片直接按 3840x2160 渲染、成片即 4K（文字像素级清晰，跳过 Step6 放大）；默认关=720p 合成后放大。开启会显著增加编码耗时"},
    "sleep_intro": {"default": True, "type": "checkbox", "group": "sleep",
                    "modes": ["sleep"],
                    "label": "片头",
                    "help": "开启=片头（频道名卡片或片头库视频+播报）开头；关闭=无 intro 段直接从第一组开始，片头库绑定同时失效（YouTube 章节同步去掉 Intro）"},
    "sleep_card_lead": {"default": 0.3, "type": "number", "group": "sleep",
                        "modes": ["sleep"],
                        "label": "卡片提前量(秒)",
                        "help": "每组画面先出现 N 秒再开始朗读（0-2，默认 0.3，0=出现即出声）；TTS 音频自带约 0.3 秒前导静音，实际停顿略长"},
    "sleep_xfade": {"default": False, "type": "checkbox", "group": "sleep",
                    "modes": ["sleep"],
                    "label": "组间交叉溶解",
                    "help": "开启后相邻块边界（组与组、片头/片尾衔接）画面交叉溶解过渡；仅画面、音频不动。整片多 1-2 次视频重编码，合成耗时明显增加"},
    "sleep_xfade_sec": {"default": 0.5, "type": "number", "group": "sleep",
                        "modes": ["sleep"],
                        "label": "溶解时长(秒)",
                        "help": "交叉溶解过渡时长（0.2-2.0，默认 0.5）；需开启「组间交叉溶解」，调小可避免与前组首句朗读重叠"},
    "sleep_font_scale": {"default": 100, "type": "number", "group": "sleep",
                         "modes": ["sleep"],
                         "label": "句子字号缩放(%)",
                         "help": "A/B 句英文/音标/中文整体缩放（60-160，默认 100=原大）"},
    "sleep_line_spacing": {"default": 14, "type": "number", "group": "sleep",
                           "modes": ["sleep"],
                           "label": "句子行距(px)",
                           "help": "英文多行之间的行距（0-48，默认 14，@720p 基准，高分辨率等比缩放）"},
    "sleep_letter_spacing": {"default": 0, "type": "number", "group": "sleep",
                             "modes": ["sleep"],
                             "label": "句子字距(px)",
                             "help": "英文字符间距（0-24，默认 0；同样作用于音标与中文）"},
    "sleep_bg_layer": {"default": "bottom", "type": "select", "group": "sleep",
                       "modes": ["sleep"],
                       "options": {"bottom": "底层衬底（默认：渐变之上、白卡之下）",
                                   "top": "第二级（盖过白卡/边框/叶片，文字仍在最上层）"},
                       "label": "背景图层级",
                       "help": "需开启「背景图片」；第二级建议不透明度 40-100 才有整幅背景效果，A/B 句、频道名、角标、序号始终绘制在背景图之上"},
    "sleep_sequence": {"default": "", "type": "textarea", "group": "sleep",
                       "modes": ["sleep"],
                       "label": "组内步骤序列 (JSON)",
                       "help": "朗读步骤编排 JSON（[{step, gap}]，step ∈ a_m/a_slow/b_m/b_slow/b_f/combo，gap=该步后停顿秒数或 null=沿用全局停顿参数）；留空=默认结构 a_m→a_slow→b_m→b_slow→combo；建议用「😴 Sleep 睡前短句」页的可视化编辑器修改"},

    # --- LLM ---
    "llm_provider": {"default": "sensenova", "type": "select", "group": "llm",
                     "label": "LLM Provider", "options": {
                         "sensenova": "SenseNova",
                         "gemini": "Gemini",
                         "openai": "OpenAI Compatible"}},
    "sensenova_api_key": {"default": "", "type": "password", "group": "llm",
                          "label": "SenseNova API Key",
                          "help": "LLM 与生图 Provider=sensenova 共用此 Key；LLM 走自定义通道时也可单独填写"},
    "sensenova_model": {"default": "deepseek-v4-flash", "type": "select", "group": "llm",
                        "label": "SenseNova Model", "options": ["deepseek-v4-flash", "glm-5.2"]},
    "openai_base_url": {"default": "https://x666.me/v1", "type": "text", "group": "llm",
                        "label": "OpenAI Base URL"},
    "openai_api_key": {"default": "", "type": "password", "group": "llm",
                      "label": "OpenAI API Key"},
    "openai_model": {"default": "grok-4.6", "type": "select", "group": "llm",
                    "label": "OpenAI Model", "options": [
                        "grok-4.6", "grok-4.5", "gemini-3.1-pro-preview",
                        "gemini-3.7-flash", "claude-sonnet-5", "gemini-2.5-pro-1m"]},
    "gemini_api_key": {"default": "", "type": "password", "group": "llm",
                      "label": "Gemini API Key",
                      "help": "llm_provider=gemini 时使用（google-genai SDK 直连 Google API）"},
    "gemini_model": {"default": "models/gemini-3.8-flash", "type": "select", "group": "llm",
                    "label": "Gemini Model", "options": [
                        "models/gemini-3.8-flash", "models/gemini-3.7-flash",
                        "models/gemini-3.6-flash", "models/gemini-3.5-flash",
                        "models/gemini-3.5-flash-lite", "models/gemini-3.1-flash-lite",
                        "models/gemini-3-flash", "models/gemini-2.5-flash",
                        "models/gemini-2.5-flash-lite"],
                    "help": "限流(429)时自动从新到旧降级到下一个更旧模型"},
    "llm_retries": {"default": 10, "type": "number", "group": "llm",
                    "label": "LLM 重试次数"},
    "llm_min_interval": {"default": 3, "type": "number", "group": "llm",
                         "label": "LLM 最小间隔(秒)"},
    "llm_proxy_enabled": {"default": False, "type": "checkbox", "group": "llm",
                          "label": "LLM API 走代理",
                          "help": "开启后当前 LLM Provider 的全部 API 调用走代理（Gemini/SenseNova/OpenAI 均生效）；MCP 生图/视频、TTS 等不受影响"},
    "llm_proxy_url": {"default": "http://127.0.0.1:7890", "type": "text", "group": "llm",
                     "label": "LLM 代理地址",
                     "help": "支持 http:// 与 socks5://、socks5h://（socks 系列 DNS 经代理解析）；例 http://127.0.0.1:7890 或 socks5://127.0.0.1:10308"},

    # --- 脚本质量增强（四开关独立，默认全关 = 原生成流程不变） ---

    # --- TTS ---
    "tts_engine": {"default": "kokoro", "type": "select", "group": "tts",
                   "label": "TTS 引擎", "options": {
                       "kokoro": "Kokoro (本地)",
                       "qwen": "Qwen3-TTS (本地 GPU)",
                       "moss": "MOSS-TTS-Nano (本地 CPU)"}},
    "qwen_model_path": {"default": r"H:\models\Qwen3-TTS-12Hz-0.6B-CustomVoice",
                        "type": "text", "group": "tts",
                        "label": "Qwen3-TTS 模型路径",
                        "help": "CustomVoice 模型 (预设音色)"},
    "qwen_base_model_path": {"default": r"H:\models\Qwen3-TTS-12Hz-1.7B-Base",
                             "type": "text", "group": "tts",
                             "label": "Qwen3-TTS Base 模型路径",
                             "help": "Base 模型 (voice clone 需要)"},
    "qwen_voicedesign_model_path": {"default": r"H:\models\Qwen3-TTS-12Hz-1.7B-VoiceDesign",
                                    "type": "text", "group": "tts",
                                    "label": "Qwen3-TTS VoiceDesign 模型路径",
                                    "help": "VoiceDesign 模型 (设计音色/英语女声需要)"},
    "qwen_device": {"default": "cuda:0", "type": "text", "group": "tts",
                    "label": "Qwen3-TTS 设备", "help": "如 cuda:0, cpu"},

    "moss_model_path": {"default": r"H:\models\MOSS-TTS-Nano-Model", "type": "text", "group": "tts",
                        "label": "MOSS-TTS-Nano 模型路径",
                        "help": "模型 checkpoint 目录"},
    "moss_tokenizer_path": {"default": r"H:\models\MOSS-Audio-Tokenizer-Nano", "type": "text", "group": "tts",
                            "label": "MOSS Audio Tokenizer 路径",
                            "help": "音频 Tokenizer 目录"},
    "moss_device": {"default": "cpu", "type": "text", "group": "tts",
                    "label": "MOSS-TTS 设备", "help": "如 cpu, cuda:0 (默认 CPU)"},
    "moss_repo_dir": {"default": r"H:\models\MOSS-TTS-Nano", "type": "text", "group": "tts",
                      "label": "MOSS-TTS-Nano 仓库目录",
                      "help": "包含 infer.py / moss_tts_nano_runtime.py 的仓库路径"},
    "moss_tts_temperature": {"default": 0.8, "type": "number", "group": "tts",
                             "label": "MOSS-TTS 采样温度",
                             "help": "越低越稳定（推荐 0.6-0.8），越高越有表现力；修改后自动重新生成 TTS 缓存"},
    "moss_tts_retry": {"default": 3, "type": "number", "group": "tts",
                       "label": "MOSS-TTS 重试次数",
                       "help": "每句合成失败/校验不达标时换 seed 重试的次数（1=不重试）"},
    "moss_tts_top_p": {"default": 0.95, "type": "number", "group": "tts",
                       "label": "MOSS-TTS Top-P",
                       "help": "音频核采样阈值（0.05-1.0），越低越稳定；修改后自动重新生成 TTS 缓存"},
    "moss_tts_top_k": {"default": 25, "type": "number", "group": "tts",
                       "label": "MOSS-TTS Top-K",
                       "help": "音频采样候选数（≥1），越小越稳定；修改后自动重新生成 TTS 缓存"},
    "moss_tts_rep_penalty": {"default": 1.2, "type": "number", "group": "tts",
                             "label": "MOSS-TTS 重复惩罚",
                             "help": "≥1.0，抑制重复/卡顿，过高会压音质；修改后自动重新生成 TTS 缓存"},
    "moss_tts_text_temperature": {"default": 1.0, "type": "number", "group": "tts",
                                  "label": "MOSS-TTS 文本温度",
                                  "help": "文本 token 采样温度（0.05-2.0），一般保持 1.0；修改后自动重新生成 TTS 缓存"},
    "moss_tts_greedy": {"default": False, "type": "checkbox", "group": "tts",
                        "label": "MOSS-TTS 贪心解码（最稳定）",
                        "help": "关闭随机采样，音色最稳定但语调略机械；勾选后温度/Top-P/Top-K 不生效；修改后自动重新生成 TTS 缓存"},

    # --- MCP / Image ---
    "mcp_tokens": {"default": "", "type": "textarea", "group": "mcp",
                   "label": "MCP Tokens", "help": "每行一个 token, 多 token 自动轮换"},
    "image_provider": {"default": "mcp", "type": "select", "group": "mcp",
                       "label": "生图 Provider", "options": ["mcp", "sensenova"],
                       "help": "mcp=TJGenerators(积分)；sensenova=SenseNova U1.5 Lite(API 计费, 复用 SenseNova API Key)"},
    "no_thumbnail": {"default": False, "type": "checkbox", "group": "mcp",
                     "label": "跳过缩略图",
                     "help": "勾选后 Step 4.5 只生成 YouTube 元数据，不生成缩略图图片（不消耗生图 API）"},
    "quick_test": {"default": False, "type": "checkbox", "group": "mcp",
                   "label": "⚡ 快速测试（零积分）",
                   "help": "勾选后：脚本直接复用同模式最近一次运行（无则回退同结构族）；"
                           "图片/音频/序列帧优先复制该运行现成产物，缺失处用黑色占位代替；"
                           "跳过 LLM/MCP/缩略图图/视频片段生成。主题词仅兜底命名，实际沿用上次标题"},
    "output_dir": {"default": str(WEB_ROOT / "output"), "type": "text", "group": "mcp",
                   "label": "输出目录"},
    "topics_file": {"default": str(PIPELINE_DIR / "topics.json"), "type": "text", "group": "content",
                    "label": "主题库文件"},
    "used_topics_file": {"default": "", "type": "text", "group": "content",
                         "label": "已用主题文件", "help": "留空=<output>/used_topics.json"},

    # --- BGM 版权音乐混合（Step 5.5，移植自 yt_aduio_book_one_to_all_v2/pipeline/bgm.py）---
    "bgm_mix": {"default": False, "type": "checkbox", "group": "bgm",
                "label": "运行时自动混入版权 BGM",
                "help": "Step 5 完成后自动把音乐库随机连串拼接混入成片音轨，"
                        "输出 {标题}_bgm.mp4 新文件（原片保留）；视频流零重编码。"
                        "关闭时仍可用「运行历史」页按钮手动混音"},
    "bgm_music_dir": {"default": str(WEB_ROOT / "bgm_music"), "type": "text", "group": "bgm",
                      "label": "音乐库路径",
                      "help": "版权 BGM 音乐文件夹（支持 mp3/wav/flac/ogg/m4a/aac/wma），"
                              "混音时随机打乱循环拼接至全片时长"},
    "bgm_ducking_mode": {"default": "sidechain", "type": "select", "group": "bgm",
                         "label": "混音模式",
                         "options": {"amix": "简单叠加 (amix)",
                                     "sidechain": "侧链压缩 (sidechain)",
                                     "sidechain_adaptive": "自适应侧链 (sidechain_adaptive)"},
                         "help": "sidechain=旁白说话时 BGM 自动压低、静默时升高，"
                                 "保留完整频率指纹供 YouTube Content ID 识别；"
                                 "adaptive=阈值随旁白 RMS 自适应（BGM/旁白比例恒定）；"
                                 "amix=固定增益叠加（用音量偏移/高通/动态音量/频谱塑形）"},
    "bgm_start_chapter": {"default": 1, "type": "number", "group": "bgm",
                          "label": "BGM 起始章节",
                          "help": "从第几章开始混 BGM（按 YouTube 章节顺序，1=从头，"
                                  "2=从第 2 章开头…实际章节以运行目录 youtube_metadata.json "
                                  "为准，越界/缺失自动从头；起点前仅旁白无 BGM）"},
    "bgm_base_gain_db": {"default": -15, "type": "number", "group": "bgm_sidechain",
                         "label": "BGM 基础增益 dB (sidechain)",
                         "help": "sidechain 模式下 BGM 的基础增益（仅 sidechain/adaptive 生效）"},
    "bgm_volume_offset_db": {"default": -25, "type": "number", "group": "bgm_amix",
                             "label": "BGM 音量偏移 dB (amix)",
                             "help": "amix 模式下 BGM 相对旁白 RMS 的音量偏移（仅 amix 生效）"},
    "bgm_fade_ms": {"default": 3000, "type": "number", "group": "bgm",
                    "label": "交叉淡入淡出 ms",
                    "help": "音乐片段间交叉淡化时长（毫秒）"},
    "bgm_intro_outro_seconds": {"default": 5, "type": "number", "group": "bgm_sidechain",
                                "label": "首尾独立段秒数",
                                "help": "旁白前后加静音段，给 Content ID 干净指纹参考"
                                        "（仅 sidechain 模式生效；BGM 起始章节 >1 时"
                                        "不加首部参考段，起点前天然干净）"},
    "bgm_highpass_freq": {"default": 150, "type": "number", "group": "bgm_amix",
                          "label": "高通滤波 Hz (amix)",
                          "help": "BGM 高通滤波截止频率，切掉低频鼓点干扰旁白（仅 amix 生效）"},
    "bgm_min_volume_db": {"default": -40, "type": "number", "group": "bgm",
                          "label": "BGM 最低音量 dB",
                          "help": "BGM 音量下限，防止过度压低"},
    "bgm_dynamic_volume": {"default": True, "type": "checkbox", "group": "bgm_amix",
                           "label": "动态音量包络 (amix)",
                           "help": "BGM 音量跟随旁白包络动态调整：旁白响处 BGM 更低、"
                                   "停顿处更高（仅 amix 生效）"},
    "bgm_spectral_shaping": {"default": True, "type": "checkbox", "group": "bgm_amix",
                             "label": "频谱空隙塑形 (amix)",
                             "help": "分析旁白频谱空隙，BGM 在旁白能量集中的频段自动让位"
                                     "（仅 amix 生效）"},
    "bgm_stereo_offset": {"default": 0.0, "type": "number", "group": "bgm",
                          "label": "立体声偏移",
                          "help": "BGM 声像偏移 -1..1（0=居中，所有混音模式生效）"},
    "bgm_sc_threshold_db": {"default": -30, "type": "number", "group": "bgm_sidechain",
                            "label": "侧链阈值 dB",
                            "help": "旁白超过该电平时压缩 BGM（仅 sidechain 生效）"},
    "bgm_sc_threshold_offset_db": {"default": -5, "type": "number", "group": "bgm_sidechain",
                                   "label": "自适应阈值偏移 dB",
                                   "help": "adaptive 模式阈值 = 旁白RMS + 该偏移"
                                           "（仅 sidechain_adaptive 生效）"},
    "bgm_sc_ratio": {"default": 8, "type": "number", "group": "bgm_sidechain",
                     "label": "侧链压缩比",
                     "help": "旁白说话时 BGM 压缩比 N:1（仅 sidechain 生效）"},
    "bgm_sc_attack_ms": {"default": 5, "type": "number", "group": "bgm_sidechain",
                         "label": "侧链起音 ms",
                         "help": "压缩器起音时间（毫秒，仅 sidechain 生效）"},
    "bgm_sc_release_ms": {"default": 400, "type": "number", "group": "bgm_sidechain",
                          "label": "侧链释放 ms",
                          "help": "压缩器释放时间（毫秒，仅 sidechain 生效）"},
}


def param_effective_in_mode(key: str, mode: str) -> bool:
    """参数在某模式下是否被管线实际消费（缺省全模式生效）。"""
    if mode not in MODES:
        return True
    spec = PARAM_SPEC.get(key)
    if spec is None:
        return False
    modes = spec.get("modes")
    return not modes or mode in modes


def effective_param_spec(mode: str) -> dict[str, dict]:
    """返回指定模式的生效参数子集（条目浅拷贝，不改动 PARAM_SPEC 原始对象）。

    仅用于配置页渲染过滤；保存/加载/API 仍基于完整 PARAM_SPEC
    （被过滤键保存后回落默认值，load_mode_config 合并补全）。
    """
    if mode not in MODES:
        return dict(PARAM_SPEC)
    return {k: dict(v) for k, v in PARAM_SPEC.items()
            if not v.get("modes") or mode in v["modes"]}

# Group display metadata
GROUP_META = {
    "content": {"label": "内容设置", "icon": "📝", "order": 1},
    "llm": {"label": "LLM 设置", "icon": "🤖", "order": 2},
    "tts": {"label": "TTS 语音", "icon": "🎙️", "order": 3},
    "mcp": {"label": "MCP / 图片", "icon": "🎨", "order": 4},
    "bgm": {"label": "BGM 音乐（通用）", "icon": "🎵", "order": 6},
    "bgm_amix": {"label": "BGM · amix 模式", "icon": "🎵", "order": 7},
    "bgm_sidechain": {"label": "BGM · sidechain 模式", "icon": "🎵", "order": 8},
    "sleep": {"label": "Sleep 睡前短句", "icon": "😴", "order": 9},
}


# --- 控制台「常用配置」面板自定义字段 ---
# 每模式独立一份字段清单（列表顺序 = 面板显示顺序）。存独立文件而非 mode_*.json：
# /api/config/save_all 会整文件覆盖模式配置，清单放那里会被配置页保存清掉。
QUICK_CONFIG_PATH = CONFIGS_DIR / "quick_config.json"
DEFAULT_QUICK_FIELDS = [
    "tts_engine", "sleep_pairs", "sleep_channel_name",
    "sleep_bg_image", "sleep_xfade", "bgm_mix", "quick_test",
]
# 面板上方快捷区已固定显示的键（structure 由模式标签决定），不允许重复挑选
EXCLUDED_QUICK_KEYS = {"structure", "topic", "cefr", "animation"}


def _valid_quick_fields(fields: Any) -> list[str]:
    """校验字段清单：仅保留 PARAM_SPEC 内且未被排除的键，去重保序。"""
    seen: set[str] = set()
    result: list[str] = []
    for key in fields if isinstance(fields, list) else []:
        if (isinstance(key, str) and key in PARAM_SPEC
                and key not in EXCLUDED_QUICK_KEYS and key not in seen):
            seen.add(key)
            result.append(key)
    return result


def _read_quick_config() -> dict[str, list[str]]:
    if not QUICK_CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(QUICK_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    modes = data.get("modes") if isinstance(data, dict) else None
    if not isinstance(modes, dict):
        return {}
    return {m: _valid_quick_fields(fields)
            for m, fields in modes.items() if m in MODES}


def load_quick_fields(mode: str) -> list[str]:
    """某模式的常用配置字段清单（缺文件/缺模式回默认清单；渲染时再按模式过滤生效性）。"""
    if mode not in MODES:
        raise ValueError(f"未知模式: {mode}")
    saved = _read_quick_config().get(mode)
    return list(saved) if saved is not None else list(DEFAULT_QUICK_FIELDS)


def load_all_quick_fields() -> dict[str, list[str]]:
    """全部模式的字段清单（控制台前端切模式不刷新页面，需一次带全）。"""
    saved = _read_quick_config()
    return {m: list(saved[m]) if m in saved else list(DEFAULT_QUICK_FIELDS)
            for m in MODES}


def save_quick_fields(mode: str, fields: list[str]) -> list[str]:
    """保存某模式字段清单（校验+去重保序+原子写），返回生效清单。"""
    if mode not in MODES:
        raise ValueError(f"未知模式: {mode}")
    valid = _valid_quick_fields(fields)
    data = _read_quick_config()
    data[mode] = valid
    QUICK_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUICK_CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"modes": data}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, QUICK_CONFIG_PATH)
    return valid


# --- Per-mode config storage ---
# 每种视频结构模式各一份完整独立配置，切换互不覆盖。
# default.json 仅作首次迁移源；active_mode.json 记录当前激活模式。

MODES = ["sleep"]



MODE_LABELS = {
    "sleep": "Sleep (睡觉听短句循环)",
}
ACTIVE_MODE_PATH = CONFIGS_DIR / "active_mode.json"

# --- 输出目录布局 ---
# 新布局：output/{mode}/{run_name}/，每种模式一个独立文件夹；
# 旧扁平布局 output/{run_name}/ 继续兼容读写。
# 回收站：output/_recycle_bin/（与模式文件夹平级的独立文件夹）。
RECYCLE_DIRNAME = "_recycle_bin"
LEGACY_RECYCLE_DIRNAME = ".recycle_bin"  # 旧版回收站（兼容恢复/清空）

MODE_SHORT_LABELS = {
    "sleep": "Sleep",
}


def _is_safe_run_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


def find_run_dir(output_dir: Path | str, name: str, mode_hint: str = "") -> Path | None:
    """按运行名查找运行目录（兼容新旧两种布局）。

    查找顺序：mode_hint 指定的模式文件夹 → 旧扁平布局 → 各模式文件夹。
    同名运行跨模式重名时以 hint 消歧；未找到返回 None。
    """
    root = Path(output_dir)
    if not _is_safe_run_name(name):
        return None
    if mode_hint in MODES:
        p = root / mode_hint / name
        if p.is_dir():
            return p
    if name not in MODES and name not in (RECYCLE_DIRNAME, LEGACY_RECYCLE_DIRNAME):
        p = root / name
        if p.is_dir():
            return p
    for mode in MODES:
        p = root / mode / name
        if p.is_dir():
            return p
    return None


def iter_run_dirs(output_dir: Path | str) -> list[Path]:
    """遍历输出目录下所有运行目录（新旧布局，按修改时间倒序）。

    新布局取模式文件夹下一层；旧扁平布局取根目录一层
    （模式文件夹与回收站文件夹除外，不要求 script.json 已生成）。
    """
    root = Path(output_dir)
    runs: list[Path] = []
    if not root.is_dir():
        return runs
    for entry in root.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name in (RECYCLE_DIRNAME, LEGACY_RECYCLE_DIRNAME):
            continue
        if entry.name in MODES:
            runs.extend(d for d in entry.iterdir()
                        if d.is_dir() and not d.name.startswith("."))
        else:
            runs.append(entry)  # 旧扁平布局运行目录
    runs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return runs


def _mode_config_path(mode: str) -> Path:
    return CONFIGS_DIR / f"mode_{mode}.json"


def _read_legacy_default() -> dict[str, Any] | None:
    """Read legacy default.json (migration source only)."""
    if DEFAULT_CONFIG_PATH.exists():
        try:
            return json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def get_active_mode() -> str:
    if ACTIVE_MODE_PATH.exists():
        try:
            mode = json.loads(ACTIVE_MODE_PATH.read_text(encoding="utf-8")).get("mode", "sleep")
            if mode in MODES:
                return mode
        except (json.JSONDecodeError, OSError):
            pass
    return "sleep"


def set_active_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"未知模式: {mode}")
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_MODE_PATH.write_text(
        json.dumps({"mode": mode}, ensure_ascii=False, indent=2), encoding="utf-8")
    return mode


def load_mode_config(mode: str) -> dict[str, Any]:
    """Load full config for a mode; lazily initialize from legacy default.json."""
    if mode not in MODES:
        raise ValueError(f"未知模式: {mode}")
    path = _mode_config_path(mode)
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            saved = None
    else:
        saved = None
    if saved is None:
        # 首次：继承现有全局配置（或出厂默认）
        base = _read_legacy_default() or get_default_config()
        saved = dict(base)
    merged = get_default_config()
    merged.update(saved)
    # API Key 兜底：模式文件若 seed 于 default.json 尚无 key 的时期，
    # 空字符串会永久遮蔽后续填入 default.json 的 key —— 空 key 回落 legacy 值
    # （仅 allowlist 两个 key 字段；topic/used_topics_file 等空串是合法业务值不做回落）
    legacy = _read_legacy_default() or {}
    for _k in ("sensenova_api_key", "openai_api_key"):
        if not merged.get(_k) and legacy.get(_k):
            merged[_k] = legacy[_k]
    merged["structure"] = mode  # 结构由模式文件决定，恒等于文件名
    save_mode_config(mode, merged)
    return merged


def save_mode_config(mode: str, config: dict[str, Any]) -> None:
    if mode not in MODES:
        raise ValueError(f"未知模式: {mode}")
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    config = dict(config)
    config["structure"] = mode  # 防止串模式
    # 原子写：多进程（Web 服务/CLI/并行会话）并发 read-merge-write 下
    # 防止读到半截 JSON 触发整体兜底链
    path = _mode_config_path(mode)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def resolve_mcp_tokens(mode: str = "") -> str:
    """MCP token 解析链：模式配置 → legacy default.json → 本机 CLI 检测。

    模式文件 seed 时期不同，mcp_tokens 可能为空串（如 sleep），空值依次回落
    legacy default.json（用户实际维护 token 的地方）与本机 Codely CLI OAuth
    token。返回原始串（多行 token，调用方自行按行/逗号切分）。
    """
    cfg = load_mode_config(mode) if mode in MODES else load_config()
    raw = str(cfg.get("mcp_tokens", "") or "").strip()
    if raw:
        return raw
    legacy = _read_legacy_default() or {}
    raw = str(legacy.get("mcp_tokens", "") or "").strip()
    if raw:
        return raw
    return str(detect_local_mcp_token() or "").strip()


def load_all_mode_configs() -> dict[str, dict[str, Any]]:
    return {mode: load_mode_config(mode) for mode in MODES}


def get_default_config() -> dict[str, Any]:
    return {k: v["default"] for k, v in PARAM_SPEC.items()}


def load_config() -> dict[str, Any]:
    """Current active mode's config (all pages follow the active mode)."""
    return load_mode_config(get_active_mode())


def save_config(config: dict[str, Any]) -> None:
    save_mode_config(get_active_mode(), config)


def list_presets() -> list[str]:
    if not CONFIGS_DIR.exists():
        return []
    return sorted([
        f.stem for f in CONFIGS_DIR.glob("*.json")
        if f.name != "default.json"
    ])


def save_preset(name: str, config: dict[str, Any]) -> None:
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c for c in name if c.isalnum() or c in "-_") or "preset"
    path = CONFIGS_DIR / f"{safe_name}.json"
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def load_preset(name: str) -> dict[str, Any]:
    path = CONFIGS_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Preset '{name}' not found")
    return json.loads(path.read_text(encoding="utf-8"))


def delete_preset(name: str) -> None:
    path = CONFIGS_DIR / f"{name}.json"
    if path.exists():
        path.unlink()


# --- Sleep 配色组合（配置页 😴 Sleep 组「随机配色/保存配色」）---

SLEEP_COLORS_DIR = CONFIGS_DIR / "sleep_color_presets"


def _sanitize_combo_name(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in "-_") or "combo"


def list_sleep_color_presets() -> list[dict[str, Any]]:
    """全部配色组合 [{name, colors}]，按名称排序；单文件损坏跳过。"""
    if not SLEEP_COLORS_DIR.exists():
        return []
    out = []
    for f in sorted(SLEEP_COLORS_DIR.glob("*.json"), key=lambda p: p.stem):
        try:
            colors = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(colors, dict):
            out.append({"name": f.stem, "colors": colors})
    return out


def save_sleep_color_preset(name: str, colors: dict[str, str]) -> str:
    """保存/覆盖一个配色组合（颜色键→hex），返回落盘名称。"""
    SLEEP_COLORS_DIR.mkdir(parents=True, exist_ok=True)
    safe = _sanitize_combo_name(name)
    (SLEEP_COLORS_DIR / f"{safe}.json").write_text(
        json.dumps(colors, ensure_ascii=False, indent=2), encoding="utf-8")
    return safe


def load_sleep_color_preset(name: str) -> dict[str, str]:
    path = SLEEP_COLORS_DIR / f"{_sanitize_combo_name(name)}.json"
    if not path.exists():
        raise FileNotFoundError(f"Sleep color preset '{name}' not found")
    return json.loads(path.read_text(encoding="utf-8"))


def delete_sleep_color_preset(name: str) -> bool:
    path = SLEEP_COLORS_DIR / f"{_sanitize_combo_name(name)}.json"
    if path.exists():
        path.unlink()
        return True
    return False


# --- Custom LLM Providers ---

LLM_PROVIDERS_PATH = CONFIGS_DIR / "llm_providers.json"


def load_llm_providers() -> list[dict]:
    """Load custom LLM providers from configs/llm_providers.json."""
    if not LLM_PROVIDERS_PATH.exists():
        return []
    try:
        data = json.loads(LLM_PROVIDERS_PATH.read_text(encoding="utf-8"))
        return data.get("providers", [])
    except (json.JSONDecodeError, OSError):
        return []


def save_llm_providers(providers: list[dict]) -> None:
    LLM_PROVIDERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LLM_PROVIDERS_PATH.write_text(
        json.dumps({"providers": providers}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def get_provider_options() -> dict[str, str]:
    """Return all LLM provider options (static + custom) as {value: label}."""
    options = {
        "sensenova": "SenseNova",
        "gemini": "Gemini（google-genai）",
        "openai": "OpenAI Compatible (内置)",
    }
    custom = load_llm_providers()
    for p in custom:
        options[f"custom:{p['id']}"] = p["name"]
    return options


def resolve_provider(config: dict[str, Any]) -> tuple[str, str, str, str]:
    """Resolve LLM provider config → (provider_type, base_url, api_key, model).

    For custom:* providers, reads from llm_providers.json.
    Returns provider_type as 'sensenova' or 'openai' (custom always → openai).
    """
    provider = config.get("llm_provider", "sensenova")
    if provider == "sensenova":
        return (
            "sensenova",
            "https://token.sensenova.cn/v1",
            config.get("sensenova_api_key", ""),
            config.get("sensenova_model", "deepseek-v4-flash"),
        )
    elif provider == "gemini":
        return (
            "gemini",
            "",
            config.get("gemini_api_key", ""),
            config.get("gemini_model", "models/gemini-3.8-flash"),
        )
    elif provider.startswith("custom:"):
        custom_id = provider.split(":", 1)[1]
        customs = load_llm_providers()
        cp = next((p for p in customs if p["id"] == custom_id), None)
        if cp:
            models = cp.get("models") or []
            model = config.get("openai_model") or ""
            if model not in models:
                # 配置里的 openai_model 不属于该 Provider（如内置 OpenAI 的模型）→ 用第一个
                model = models[0] if models else ""
            return (
                "openai",
                cp.get("base_url", ""),
                cp.get("api_key", ""),
                model,
            )
    # Default: openai
    return (
        "openai",
        config.get("openai_base_url", "https://x666.me/v1"),
        config.get("openai_api_key", ""),
        config.get("openai_model", "grok-4.6"),
    )


def _extract_token(entry: dict) -> str | None:
    """从单条 token 文件条目中提取 accessToken（兼容多种字段布局）。"""
    tok = entry.get("token")
    if isinstance(tok, dict):
        at = tok.get("accessToken") or tok.get("access_token")
        if at and isinstance(at, str) and len(at) > 10:
            return at
    elif isinstance(tok, str) and len(tok) > 10:
        return tok
    for key in ("access_token", "accessToken"):
        val = entry.get(key)
        if val and isinstance(val, str) and len(val) > 10:
            return val
    return None


def detect_local_mcp_token() -> str | None:
    """Try to read MCP OAuth token from Codely CLI config.

    优先精确匹配 TJGenerators server（本项目唯一的 MCP 图片生成服务），
    避免将来配置其他 MCP server 后误取别家 token；找不到再宽松兜底。
    """
    home = Path.home()
    token_file = home / ".codely-cli" / "mcp-oauth-tokens.json"
    if not token_file.exists():
        return None
    try:
        data = json.loads(token_file.read_text(encoding="utf-8"))
        # Format: [{"serverName": "...", "token": {"accessToken": "...", ...}, ...}]
        if isinstance(data, list):
            # 1. 优先 TJGenerators
            for entry in data:
                if (isinstance(entry, dict)
                        and entry.get("serverName") == "TJGenerators"):
                    at = _extract_token(entry)
                    if at:
                        return at
            # 2. 宽松兜底：任意条目的 token
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                at = _extract_token(entry)
                if at:
                    return at
        # Format: {"serverName": {"token": "..."}, ...} or {"token": "..."}
        if isinstance(data, dict):
            for key in ("access_token", "token", "accessToken"):
                if key in data:
                    val = data[key]
                    if isinstance(val, str) and len(val) > 10:
                        return val
                    if isinstance(val, dict):
                        at = val.get("accessToken") or val.get("access_token")
                        if at and isinstance(at, str):
                            return at
            for v in data.values():
                if isinstance(v, str) and len(v) > 20:
                    return v
                if isinstance(v, dict):
                    at = v.get("accessToken") or v.get("access_token") or v.get("token")
                    if at and isinstance(at, str) and len(at) > 10:
                        return at
        return None
    except (json.JSONDecodeError, OSError):
        return None


def structure_family(mode: str) -> str:
    """本项目仅 sleep 模式：恒等返回（保留函数以兼容 pipeline_service 调用）。"""
    return mode
