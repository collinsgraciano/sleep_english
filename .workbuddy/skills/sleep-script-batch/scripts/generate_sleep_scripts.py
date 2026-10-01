#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sleep-script-batch (AI 模式) —— 批量生成「睡觉听英语」视频脚本（真 AI 生成）。

与模板版本不同：本版本**不使用 Python 模板套词**，而是调用项目自带的
pipeline/sleep/llm_client_sleep.generate_sleep_script，让 LLM 针对每个主题
**真正生成** 400 行（200 组 A/B）与其主题相关的完整、自然、不重复的英语短句，
含 IPA 音标 + 繁体中文翻译，以及全套 YouTube 标题/简介/标签/缩略图文案。

每个主题调用 use_cache=False，保证「每次生成都重新用 AI 生成」全新内容。

产出与 sleep_english pipeline (pipeline/pipeline.py -> _step0_script / --resume)
完全兼容的 script.json：每个主题一个「运行目录」，内含 script.json +
images/audio/clips/subtitles/videos 子目录 + checkpoint.json（已标记
step0_script 完成），可直接被 pipeline 用 --resume 续跑 TTS / 合成 / 4K 生成视频。

依赖：
  - 项目 pipeline/llm_client.py 的 _chat（SenseNova / OpenAI / Gemini / WBK）
  - LLM 凭证与参数从 configs/default.json 读取并注入环境变量（无需手动配置）
用法：
  python generate_sleep_scripts.py --out batch_scripts --count 100 --lines 400
  python generate_sleep_scripts.py --out batch_scripts --count 10 --start 0   # 续跑/分批
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# ===========================================================================
# 1) 加载项目 LLM 配置到环境变量（llm_client._chat 经 _env_get 读 os.environ）
# ===========================================================================
_PROJ = Path(__file__).resolve().parents[4]          # .../sleep_english
_CFG_PATH = _PROJ / "configs" / "default.json"

# 配置键 -> 环境变量名（大小写不敏感地在 default.json 中递归查找）
_ENV_MAP = {
    "llm_provider": "LLM_PROVIDER",
    "sensenova_api_key": "SENSENOVA_API_KEY",
    "sensenova_model": "SENSENOVA_MODEL",
    "sensenova_base": "SENSENOVA_BASE",
    "openai_base_url": "OPENAI_BASE_URL",
    "openai_api_key": "OPENAI_API_KEY",
    "openai_model": "OPENAI_MODEL",
    "gemini_api_key": "GEMINI_API_KEY",
    "gemini_model": "GEMINI_MODEL",
    "llm_min_interval": "LLM_MIN_INTERVAL",
    "llm_retries": "LLM_RETRIES",
}


def _deep_find(d, key):
    """在（可能嵌套的）dict 中递归查找键，返回首个匹配值。"""
    if isinstance(d, dict):
        lk = key.lower()
        for k, v in d.items():
            if str(k).lower() == lk:
                return v
        for v in d.values():
            r = _deep_find(v, key)
            if r is not None:
                return r
    return None


