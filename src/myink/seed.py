"""Demo 种子数据：《九州问天》世界观基线（conflict-samples.md 附录 A）。

init 命令：建库建表 + RLS 启用 + 写入 demo user/project/人物/硬约束/剧情线。
幂等：重复执行不重复写。
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from myink.db import get_engine, tenant_session
from myink.models import Character, Fact, PlotThread, Project, ProjectSettings, User

DEMO_USERNAME = "demo"
DEMO_PROJECT_TITLE = "九州问天"
DEMO_TARGET_WORDS = 3000  # §6.9 单遍生成整章目标字数

REALM_ORDER = ["炼气", "筑基", "金丹", "元婴", "化神", "大乘", "渡劫"]

# 文风档案（§7.12 / §8.6）：进写作 Prompt 的生成约束，禁止 AI 味句式
DEMO_STYLE_PROFILE = {
    "pov": "第三人称限知视角，以林砚为主，外部环境描写克制",
    "sentence_style": "长短句结合但自然，禁止机械交替排比；对话口语化、有真实语感；段落不宜整段叙述堆叠",
    "forbidden": [
        "禁止'不是…而是…'式转折句堆砌",
        "禁止段落结尾总结式收束（每段末句点题升华）",
        "禁止连续排比/对仗句式机械重复",
        "禁止高频空泛连接词滥用（'仿佛'、'顿时'、'竟然'、'只见'）",
        "禁止'只见一道流光'、'他的眼神闪过一丝'类模板化动作描写",
        "禁止解释性旁白替读者下结论（不说'他知道事情不简单了'）",
    ],
    # 高频句式/用词（§8.6 AI 味治理检测侧，L1 每章频次统计超阈值提示）
    "fatigue_words": ["仿佛", "不禁", "竟然", "宛如", "冷笑", "蝼蚁", "倒吸凉气",
                      "瞳孔骤缩", "天道", "大道", "因果", "气运"],
    "fatigue_patterns": ["不是.{0,20}而是", "仿佛.{0,6}(一般|一样)"],
    "dialogue": "不同角色腔调需区分：林砚克制隐忍、秦虎直率莽撞、黑脸修士阴冷，同一角色对话风格稳定",
}

# 硬约束（is_hard，恒在 Top-K，§7.2）
_HARD_FACTS = [
    ("境界体系：炼气→筑基→金丹→元婴→化神→大乘→渡劫，不可越级晋升，需逐境淬炼", "规则"),
    ("地域禁制：全九州设禁制结界，不可瞬移，跨域需传送阵", "规则"),
    ("天衡宗与叛出弟子为敌对关系", "归属"),
]


def _ensure_demo_project() -> str:
    """建 demo 用户 + 作品，返回 project_id（先 commit 根表，避免嵌套事务外键不可见）。"""
    with Session(get_engine()) as db:
        user = db.query(User).filter(User.username == DEMO_USERNAME).first()
        if user is None:
            user = User(username=DEMO_USERNAME, email="demo@myink.local")
            db.add(user)
            db.flush()
        project = db.query(Project).filter(Project.title == DEMO_PROJECT_TITLE, Project.user_id == user.id).first()
        if project is None:
            project = Project(user_id=user.id, title=DEMO_PROJECT_TITLE, genre="仙侠玄幻",
                              target_words=DEMO_TARGET_WORDS)
            db.add(project)
            db.flush()
        pid = str(project.id)
        db.commit()  # 根表先落库，后续租户事务才能引用外键
        return pid


def create_demo_project() -> str:
    """幂等建 demo 数据，返回 project_id。"""
    pid = _ensure_demo_project()
    with tenant_session(pid) as tdb:
        if tdb.query(ProjectSettings).filter_by(project_id=pid).first() is None:
            tdb.add(ProjectSettings(
                project_id=pid,
                world_rules={"realm_order": REALM_ORDER},
                hard_constraints=[f[0] for f in _HARD_FACTS],
                style_profile=DEMO_STYLE_PROFILE,
            ))
            for content, category in _HARD_FACTS:
                tdb.add(Fact(project_id=pid, content=content, category=category, is_hard=True,
                             source_chapter=1, confidence=1.0, confirm_status="confirmed"))
        _ensure_character(tdb, pid, "林砚", realm_cap="金丹",
                          personality="谨慎隐忍、谋定后动", origin="叛出天衡宗")
        _ensure_character(tdb, pid, "黑脸修士", realm_cap="元婴", origin="天衡宗执法堂")
        _ensure_character(tdb, pid, "秦虎", realm_cap="筑基", origin="北境城守将")
        if tdb.query(PlotThread).filter_by(project_id=pid).first() is None:
            tdb.add(PlotThread(project_id=pid, name="叛出天衡宗，追寻真相", kind="main",
                               status="active", priority=1))
            tdb.add(PlotThread(project_id=pid, name="玉佩真相", kind="side",
                               status="active", priority=2))
    return pid


def _ensure_character(db: Session, project_id: str, name: str, **kw) -> None:
    if db.query(Character).filter_by(project_id=project_id, name=name).first() is None:
        db.add(Character(project_id=project_id, name=name, **kw))


# ---- 多本书展示数据源（2026-08-09）：阶段 2 展示前端侧边栏多书切换 ----
# 与《九州问天》题材错开的示例书，幂等补建（重复 init 不重复写）。
_SAMPLE_BOOKS = [
    {
        "title": "长安夜行",
        "genre": "历史悬疑",
        "target_words": 3000,
        "world_rules": {"era": "盛唐长安", "power_system": "无超凡力量，靠智谋与武艺",
                        "restrictions": "人物行动须符合唐代官制与地理，不得越界"},
        "style_profile": {"pov": "第三人称限知视角，以沈青梧为主", "sentence_style": "克制冷峻，对话简练",
                          "forbidden": ["禁止引入仙佛鬼神", "禁止现代词汇（'摄像头''监控'等）"],
                          # 高频句式/用词（§8.6 AI 味治理检测侧）：恐怖题材克制叙事、事实传达
                          "fatigue_words": ["仿佛", "不禁", "竟然", "宛如", "毛骨悚然", "不寒而栗",
                                            "头皮发麻", "震惊", "不可思议", "深吸一口气"],
                          "fatigue_patterns": ["不是.{0,20}而是", "眼神.{0,6}闪过一丝"],
                          "dialogue": "沈青梧重证据轻直觉、言语冷硬；裴元庆圆滑世故、话里藏话，腔调需区分"},
        "hard_constraints": ["主线为查案，不得引入仙佛鬼神", "主角是大理寺不良人，不得越权执政"],
        "characters": [
            {"name": "沈青梧", "realm_cap": "武艺·一流", "origin": "大理寺不良人",
             "personality": "敏锐孤僻、重证据轻直觉"},
            {"name": "裴元庆", "realm_cap": "武艺·三流", "origin": "京兆府推官",
             "personality": "圆滑世故、暗藏底线"},
        ],
        "threads": [("朱雀大街命案，追查玉匣背后的朝堂暗流", "main", 1)],
    },
    {
        "title": "星舰远征",
        "genre": "科幻",
        "target_words": 3000,
        "world_rules": {"tech": "超光速曲率航行，战舰有护盾与相位炮", "power": "无个人超凡，靠舰船与战术",
                        "restrictions": "物理设定遵循经典科幻（超光速例外），不得出现玄幻力量"},
        "style_profile": {"pov": "第三人称，以指挥视角为主", "sentence_style": "冷静技术感，术语精准",
                          "forbidden": ["禁止魔法/修仙元素", "禁止忽略硬性物理后果"],
                          # 高频句式/用词（§8.6 AI 味治理检测侧）：禁 info-dump、技术经角色交互呈现
                          "fatigue_words": ["仿佛", "不禁", "竟然", "宛如", "震惊", "不可思议",
                                            "难以置信", "深吸一口气"],
                          "fatigue_patterns": ["不是.{0,20}而是", "系统.{0,10}提示"],
                          "dialogue": "苏晚晴冷静果决、指令简短；凌风话少技高、专业术语精准，腔调需区分"},
        "hard_constraints": ["不得出现魔法/修仙元素", "战斗须基于舰船武器系统，不依赖个人武力"],
        "characters": [
            {"name": "苏晚晴", "realm_cap": "指挥·上将", "origin": "远征军旗舰舰长",
             "personality": "冷静果决、护短"},
            {"name": "凌风", "realm_cap": "驾驶·王牌", "origin": "领航员",
             "personality": "话少技高、信任队友"},
        ],
        "threads": [("殖民舰队遭遇未知星域异常，追查信标信号", "main", 1)],
    },
    {
        "title": "都市医馆",
        "genre": "都市",
        "target_words": 3000,
        "world_rules": {"era": "现代都市", "power_system": "无超凡力量，靠医术与商业经营",
                        "restrictions": "医疗操作须有基本医学可信度，商业逻辑须自洽"},
        "style_profile": {"pov": "第三人称限知，以叶青为主",
                          "sentence_style": "内心独白口语化、直觉化，禁止商业分析术语渗入叙事；对话简洁真实",
                          "forbidden": ["禁止无逻辑的商业奇迹（无铺垫的暴富）", "禁止反派降智配合主角",
                                         "禁止'一个电话搞定'跳过具体操作过程", "禁止女性角色沦为花瓶"],
                          # 高频句式/用词（§8.6 AI 味治理检测侧）：都市 AI 味最典型（商战/人际）
                          "fatigue_words": ["仿佛", "不禁", "竟然", "宛如", "冷笑", "震惊", "不可思议",
                                            "难以置信", "深吸一口气", "眼中闪过一丝"],
                          "fatigue_patterns": ["不是.{0,20}而是", "深吸一口气.{0,6}道"],
                          "dialogue": "叶青谦和但针锋相对时犀利；对手市侩精明，腔调需区分"},
        "hard_constraints": ["主线围绕医馆经营与医术成长，不得引入仙佛鬼神",
                            "商业/医疗操作须符合现实逻辑"],
        "characters": [
            {"name": "叶青", "realm_cap": "医术·顶尖", "origin": "城中老字号医馆继承人",
             "personality": "外柔内刚、医术求实"},
            {"name": "沈越", "realm_cap": "商道·新贵", "origin": "同行医馆少东",
             "personality": "精明算计、表面客气"},
        ],
        "threads": [("老字号医馆在资本冲击下守业并重振祖业", "main", 1)],
    },
]


# 题材 Skill 预设注册表（§7.12 预设包）：4 本种子书 = 4 个预设（含《九州问天》）。
# 引用各 book 的 style_profile（DRY，改书档案即改预设）；设置页「预设导入」据此渲染 +
# 原子写 project_settings.style_profile + skill_pack（marker）。
STYLE_PRESETS = [
    {
        "id": "xianxia-jiuzhou",
        "name": "九州问天",
        "genre": "仙侠玄幻",
        "style_profile": DEMO_STYLE_PROFILE,
    },
    {
        "id": "changan-yexing",
        "name": "长安夜行",
        "genre": "历史悬疑",
        "style_profile": _SAMPLE_BOOKS[0]["style_profile"],
    },
    {
        "id": "xingjian-yuanzheng",
        "name": "星舰远征",
        "genre": "科幻",
        "style_profile": _SAMPLE_BOOKS[1]["style_profile"],
    },
    {
        "id": "dushi-yiguan",
        "name": "都市医馆",
        "genre": "都市",
        "style_profile": _SAMPLE_BOOKS[2]["style_profile"],
    },
]


def _ensure_sample_book(user_id: uuid.UUID, spec: dict) -> str | None:
    """幂等建单本示例书（根表先 commit，再租户会话写设定/人物/剧情线），返回新 pid。"""
    with Session(get_engine()) as db:
        if db.query(Project).filter(Project.title == spec["title"], Project.user_id == user_id).first():
            return None
        p = Project(user_id=user_id, title=spec["title"], genre=spec["genre"],
                    target_words=spec.get("target_words", DEMO_TARGET_WORDS))
        db.add(p)
        db.flush()
        pid = str(p.id)
        db.commit()
    with tenant_session(pid) as tdb:
        tdb.add(ProjectSettings(
            project_id=pid,
            world_rules=spec["world_rules"],
            style_profile=spec["style_profile"],
            hard_constraints=spec["hard_constraints"],
        ))
        for ch in spec["characters"]:
            _ensure_character(tdb, pid, **ch)
        for name, kind, priority in spec["threads"]:
            tdb.add(PlotThread(project_id=pid, name=name, kind=kind, status="active", priority=priority))
    return pid


def create_sample_books() -> list[str]:
    """幂等补建示例书（多本书展示数据源），返回本次新建的 pid 列表。"""
    created: list[str] = []
    with Session(get_engine()) as db:
        user = db.query(User).filter(User.username == DEMO_USERNAME).first()
        if user is None:
            return created  # demo 用户未建（init 顺序保证先建 demo），跳过
    for spec in _SAMPLE_BOOKS:
        pid = _ensure_sample_book(user.id, spec)
        if pid:
            created.append(pid)
    return created
