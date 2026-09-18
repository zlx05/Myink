"""整书大纲形状：全书 Objective → 卷 → 约 30 章一段的阶段细纲。

历史数据：扁平 {arc, chapters}、以及「卷下逐章」三层，读取时归一为卷 + stages。
"""

from __future__ import annotations

CHAPTER_COUNT_MIN = 50
CHAPTER_COUNT_MAX = 1000
CHAPTER_COUNT_DEFAULT = 200
STAGE_SPAN = 30


def _as_int(value, default: int = 0) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _clean_str_list(items) -> list[str]:
    if not isinstance(items, list):
        return []
    return [str(x).strip() for x in items if str(x).strip()]


def _normalize_stage(raw: dict, index: int) -> dict:
    start = _as_int(raw.get("chapter_start"))
    end = _as_int(raw.get("chapter_end"))
    if start and end and start > end:
        start, end = end, start
    name = str(raw.get("name") or "").strip() or f"第 {index + 1} 段"
    return {
        "stage_seq": index + 1,
        "name": name,
        "chapter_start": start,
        "chapter_end": end,
        "goal": str(raw.get("goal") or "").strip(),
        "beats": _clean_str_list(raw.get("beats")),
    }


def _stages_from_chapter_list(chapters: list) -> list[dict]:
    valid = [c for c in chapters if isinstance(c, dict)]
    if not valid:
        return []
    chunks: list[list[dict]] = []
    buf: list[dict] = []
    for c in valid:
        buf.append(c)
        if len(buf) >= STAGE_SPAN:
            chunks.append(buf)
            buf = []
    if buf:
        chunks.append(buf)
    stages = []
    labels = ("前期", "中期", "后期")
    for i, chunk in enumerate(chunks):
        seqs = [_as_int(c.get("seq")) for c in chunk if _as_int(c.get("seq"))]
        start = seqs[0] if seqs else 0
        end = seqs[-1] if seqs else 0
        goals = [str(c.get("goal") or "").strip() for c in chunk if str(c.get("goal") or "").strip()]
        beats: list[str] = []
        for c in chunk:
            beats.extend(_clean_str_list(c.get("beats")))
        name = labels[i] if i < len(labels) and len(chunks) <= 3 else f"第 {i + 1} 段"
        stages.append({
            "stage_seq": i + 1,
            "name": name,
            "chapter_start": start,
            "chapter_end": end,
            "goal": goals[0] if goals else "",
            "beats": beats[:8],
        })
    return stages


