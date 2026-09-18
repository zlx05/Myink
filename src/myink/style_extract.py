"""文风样本提取（§7.12 文风档案闭环：统计层 + LLM 提炼 → 草稿 → 确认落库）。

分两层：
- 统计层（analyze_sample_stats）：纯函数、确定性——句长三档分布 / 平均句长 / 对话密度 /
  段落结构 / 高频 2-gram 词串（无分词时的稳定信号，jieba 留后续升级）；
- LLM 提炼（extract_style_profile）：一次 extract 档调用（便宜快模型），提炼统计层量不到
  的语义（pov / 句式 / 词汇修辞 / 对话腔调 / 禁忌清单 / 样本摘录），失败降级回统计层
  （§6.12 不 500、不阻塞——端点只回统计草稿 + extract_error）。

合并（merge_style_draft）：统计字段 + LLM 语义字段 + source="sample" 标记，作为
StyleProfile 草稿返回前端；确认（PUT）走 routes_style 落 project_settings.style_profile。
"""

from __future__ import annotations

import re
from collections import Counter

# 中文句末标点（……归一后）+ 紧随的闭引号。
# 口径：分号；不在边界集（中文分号是句内并列、非断句）；省略号仍视边界（欲言又止的短句
# 也计短句）——启发式简化，确定性统计（句长分布为确定性启发式分句产出）。
_SENTENCE_END = re.compile(r"[。！？…!?]+[”’」』»\"']*")
# 对话引号对（成对出现才判对话行）
_QUOTE_PAIRS = (("「", "」"), ("『", "』"), ("“", "”"), ('"', '"'))
# 句长三档阈值（字）：短 / 长
_SHORT = 15
_LONG = 40
_TOP_GRAMS = 8
# 高频词过滤：虚词/代词/量词单字（2-gram 命中任一字即跳过，滤掉"的了/我在"类无意义串）
_STOP_CHARS = "的了是在我有不被人这一也他就都个你我们它们来里着过说看为以和与或但并还又很更最非没呢吗吧啊呀哦嗯其此那这而于向对从把被"

# LLM 提炼键（merge 只收这些，不覆盖统计层产出；validate 按类型通用归一）
_LLM_TEXT_KEYS = ("pov", "sentence_style", "lexicon_tendency", "dialogue")
_LLM_LIST_KEYS = ("forbidden", "reference_excerpts")


def _split_sentences(text: str) -> list[str]:
    """中文分句：按句末标点切分，连续省略号归一，去空段。返回纯句文本（不含末尾标点）。"""
    normalized = re.sub(r"…{2,}", "…", text)
    return [p.strip() for p in _SENTENCE_END.split(normalized) if p.strip()]


def _is_dialogue(para: str) -> bool:
    """对话行判定：含一对匹配引号（「」/『』/“”/"）。"""
    return any(open_q in para and close_q in para for open_q, close_q in _QUOTE_PAIRS)


def _frequent_words(text: str) -> list[str]:
    """高频 2-gram 字串（去非汉字后计数，跳过含虚词单字的串）——无分词时的稳定词频信号。"""
    cleaned = re.sub(r"[^一-鿿]", "", text)
    if len(cleaned) < 2:
        return []
    counts: Counter[str] = Counter()
    for i in range(len(cleaned) - 1):
        gram = cleaned[i:i + 2]
        if any(ch in _STOP_CHARS for ch in gram):
            continue
        counts[gram] += 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:_TOP_GRAMS]
    return [g for g, _ in top]


def analyze_sample_stats(texts: list[str]) -> dict:
    """统计层（§7.12）：句长三档分布 + 平均句长 / 对话密度 / 段落结构 / 高频词串。

    纯函数、确定性——同一批样本永远产出同一组数字（写章节奏参考与测试锚定都靠它）。
    """
    full = "\n".join(texts)
    lens = [len(s) for s in _split_sentences(full)]
    n = len(lens)
    if n:
        dist = {
            "short": round(sum(1 for L in lens if L < _SHORT) / n, 3),
            "mid": round(sum(1 for L in lens if _SHORT <= L < _LONG) / n, 3),
            "long": round(sum(1 for L in lens if L >= _LONG) / n, 3),
            "avg": round(sum(lens) / n, 1),
        }
    else:
        dist = {"short": 0.0, "mid": 0.0, "long": 0.0, "avg": 0.0}
    paras = [p.strip() for p in full.split("\n") if p.strip()]
    para_stats = {
        "count": len(paras),
        "avg_len": round(sum(len(p) for p in paras) / len(paras), 1) if paras else 0.0,
    }
    return {
        "sentence_len_dist": dist,
        "dialogue_ratio": round(sum(1 for p in paras if _is_dialogue(p)) / len(paras), 3) if paras else 0.0,
        "para_stats": para_stats,
        "frequent_words": _frequent_words(full),
    }