def _load_llm_env():
    if not _CFG_PATH.exists():
        print(f"  [WARN] 未找到 {_CFG_PATH}，依赖已设置的环境变量")
        return
    try:
        cfg = json.loads(_CFG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [WARN] 读取 {_CFG_PATH} 失败：{e}")
        return

    # ---- sensenova -> openai 兼容通道映射（_chat 无 sensenova 分支）----
    # 凭证可能来自「环境变量」或「config」中的 SENSENOVA_*；_chat 实际读取
    # OPENAI_*。这里从多来源收集并强制映射，避免依赖继承环境的瞬时可用性。
    def _first(*cands):
        for c in cands:
            if c and str(c).strip():
                return str(c).strip()
        return ""

    env_base = os.environ.get("OPENAI_BASE_URL", "") or os.environ.get("SENSENOVA_BASE_URL", "")
    cfg_base_url = _deep_find(cfg, "sensenova_base_url") or ""
    cfg_base_path = _deep_find(cfg, "sensenova_base") or ""
    env_key = os.environ.get("OPENAI_API_KEY", "") or os.environ.get("SENSENOVA_API_KEY", "")
    cfg_key = _deep_find(cfg, "sensenova_api_key") or ""
    env_model = os.environ.get("OPENAI_MODEL", "") or os.environ.get("SENSENOVA_MODEL", "")
    cfg_model = _deep_find(cfg, "sensenova_model") or ""

    base = _first(env_base, cfg_base_url, cfg_base_path if cfg_base_path.startswith("http") else "")
    if not base and cfg_base_path.startswith("/"):
        base = "https://api.sensenova.cn/v1/llm" + cfg_base_path
    key = _first(env_key, cfg_key)
    model = _first(env_model, cfg_model)

    if base:
        os.environ["OPENAI_BASE_URL"] = base
    if key:
        os.environ["OPENAI_API_KEY"] = key
    if model:
        os.environ["OPENAI_MODEL"] = model
    os.environ["LLM_PROVIDER"] = "openai"   # 强制走 openai 兼容分支

    # 退回 wbk：若没有任何 openai(sensenova) 凭证，但环境有 WBK_API_KEY，
    # 则改用 wbk provider（base URL 硬编码，无需 host 配置）。
    wbk_key = os.environ.get("WBK_API_KEY", "")
    if not (base and key) and wbk_key:
        os.environ["LLM_PROVIDER"] = "wbk"
        os.environ["WBK_API_KEY"] = wbk_key
        os.environ.setdefault("WBK_MODEL", "cn:auto")
        os.environ.setdefault("WBK_THINKING", "default")
        print("  [LLM] 无 openai(sensenova) 凭证，退回 wbk provider")

    # 调试：把非密钥信息写到 debug 文件，便于排查（来源 + host/model，不记 key）
    try:
        dbg = {
            "env_has_OPENAI_BASE_URL": bool(env_base),
            "cfg_has_sensenova_base_url": bool(cfg_base_url),
            "cfg_has_sensenova_base": bool(cfg_base_path),
            "env_has_OPENAI_API_KEY": bool(env_key),
            "cfg_has_sensenova_api_key": bool(cfg_key),
            "env_has_OPENAI_MODEL": bool(env_model),
            "cfg_has_sensenova_model": bool(cfg_model),
            "env_has_WBK_API_KEY": bool(wbk_key),
            "resolved_OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL", ""),
            "resolved_OPENAI_MODEL": os.environ.get("OPENAI_MODEL", ""),
            "resolved_LLM_PROVIDER": os.environ.get("LLM_PROVIDER", ""),
        }
        (_PROJ / "ai_scripts" / "_llm_debug.json").write_text(
            json.dumps(dbg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    print(f"  [LLM] provider={os.environ.get('LLM_PROVIDER')} "
          f"base={os.environ.get('OPENAI_BASE_URL')} "
          f"model={os.environ.get('OPENAI_MODEL')} "
          f"min_interval={os.environ.get('LLM_MIN_INTERVAL', '5')}s")

    # 凭证前置检查：没有任何可用通道时，提前给出清晰报错（避免卡在底层
    # "unknown url type: '/chat/completions'" 这种晦涩错误）。
    has_openai = bool(os.environ.get("OPENAI_BASE_URL")) and bool(os.environ.get("OPENAI_API_KEY"))
    has_wbk = bool(os.environ.get("WBK_API_KEY"))
    if not has_openai and not has_wbk:
        print("  [ERROR] 未找到任何 LLM 凭证（OPENAI_BASE_URL+OPENAI_API_KEY "
              "或 WBK_API_KEY 均不存在）。\n"
              "          configs/default.json 仅含 llm_provider=sensenova，"
              "实际 base url / api key / model 由运行环境的变量提供。\n"
              "          请在「已注入 LLM 凭证」的环境中运行本脚本"
              "（例如你本地/你的 WorkBuddy 会话中），\n"
              "          或在 configs/default.json 里补全 sensenova_base_url /"
              " sensenova_api_key / sensenova_model。")
        sys.exit(2)


# ===========================================================================
# 2) 将 pipeline 加入 sys.path，便于 import llm_client / llm_client_sleep
# ===========================================================================
sys.path.insert(0, str(_PROJ / "pipeline"))
sys.path.insert(0, str(_PROJ / "pipeline" / "sleep"))

from llm_client_sleep import generate_sleep_script  # noqa: E402


# ===========================================================================
# 3) 100 个不重复热门主题（en, 简中, 分类键），跨 29 类覆盖
# ===========================================================================
THEMES = [
    # Daily Life x4
    ("Making Breakfast", "做早餐", "Daily Life"),
    ("Doing Laundry", "洗衣服", "Daily Life"),
    ("Cleaning the House", "打扫房子", "Daily Life"),
    ("Walking the Dog", "遛狗", "Daily Life"),
    # Travel x4
    ("At the Airport", "在机场", "Travel"),
    ("Booking a Hotel", "订酒店", "Travel"),
    ("Asking for Directions", "问路", "Travel"),
    ("Renting a Car", "租车", "Travel"),
    # Restaurant x4
    ("Making a Reservation", "订位", "Restaurant"),
    ("Reading the Menu", "看菜单", "Restaurant"),
    ("Ordering Food", "点餐", "Restaurant"),
    ("Paying the Bill", "付账", "Restaurant"),
    # Shopping x4
    ("Grocery Shopping", "买菜", "Shopping"),
    ("Trying on Clothes", "试穿衣服", "Shopping"),
    ("Returning an Item", "退货", "Shopping"),
    ("Comparing Prices", "比价", "Shopping"),
    # Work x4
    ("Sending an Email", "发邮件", "Work"),
    ("Joining a Meeting", "开会", "Work"),
    ("Finishing a Report", "完成报告", "Work"),
    ("Asking for Help", "求助", "Work"),
    # School x4
    ("Doing Homework", "写作业", "School"),
    ("Taking Notes", "做笔记", "School"),
    ("Studying for a Test", "复习考试", "School"),
    ("Reading a Book", "看书", "School"),
    # Health x4
    ("Drinking Water", "喝水", "Health"),
    ("Taking a Walk", "散步", "Health"),
    ("Seeing a Doctor", "看医生", "Health"),
    ("Washing Hands", "洗手", "Health"),
    # Social x4
    ("Saying Hello", "打招呼", "Social"),
    ("Making Small Talk", "闲聊", "Social"),
    ("Planning a Party", "筹备派对", "Social"),
    ("Introducing a Friend", "介绍朋友", "Social"),
    # Transportation x4
    ("Taking the Bus", "搭公车", "Transportation"),
    ("Buying a Ticket", "买票", "Transportation"),
    ("Calling a Taxi", "叫计程车", "Transportation"),
    ("Parking the Car", "停车", "Transportation"),
    # Bank & Post x3
    ("Opening an Account", "开户", "Bank & Post"),
    ("Sending a Package", "寄包裹", "Bank & Post"),
    ("Exchanging Money", "换钱", "Bank & Post"),
    # Weather x3
    ("Checking the Forecast", "看天气预报", "Weather"),
    ("Bringing an Umbrella", "带伞", "Weather"),
    ("Shoveling Snow", "铲雪", "Weather"),
    # Technology x4
    ("Charging the Phone", "手机充电", "Technology"),
    ("Updating the App", "更新应用", "Technology"),
    ("Sending a Message", "传讯息", "Technology"),
    ("Resetting Password", "重设密码", "Technology"),
    # Entertainment x4
    ("Watching a Movie", "看电影", "Entertainment"),
    ("Listening to Music", "听音乐", "Entertainment"),
    ("Playing a Game", "玩游戏", "Entertainment"),
    ("Singing a Song", "唱歌", "Entertainment"),
    # Family x4
    ("Setting the Table", "摆碗筷", "Family"),
    ("Feeding the Baby", "喂宝宝", "Family"),
    ("Cooking for Family", "为家人做饭", "Family"),
    ("Calling Grandma", "打电话给奶奶", "Family"),
    # Housing x3
    ("Fixing a Leak", "修漏水", "Housing"),
    ("Painting the Wall", "油漆墙壁", "Housing"),
    ("Sweeping the Floor", "扫地", "Housing"),
    # Emergency x3
    ("Calling 911", "打911", "Emergency"),
    ("Using Fire Exit", "走消防安全门", "Emergency"),
    ("Staying Calm", "保持冷静", "Emergency"),
    # Grooming x3
    ("Brushing Teeth", "刷牙", "Grooming"),
    ("Washing Face", "洗脸", "Grooming"),
    ("Combing Hair", "梳头", "Grooming"),
    # Customer Service x3
    ("Returning a Product", "退产品", "Customer Service"),
    ("Tracking an Order", "查订单", "Customer Service"),
    ("Requesting a Refund", "申请退款", "Customer Service"),
    # Finance x3
    ("Saving Money", "存钱", "Finance"),
    ("Paying Rent", "付房租", "Finance"),
    ("Splitting the Bill", "分账", "Finance"),
    # Cooking x4
    ("Chopping Onions", "切洋葱", "Cooking"),
    ("Frying an Egg", "煎蛋", "Cooking"),
    ("Baking a Cake", "烤蛋糕", "Cooking"),
    ("Washing Dishes", "洗碗", "Cooking"),
    # Pet x3
    ("Feeding the Cat", "喂猫", "Pet"),
    ("Taking to Vet", "带去看兽医", "Pet"),
    ("Training the Puppy", "训练小狗", "Pet"),
    # Dating x3
    ("Sending a Text", "传讯息", "Dating"),
    ("Choosing a Place", "选地点", "Dating"),
    ("Saying Goodnight", "说晚安", "Dating"),
    # Culture x3
    ("Learning Customs", "学习习俗", "Culture"),
    ("Trying Local Food", "试当地食物", "Culture"),
    ("Greeting Elders", "向长辈问好", "Culture"),
    # Work POV x3
    ("Leading a Project", "带领专案", "Work POV"),
    ("Handling Clients", "处理客户", "Work POV"),
    ("Managing a Team", "管理团队", "Work POV"),
    # Daily Mishaps x3
    ("Spilling Coffee", "打翻咖啡", "Daily Mishaps"),
    ("Losing Keys", "弄丢钥匙", "Daily Mishaps"),
    ("Missing the Bus", "错过公车", "Daily Mishaps"),
    # Culture Q&A x3
    ("Asking About Holidays", "问节庆", "Culture Q&A"),
    ("Comparing Festivals", "比较节日", "Culture Q&A"),
    ("Learning History", "学历史", "Culture Q&A"),
    # Functional Language x3
    ("Making a Request", "提出请求", "Functional Language"),
    ("Giving Suggestions", "给建议", "Functional Language"),
    ("Apologizing", "道歉", "Functional Language"),
    # Classroom English x3
    ("Opening Your Book", "打开课本", "Classroom English"),
    ("Answering Questions", "回答问题", "Classroom English"),
    ("Handing In Homework", "交作业", "Classroom English"),
    # Presentations x3
    ("Starting the Slides", "开始简报", "Presentations"),
    ("Explaining Charts", "解说图表", "Presentations"),
    ("Thanking the Audience", "感谢听众", "Presentations"),
]


# ===========================================================================
# 工具函数
# ===========================================================================
def safe_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")
    return s[:48] or "topic"


def build_script_ai(en: str, zh: str, cat: str, num_lines: int, cache_dir: str) -> dict:
    """调用项目真 AI 脚本生成器，针对主题生成完整 400 行脚本。

    每个主题 use_cache=False，保证每次都重新用 AI 生成全新内容。
    返回的 script dict 与 pipeline _validate_script 兼容。
    """
    num_pairs = num_lines // 2  # 400 行 = 200 组
    script = generate_sleep_script(
        topic=en,
        cefr="A2",
        num_pairs=num_pairs,
        batch_pairs=50,          # 项目验证过的稳妥批次大小
        cache_dir=cache_dir,
        use_cache=False,         # 关键：每次重新 AI 生成
    )

    # 字段补全（保证后续 TTS / 合成 / YouTube 元数据齐全）
    script["scene"] = script.get("scene") or en
    script.setdefault("channel_id", "")
    if not script.get("title"):
        script["title"] = f"Everyday Phrases — {en}"
    if not script.get("youtube_title"):
        script["youtube_title"] = (
            f"【睡前英文聽力】 {en} ｜ 不用背！睡覺聽就會 ｜ 🌱A2 ｜ "
            f"💡{num_lines}句循環聽")
    if not script.get("youtube_title_en"):
        script["youtube_title_en"] = (
            f"{num_lines} Everyday English Phrases While You Sleep — {en}")
    if not script.get("topic"):
        script["topic"] = en
    return script


def validate(script: dict, num_lines: int):
    d = script.get("dialogue", [])
    if len(d) < num_lines:
        return False, f"dialogue {len(d)} < {num_lines}"
    for i, ln in enumerate(d):
        if not ln.get("text", "").strip():
            return False, f"line {i} empty text"
        if not ln.get("zh", "").strip():
            return False, f"line {i} empty zh"
        if not ln.get("phonetic", "").strip():
            return False, f"line {i} empty phonetic"
        if not ln.get("speaker", ""):
            return False, f"line {i} empty speaker"
    return True, ""


def main():
    ap = argparse.ArgumentParser(description="批量 AI 生成 sleep_english script.json（真 LLM）")
    ap.add_argument("--out", default="batch_scripts", help="输出根目录")
    ap.add_argument("--count", type=int, default=100, help="生成主题数")
    ap.add_argument("--lines", type=int, default=400, help="每个脚本 dialogue 行数(应为偶数)")
    ap.add_argument("--start", type=int, default=0, help="从第几个主题开始(0-based)")
    args = ap.parse_args()

    _load_llm_env()

    args.lines = max(2, (args.lines // 2) * 2)  # 保证偶数
    themes = THEMES[args.start: args.start + args.count]
    if not themes:
        print("没有可用主题（count/start 超出范围）")
        sys.exit(1)

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    llm_cache = out_root / ".llm_cache"
    llm_cache.mkdir(parents=True, exist_ok=True)

    manifest_path = out_root / "manifest.json"
    # 读取已有 manifest（仅保留「索引 <= start」的旧条目，支持续跑累加；
    # 全新运行 start=0 时全部丢弃，避免旧 manifest 残留造成重复）
    manifest = []
    if manifest_path.exists():
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
            old_themes = old.get("themes", []) if isinstance(old, dict) else []
            manifest = [t for t in old_themes
                        if isinstance(t, dict) and t.get("index", 0) <= args.start]
        except Exception:
            manifest = []

    ok, fail = 0, 0
    failed_themes = []
    t0 = time.time()

    for gi, (en, zh, cat) in enumerate(themes):
        gi_global = args.start + gi
        folder = out_root / f"{gi_global + 1:03d}_{safe_name(en)}"
        folder.mkdir(parents=True, exist_ok=True)
        for sub in ("images", "audio", "clips", "subtitles", "videos"):
            (folder / sub).mkdir(exist_ok=True)

        # 续跑优化：若该主题已有通过校验的 AI 脚本，则跳过（避免重复消耗 LLM 额度）
        prior = folder / "script.json"
        if prior.exists():
            try:
                old = json.loads(prior.read_text(encoding="utf-8"))
                vok, _ = validate(old, args.lines)
                if vok and old.get("topic") == en:
                    print(f"  [SKIP] {en} (已有有效脚本，跳过)")
                    ok += 1
                    manifest.append({
                        "index": gi_global + 1, "en": en, "zh": zh,
                        "category": cat, "folder": folder.name,
                        "lines": len(old.get("dialogue", [])),
                    })
                    manifest_path.write_text(json.dumps({
                        "count": ok, "failed": fail, "lines_per_script": args.lines,
                        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "themes": manifest,
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    continue
            except Exception:
                pass

        theme_t0 = time.time()
        try:
            script = build_script_ai(en, zh, cat, args.lines, str(llm_cache))
        except Exception as e:
            fail += 1
            failed_themes.append((en, str(e)[:200]))
            print(f"  [FAIL] {en}: {type(e).__name__}: {str(e)[:160]}")
            continue

        valid, msg = validate(script, args.lines)
        if not valid:
            fail += 1
            failed_themes.append((en, "validate: " + msg))
            print(f"  [FAIL] {en}: {msg}")
            continue

        (folder / "script.json").write_text(
            json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        cp = {
            "completed_steps": ["step0_script"],
            "topic": en, "cefr": "A2", "structure": "sleep", "channel_id": "",
            "_run_dir": str(folder.resolve()), "timestamp": ts,
        }
        (folder / "checkpoint.json").write_text(
            json.dumps(cp, ensure_ascii=False, indent=2), encoding="utf-8")

        ok += 1
        manifest.append({
            "index": gi_global + 1, "en": en, "zh": zh, "category": cat,
            "folder": folder.name, "lines": len(script["dialogue"]),
        })
        dt = int(time.time() - theme_t0)
        print(f"  [{gi + 1}/{len(themes)}] {en} -> {folder.name} "
              f"({len(script['dialogue'])} lines, {dt}s)")

        # 每主题后落盘 manifest（中断可续跑，已成功项不丢失）
        manifest_path.write_text(json.dumps({
            "count": ok, "failed": fail, "lines_per_script": args.lines,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "themes": manifest,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    total = int(time.time() - t0)
    print(f"\n完成：成功 {ok} 个，失败 {fail} 个；"
          f"输出目录：{out_root.resolve()}；耗时 {total}s")
    if failed_themes:
        print("失败主题：")
        for en, m in failed_themes:
            print(f"  - {en}: {m}")


if __name__ == "__main__":
    main()