def split_stages(start: int, end: int) -> list[dict]:
    """把卷的章区间切成约 STAGE_SPAN 章一段（前/中/后期或第 N 段）。"""
    if start <= 0 or end <= 0 or start > end:
        return []
    length = end - start + 1
    n = max(1, (length + STAGE_SPAN - 1) // STAGE_SPAN)
    labels = ("前期", "中期", "后期")
    stages = []
    cursor = start
    for i in range(n):
        remain = n - i
        span = (end - cursor + 1 + remain - 1) // remain
        lo, hi = cursor, min(end, cursor + span - 1)
        name = labels[i] if n <= 3 and i < 3 else f"第 {i + 1} 段"
        stages.append({
            "stage_seq": i + 1,
            "name": name,
            "chapter_start": lo,
            "chapter_end": hi,
            "goal": "",
            "beats": [],
        })
        cursor = hi + 1
    return stages


def _fill_volume_ranges(volumes: list[dict], chapter_count: int) -> list[dict]:
    """缺 chapter_start/end 时按卷数均分 1..chapter_count。"""
    if not volumes:
        return volumes
    missing = any(not v.get("chapter_start") or not v.get("chapter_end") for v in volumes)
    total = chapter_count if chapter_count > 0 else 0
    if missing and total > 0:
        n = len(volumes)
        cursor = 1
        filled = []
        for i, v in enumerate(volumes):
            remain = n - i
            span = (total - cursor + 1 + remain - 1) // remain
            lo, hi = cursor, min(total, cursor + span - 1)
            nv = dict(v)
            nv["chapter_start"] = lo
            nv["chapter_end"] = hi
            filled.append(nv)
            cursor = hi + 1
        volumes = filled
    out = []
    for v in volumes:
        nv = dict(v)
        start, end = _as_int(nv.get("chapter_start")), _as_int(nv.get("chapter_end"))
        stages = list(nv.get("stages") or [])
        if start and end and not stages:
            stages = split_stages(start, end)
        elif start and end:
            patched = []
            for i, s in enumerate(stages):
                st = dict(s)
                if not st.get("chapter_start") or not st.get("chapter_end"):
                    auto = split_stages(start, end)
                    if i < len(auto):
                        st["chapter_start"] = auto[i]["chapter_start"]
                        st["chapter_end"] = auto[i]["chapter_end"]
                patched.append(_normalize_stage(st, i))
            stages = patched
        else:
            stages = [_normalize_stage(s, i) for i, s in enumerate(stages)]
        nv["stages"] = stages
        if (not nv.get("chapter_start") or not nv.get("chapter_end")) and stages:
            lo = min((_as_int(s.get("chapter_start")) for s in stages), default=0)
            hi = max((_as_int(s.get("chapter_end")) for s in stages), default=0)
            if lo and hi:
                nv["chapter_start"] = lo
                nv["chapter_end"] = hi
        out.append(nv)
    return out


def _normalize_volume(raw: dict, index: int) -> dict | None:
    stages_raw = raw.get("stages")
    chapters = raw.get("chapters")
    start = _as_int(raw.get("chapter_start"))
    end = _as_int(raw.get("chapter_end"))
    if isinstance(stages_raw, list) and stages_raw:
        stages = [_normalize_stage(s, i) for i, s in enumerate(stages_raw) if isinstance(s, dict)]
    elif isinstance(chapters, list) and chapters:
        stages = _stages_from_chapter_list(chapters)
        seqs = [_as_int(c.get("seq")) for c in chapters if isinstance(c, dict) and _as_int(c.get("seq"))]
        if seqs:
            start = start or seqs[0]
            end = end or seqs[-1]
    else:
        stages = []
    if start and end and start > end:
        start, end = end, start
    if not start and stages:
        start = min((_as_int(s.get("chapter_start")) for s in stages), default=0)
    if not end and stages:
        end = max((_as_int(s.get("chapter_end")) for s in stages), default=0)
    raw_title = str(raw.get("title") or "").strip()
    goal = str(raw.get("goal") or "").strip()
    if not raw_title and not goal and not stages and not (start and end):
        return None
    title = raw_title or f"第 {index + 1} 卷"
    return {
        "volume_seq": index + 1,
        "title": title,
        "theme": str(raw.get("theme") or "").strip(),
        "goal": goal,
        "key_results": _clean_str_list(raw.get("key_results")),
        "end_event": str(raw.get("end_event") or "").strip(),
        "chapter_start": start,
        "chapter_end": end,
        "stages": stages,
    }


def normalize_outline(outline: dict | None) -> dict | None:
    """整书大纲归一为 Objective → 卷 → 阶段。旧扁平/旧逐章形状并入 stages。"""
    if not isinstance(outline, dict):
        return None
    volumes = outline.get("volumes")
    if isinstance(volumes, list):
        normed = []
        for i, v in enumerate(volumes):
            if not isinstance(v, dict):
                continue
            nv = _normalize_volume(v, len(normed))
            if nv:
                normed.append(nv)
        chapter_count = _as_int(outline.get("chapter_count"))
        if not chapter_count and normed:
            chapter_count = max((_as_int(v.get("chapter_end")) for v in normed), default=0)
        normed = _fill_volume_ranges(normed, chapter_count)
        keep = {k: v for k, v in outline.items() if k != "volumes"}
        keep["volumes"] = normed
        if chapter_count:
            keep["chapter_count"] = chapter_count
        return keep
    chapters = outline.get("chapters")
    if not isinstance(chapters, list):
        return outline
    wrapped = {
        **{k: v for k, v in outline.items() if k not in ("arc", "chapters")},
        "objective": str(outline.get("objective") or ""),
        "volumes": [
            {
                "volume_seq": 1,
                "title": "全书主线",
                "theme": "",
                "goal": "",
                "key_results": [],
                "end_event": "",
                "chapters": [c for c in chapters if isinstance(c, dict)],
            }
        ],
    }
    return normalize_outline(wrapped)


def covering_item(items: list[dict], seq: int) -> dict | None:
    """章号落在 chapter_start..end 的项；落在全部之前取第一项，之后取最后一项。"""
    valid = [x for x in items if isinstance(x, dict)]
    if not valid:
        return None
    for item in valid:
        lo, hi = _as_int(item.get("chapter_start")), _as_int(item.get("chapter_end"))
        if lo and hi and lo <= seq <= hi:
            return item
    first_lo = min((_as_int(x.get("chapter_start")) or 10**9 for x in valid))
    if first_lo < 10**9 and seq < first_lo:
        return valid[0]
    return valid[-1]


def build_persisted_outline(*, objective: str, volumes: list, premise: str,
                            chapter_count: int, storyline: str) -> dict:
    """确认落库前清洗：重编号、空卷丢弃、缺区间均分、长卷自动切阶段。"""
    raw = {
        "objective": str(objective or "").strip(),
        "volumes": [v for v in (volumes or []) if isinstance(v, dict)],
        "premise": str(premise or "").strip(),
        "chapter_count": _as_int(chapter_count),
        "storyline": str(storyline or "").strip(),
    }
    out = normalize_outline(raw) or raw
    cleaned = []
    for i, v in enumerate(out.get("volumes") or []):
        if not isinstance(v, dict):
            continue
        if not (v.get("title") or v.get("goal") or v.get("stages")
                or (v.get("chapter_start") and v.get("chapter_end"))):
            continue
        nv = dict(v)
        nv["volume_seq"] = len(cleaned) + 1
        cleaned.append(nv)
    out["volumes"] = _fill_volume_ranges(cleaned, _as_int(out.get("chapter_count")))
    if not out.get("chapter_count") and out["volumes"]:
        out["chapter_count"] = max((_as_int(v.get("chapter_end")) for v in out["volumes"]), default=0)
    return out