def extract_style_profile(samples: list[str], stats: dict, *,
                          project_id: str | None = None, db=None) -> tuple[dict, str | None]:
    """LLM 提炼文风语义：一次 extract 档调用（便宜快模型）+ 鲁棒 JSON 解析。

    返回 (llm_profile, error)：LLM 失败 / 解析失败 → ({}, error)（§6.12 降级，端点回统计草稿）。
    函数内懒导入 providers/workflow（api 层 import 本模块时避免 import 环）。

    db 非 None 时记 agent_runs（§6.8 成本透明，对齐 global_audit._run_kind_llm）：generate 后
    立即 record_run（含降级行 error=resp.error），提交由调用方负责；此时 project_id 必填。
    """
    from myink.providers import make_chain
    from myink.workflow import nodes, prompts

    messages = prompts.style_extract_messages(samples, stats)
    resp = make_chain("extract", db=db, project_id=project_id).generate(messages, json_mode=True,
                                                                        max_tokens=nodes._MAX_TOKENS["extract"])
    if db is not None:
        if project_id is None:
            raise ValueError("db 非 None 时必须提供 project_id（agent_runs 归属）")
        nodes.record_run(db, project_id=project_id, task_id=None, node="style_extract",
                         role="StyleExtract", resp=resp, error=resp.error,
                         detail={"n_samples": len(samples)})
    if resp.error:
        return {}, resp.error
    try:
        data = nodes._parse_json(resp.content)
    except Exception as exc:  # noqa: BLE001 —— 解析失败同 LLM 失败处理（§6.12）
        return {}, f"parse_error: {exc}"
    if not isinstance(data, dict):
        return {}, "unexpected_json"
    return data, None


def merge_style_draft(stats: dict, llm_profile: dict, *, extract_error: str | None = None) -> dict:
    """合并统计层 + LLM 语义 → StyleProfile 草稿（source="sample" 标记；LLM 缺失只回统计层）。

    LLM 键只收规划固定的语义键（pov/句式/词汇/对话/禁忌/摘录），不覆盖统计层产出
    （frequent_words/节奏字段以统计为准——生成侧指导，不进 L1 阈值，§7.12 决策 5）。
    """
    draft: dict = dict(stats)
    draft["source"] = "sample"
    for key in _LLM_TEXT_KEYS:
        val = llm_profile.get(key)
        if isinstance(val, str) and val.strip():
            draft[key] = val.strip()
    for key in _LLM_LIST_KEYS:
        val = llm_profile.get(key)
        if isinstance(val, list):
            cleaned = [str(i).strip() for i in val if str(i).strip()]
            if cleaned:
                draft[key] = cleaned
    if extract_error:
        draft["extract_error"] = extract_error
    return draft


def validate_profile(profile) -> dict:
    """PUT 落库前校验 + 类型轻归一（非 dict → ValueError 由端点转 400；用户 canon 不重写内容）。

    仅做类型归一（str/list/dict/标量透传，**顶层** None 丢弃；嵌套结构原样透传——用户 canon
    不递归改写），内容一字不动——用户是唯一 canon（§7.11）。
    """
    if not isinstance(profile, dict):
        raise ValueError("文风档案必须是 JSON 对象")
    cleaned: dict = {}
    for key, val in profile.items():
        if val is None:
            continue
        if isinstance(val, (str, int, float, bool)):
            cleaned[key] = val
        elif isinstance(val, (list, tuple)):
            cleaned[key] = [str(i) for i in val if i is not None]
        elif isinstance(val, dict):
            cleaned[key] = val
    return cleaned
