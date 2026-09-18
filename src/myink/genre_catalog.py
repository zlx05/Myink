"""题材包根目录（建书时深拷贝到本书，根包只读）。

字段与 webnovel-writer 的题材分类对齐，正文按我们自己的 schema 重写，不搬对方 Markdown。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

UNSELECTED_NAME = "未选题材"

# 推进快 → 每卷更短 → 同样章数下卷更多。未选题材走默认。
VOLUME_SPAN_FAST = 24
VOLUME_SPAN_DEFAULT = 40
VOLUME_SPAN_SLOW = 60
_FAST_PACKS = frozenset({
    "xitong", "wuxian", "tianchong", "dianjing", "zhibo", "youxi-tiyu",
    "gouxie", "tishen", "guize", "dushi-naodong", "xianyan-naodong",
    "xuanyi-naodong", "zhihu",
})
_SLOW_PACKS = frozenset({
    "xiuxian", "lishi-gudai", "lishi-naodong", "kangzhan", "heian",
    "kesulu", "minguo", "xihuan",
})

FIELD_KEYS = (
    "selling_point", "subgenres", "taboos", "pacing", "satisfaction", "mechanics", "world_hints",
)

GROUPS: list[tuple[str, list[str]]] = [
    ("玄幻修仙", ["xiuxian", "xitong", "gaowu", "xihuan", "wuxian", "moshi", "kehuan"]),
    ("都市现代", ["dushi-yineng", "dushi-richang", "dushi-naodong", "xianshi",
                 "dianjing", "zhibo", "youxi-tiyu"]),
    ("言情", ["guyuan", "gongdou", "tianchong", "haomen", "zhichang", "minguo",
             "huanxiang-yanqing", "xianyan-naodong", "nvpin-xuanyi", "zhongtian",
             "niandai", "gouxie", "tishen", "duozifu"]),
    ("其他", ["guize", "xuanyi-naodong", "xuanyi-lingyi", "kesulu", "zhihu",
             "kangzhan", "lishi-gudai", "lishi-naodong", "heian"]),
]


def _sg(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"name": n, "hook": h} for n, h in pairs]


def _p(*, selling_point: str, subgenres: list[dict[str, str]], taboos: list[str],
       pacing: str, satisfaction: list[str], mechanics: list[str],
       world_hints: list[str]) -> dict[str, Any]:
    return {
        "selling_point": selling_point,
        "subgenres": subgenres,
        "taboos": taboos,
        "pacing": pacing,
        "satisfaction": satisfaction,
        "mechanics": mechanics,
        "world_hints": world_hints,
    }


PACKS: dict[str, dict[str, Any]] = {
    "xiuxian": {
        "name": "修仙", "group": "玄幻修仙",
        **_p(
            selling_point="与天争命、与人争利：资源稀缺，突破必须付出可见代价。",
            subgenres=_sg(("凡人流", "算计谨慎、积少成多"), ("无敌流", "开局强势、专治不服"),
                          ("家族流", "带着一群人滚资源"), ("苟道流", "避因果，出手即有准备")),
            taboos=["无铺垫越级突破", "法宝凭空解决危机", "用大道感应跳过修炼过程",
                    "反派排队送死", "主角无故心软误事"],
            pacing="修炼/悟道与冲突交替，每 3–5 章有一次小突破或关键收获。",
            satisfaction=["悟道突破", "斗法碾压", "法宝到手", "身份揭开", "因果了结"],
            mechanics=["大境界压制不可轻易翻盘", "金手指须有频率/范围/代价",
                       "同质资源重复获取要写衰减", "突破绑定剧情节点"],
            world_hints=["灵气矿脉稀缺，野外先防人", "散修/宗门/世家阶级分明",
                         "机缘走传闻→探索→争夺→兑现"],
        ),
    },
    "xitong": {
        "name": "系统流", "group": "玄幻修仙",
        **_p(
            selling_point="任务驱动、回报可见：系统是外挂也是枷锁。",
            subgenres=_sg(("签到流", "地点/时间打卡换奖励"), ("抽奖流", "随机翻盘须付代价"),
                          ("兑换流", "积分换能力，要防打工流水账")),
            taboos=["面板每章完整刷屏", "无用属性从不兑现", "任务奖励与剧情无关",
                    "系统突然改规则不解释"],
            pacing="每 2–3 章完成或推进一个可见任务，奖励立刻落到实力或处境。",
            satisfaction=["任务兑现", "面板数字上涨", "绝境翻盘", "系统反噬/博弈"],
            mechanics=["只列核心战斗或剧情属性", "失败惩罚要疼但不轻易抹杀",
                       "后期用压缩/回收压数值膨胀", "系统来源中后期必须留钩子"],
            world_hints=["系统初段像工具，中段像伙伴，后期像对手",
                         "旁人看不到面板，暴露有代价"],
        ),
    },
    "gaowu": {
        "name": "高武", "group": "玄幻修仙",
        **_p(
            selling_point="拳脚体感与招式克制，强者靠打出来而不是靠宣布。",
            subgenres=_sg(("宗门比武", "名次换资源"), ("江湖行走", "一地一敌"),
                          ("武道争霸", "门派兴衰绑主角")),
            taboos=["口头宣布战力翻倍无对打", "越级秒杀不写代价", "招式名字堆砌无体感"],
            pacing="小冲突当章见输赢，大比/大战前铺 1–2 章准备。",
            satisfaction=["以弱胜强", "招式反制", "名望提升", "收徒/收地"],
            mechanics=["伤势影响下一场", "内力/气血有消耗", "兵器与身法有克制"],
            world_hints=["武林规矩能破但要付社会代价", "高手过招先试探再摊牌"],
        ),
    },
    "xihuan": {
        "name": "西幻", "group": "玄幻修仙",
        **_p(
            selling_point="职业、魔法与种族规则清楚，冒险按委托和地图推进。",
            subgenres=_sg(("骑士冒险", "委托与荣誉"), ("法师探索", "知识换力量"),
                          ("种族冲突", "阵营利益")),
            taboos=["魔法没有消耗和反噬", "种族刻板到只剩标签", "地下城奖励无风险"],
            pacing="一委托一闭环，每 3–4 章换地点或提高风险等级。",
            satisfaction=["职业进阶", "稀有装备", "解开遗迹", "阵营站队"],
            mechanics=["法术位/魔力有限", "职业克制写进战斗", "公会/教会有立场"],
            world_hints=["货币、爵位、教会法令要落地", "地图危险等级读者能感觉到"],
        ),
    },
    "wuxian": {
        "name": "无限流", "group": "玄幻修仙",
        **_p(
            selling_point="副本规则即生死：进来要搞懂规则，带东西出去。",
            subgenres=_sg(("副本求生", "限时通关"), ("轮回小队", "队友博弈"),
                          ("主神空间", "积分换能力")),
            taboos=["规则中途乱改不提示", "副本外成长跳过结算", "队友工具人无利益"],
            pacing="副本内高频兑现线索/伤亡；副本间隙用奖励和关系消化。",
            satisfaction=["吃透规则反杀", "带出关键道具", "队友站队", "积分兑换"],
            mechanics=["每本规则写死并遵守", "死亡/重伤有团队后果",
                       "奖励与本局表现挂钩"],
            world_hints=["主神/空间不解释也可以，但赏罚要稳定",
                         "现实侧债务会逼人再进副本"],
        ),
    },
    "moshi": {
        "name": "末世", "group": "玄幻修仙",
        **_p(
            selling_point="物资、据点和人性比口号重要，活着本身就是胜利。",
            subgenres=_sg(("丧尸求生", "据点与清剿"), ("天灾重建", "秩序重塑"),
                          ("进化竞争", "异能换代价")),
            taboos=["物资无限不记账", "危险区域随便逛", "人性崩坏只靠演讲解决"],
            pacing="每 2–3 章一次物资或据点层面的得失。",
            satisfaction=["守住据点", "抢到稀缺物资", "识破内鬼", "进化突破"],
            mechanics=["食物弹药要记账", "伤病拖累行动", "势力交易不讲情面"],
            world_hints=["秩序真空里规则由枪和粮决定", "希望场景要配代价"],
        ),
    },
    "kehuan": {
        "name": "科幻", "group": "玄幻修仙",
        **_p(
            selling_point="技术规则前后一致，冲突来自资源、信息和制度。",
            subgenres=_sg(("星际政治", "舰队与条约"), ("赛博都市", "公司与义体"),
                          ("硬核探索", "未知现象")),
            taboos=["黑科技无铺垫救场", "时代细节自相矛盾", "用魔法词汇解释科学"],
            pacing="信息优势每 2–3 章翻转一次，大战前把技术限制讲清。",
            satisfaction=["技术克制", "情报反转", "制度钻空", "探索揭秘"],
            mechanics=["技术有能耗/冷却/副作用", "通信延迟影响指挥",
                       "公司/帝国法令约束行动"],
            world_hints=["设定吃书比文笔问题更致命", "小人物反应用来兑现科技冲击"],
        ),
    },
    "dushi-yineng": {
        "name": "都市异能", "group": "都市现代",
        **_p(
            selling_point="现代社会里的超常能力必须藏、必须耗、必须惹到组织。",
            subgenres=_sg(("觉醒升级", "能力成长"), ("隐秘组织", "两套秩序"),
                          ("都市修真", "修炼藏在日常里")),
            taboos=["当街开大无人拍", "异能无消耗", "官方机构完全不存在"],
            pacing="日常与超常交替，每 2–3 章一次暴露风险或组织施压。",
            satisfaction=["能力新用法", "打脸装逼者", "组织招揽/对立", "身份双线"],
            mechanics=["能力有冷却或反噬", "暴露会引来扫描/猎人",
                       "现代监控是真实威胁"],
            world_hints=["两套秩序并行：表面法律与隐秘规矩",
                         "金钱地位仍能买到信息"],
        ),
    },
    "dushi-richang": {
        "name": "都市日常", "group": "都市现代",
        **_p(
            selling_point="烟火气和关系推进，小事件也要有情绪落点。",
            subgenres=_sg(("职场生活", "同事与项目"), ("邻里社区", "人情往来"),
                          ("兴趣圈子", "爱好换遇见")),
            taboos=["无冲突流水账", "人物说教人生哲理", "突然变成爽文打脸"],
            pacing="每章一个完整小事件，跨章关系缓慢加码。",
            satisfaction=["关系升温", "生活难题落地", "被理解/被看见"],
            mechanics=["对话带潜台词", "物件和季节重复出现形成记忆点"],
            world_hints=["城市细节要具体到街道和物价", "反派可以只是难搞的普通人"],
        ),
    },
    "dushi-naodong": {
        "name": "都市脑洞", "group": "都市现代",
        **_p(
            selling_point="一个非常规设定插入当代生活，规则要早立早用。",
            subgenres=_sg(("规则游戏", "现代+异规则"), ("身份错位", "双重生活"),
                          ("信息差喜剧", "知道/不知道")),
            taboos=["脑洞只在开头炫一次", "规则随剧情改", "日常完全被设定淹没"],
            pacing="设定每 3 章揭示一层用法，同时给生活线反馈。",
            satisfaction=["新用法", "信息差兑现", "日常被设定改写又改回来"],
            mechanics=["设定边界写死", "滥用有社会或身体代价"],
            world_hints=["读者要能用规则自己推下一步", "当代法律和舆论仍在"],
        ),
    },
    "xianshi": {
        "name": "现实题材", "group": "都市现代",
        **_p(
            selling_point="制度、人情和利益摩擦，不靠超能解决问题。",
            subgenres=_sg(("行业深潜", "专业细节"), ("家庭伦理", "责任纠缠"),
                          ("社会议题", "个案切入")),
            taboos=["万能好人救场", "把复杂问题一句鸡汤化", "程序错误当没事"],
            pacing="每 2–3 章推进一桩具体事务的结果。",
            satisfaction=["谈成一笔", "赢一场程序", "关系诚实一次"],
            mechanics=["专业流程要经得起常识", "胜利带副作用"],
            world_hints=["钱权通过物、势、小人物反应兑现", "主角会误判"],
        ),
    },
    "dianjing": {
        "name": "电竞", "group": "都市现代",
        **_p(
            selling_point="赛里靠操作与BP，赛外靠合同、舆论和团队。",
            subgenres=_sg(("从青训到职业", "名额争夺"), ("冠军之路", "赛季节奏"),
                          ("退役/教练", "身份转换")),
            taboos=["比赛只写喊麦不写对线", "实力一夜翻盘无训练", "俱乐部无商业压力"],
            pacing="训练→资格赛→季后赛台阶清晰，赛间写合同和舆论。",
            satisfaction=["关键局翻盘", "转会/续约", "舆论反转", "冠军"],
            mechanics=["版本强势要过时", "状态和伤病影响发挥", "BP 有信息战"],
            world_hints=["直播和黑料是第二战场", "队友是同事不是工具"],
        ),
    },
    "zhibo": {
        "name": "直播文", "group": "都市现代",
        **_p(
            selling_point="镜头内外两套表演，热度和真实关系互相撕扯。",
            subgenres=_sg(("带货翻身", "业绩换话语权"), ("游戏主播", "技术+人设"),
                          ("真人秀博弈", "剧本与真心")),
        taboos=["热度无来源暴涨", "平台规则不存在", "黑粉只为骂而骂无利益"],
            pacing="每章一次直播事件或热搜，隔章结算收益/人设损伤。",
            satisfaction=["数据爆发", "对线黑料", "被平台看见", "线下关系落地"],
            mechanics=["算法/审核会惩罚擦边和翻车", "金主和经纪公司有条件"],
            world_hints=["人设是资产也是枷锁", "镜头外的沉默比弹幕真实"],
        ),
    },
    "youxi-tiyu": {
        "name": "游戏体育", "group": "都市现代",
        **_p(
            selling_point="竞技规则清楚，输赢改变排名、合同和自尊心。",
            subgenres=_sg(("职业联赛", "赛季积分"), ("街头/草根", "一战成名"),
                          ("教练养成", "带队")),
            taboos=["规则乱改", "体能无限", "对手没有战术"],
            pacing="一场比赛一个情绪弧，赛季节点给名次反馈。",
            satisfaction=["绝杀", "战术奏效", "转会", "夺冠"],
            mechanics=["体力和技术状态有起伏", "伤病影响后续场次"],
            world_hints=["观众、赞助、青训是生态", "输了要有技术复盘"],
        ),
    },
    "guyuan": {
        "name": "古言", "group": "言情",
        **_p(
            selling_point="礼法与情欲拉扯，情感进展必须改写人物处境。",
            subgenres=_sg(("权谋联姻", "利益里长情"), ("江湖侠侣", "走江湖的情"),
                          ("重生改命", "用记忆避坑")),
            taboos=["现代口语穿帮", "礼法想起来才有", "男女主无误会硬拖"],
            pacing="每 2–3 章一次关系质变或公开风险。",
            satisfaction=["试探成功", "身份公开", "护短", "共同对敌"],
            mechanics=["身份差带来看得见的限制", "闲话和礼部/族规是压力"],
            world_hints=["称谓、避讳、居所等级要稳", "配角有自己的婚约和野心"],
        ),
    },
    "gongdou": {
        "name": "宫斗宅斗", "group": "言情",
        **_p(
            selling_point="资源、名分和信息战，一招棋影响整房人。",
            subgenres=_sg(("后宫", "宠位与子嗣"), ("宅门", "嫡庶与管家"),
                          ("宫廷职场", "外朝内廷")),
            taboos=["反派蠢到只送人头", "主角全知无误判", "毒计无代价"],
            pacing="小局当章见分晓，大局 5–8 章收一网。",
            satisfaction=["反将一军", "名分提升", "盟友倒戈", "把柄互换"],
            mechanics=["礼物、药、账本都是武器", "皇帝/家主的注意力是稀缺资源"],
            world_hints=["下人耳目比刀快", "赢一次会树更大的敌"],
        ),
    },
    "tianchong": {
        "name": "青春甜宠", "group": "言情",
        **_p(
            selling_point="甜蜜要具体，阻碍要真实，甜完有余味。",
            subgenres=_sg(("校园", "同学到恋人"), ("初入社会", "合租/同事"),
                          ("青梅竹马", "重新看见")),
            taboos=["无沟通硬虐超长", "配角纯恶毒无动机", "甜只靠喊称呼"],
            pacing="每章一个相处事件，每 4–5 章一次关系升级。",
            satisfaction=["被偏爱", "当众站队", "和解", "官宣"],
            mechanics=["误会当章或隔章揭开", "家人朋友态度要变"],
            world_hints=["场景集中（学校/公司/小区）更好追", "配角可以有自己的线"],
        ),
    },
    "haomen": {
        "name": "豪门总裁", "group": "言情",
        **_p(
            selling_point="资本和家族压力可见，宠不是免单，是愿意付账单。",
            subgenres=_sg(("联姻", "合同里的情"), ("隐婚", "身份游戏"),
                          ("复仇爱情", "利益与心")),
            taboos=["霸总无法无天零后果", "女主无技能只被养", "家族反对只靠骂两句"],
            pacing="商战/舆论与感情线交织，每 3 章一次地位或合同变化。",
            satisfaction=["护短", "股权/身份揭晓", "家族让步", "并肩赢一局"],
            mechanics=["律师、董事会、媒体是真压力", "宠要落到时间与资源"],
            world_hints=["钱的来源和脏手要有暗示", "配角也有身价"],
        ),
    },
    "zhichang": {
        "name": "职场婚恋", "group": "言情",
        **_p(
            selling_point="项目和感情抢同一块时间，晋升与关系互相改写。",
            subgenres=_sg(("上下级", "权力差"), ("同行对手", "竞品恋爱"),
                          ("创业伴侣", "股份和信任")),
            taboos=["工作全是背景板", "职场性骚扰当浪漫", "升职全靠恋爱"],
            pacing="一个项目周期对应一段关系变化。",
            satisfaction=["项目过关", "边界被尊重", "公开关系", "共同辞职/留下"],
            mechanics=["绩效、客户、加班是冲突源", "公司政策约束公开恋情"],
            world_hints=["行业细节要像那么回事", "同事八卦是压力也是喜剧"],
        ),
    },
    "minguo": {
        "name": "民国言情", "group": "言情",
        **_p(
            selling_point="乱世里的身份、枪和报纸，情要付时代税。",
            subgenres=_sg(("谍海", "身份双重"), ("商贾世家", "买办与工厂"),
                          ("梨园/报馆", "名与身")),
            taboos=["时代物件乱穿", "战争只当烟花", "方言标点当人物"],
            pacing="个人情感与时局新闻同频，每 3–4 章一次逃/留选择。",
            satisfaction=["托付后路", "假身份被拆", "救下对方", "一同离开或留下"],
            mechanics=["通讯和交通受限制", "立场站错会死人"],
            world_hints=["报纸标题推动剧情", "租界/军阀/党派别写混"],
        ),
    },
    "huanxiang-yanqing": {
        "name": "幻想言情", "group": "言情",
        **_p(
            selling_point="奇幻设定为感情服务，能力差是障碍不是碾压工具。",
            subgenres=_sg(("人神/人魔", "禁忌之恋"), ("学院魔法", "同窗"),
                          ("契约兽/精灵", "羁绊")),
            taboos=["设定只为制造强者人设", "种族仇恨一句化解", "能力解决所有误会"],
            pacing="感情节点和设定揭示绑在一起。",
            satisfaction=["打破禁忌一小步", "为对方违例", "共同面对族群"],
            mechanics=["契约/寿命/魔力有限制", "暴露身份有政治后果"],
            world_hints=["两个世界的规矩都要写清", "配角代表族群压力"],
        ),
    },
    "xianyan-naodong": {
        "name": "现言脑洞", "group": "言情",
        **_p(
            selling_point="当代恋爱叠一个非常规机制，机制要服务关系。",
            subgenres=_sg(("系统催婚", "任务即相处"), ("时间循环", "重复里谈恋爱"),
                          ("身份互换", "看见对方的一天")),
            taboos=["机制与感情两张皮", "循环无新信息", "任务羞耻玩法无同意"],
            pacing="机制每 2–3 章给一次关系新视角。",
            satisfaction=["机制被两人一起破解", "真心压过任务", "选择关掉外挂"],
            mechanics=["机制有退出条款或代价", "旁人视角用来对照"],
            world_hints=["当代社交平台仍在", "机制秘密是两人共同的债"],
        ),
    },
    "nvpin-xuanyi": {
        "name": "女频悬疑", "group": "言情",
        **_p(
            selling_point="亲密关系里藏线索，真相改写谁能被信任。",
            subgenres=_sg(("婚姻迷雾", "配偶是嫌疑人"), ("闺蜜反转", "亲近者"),
                          ("家族旧案", "血缘与秘密")),
            taboos=["女主只尖叫不推理", "凶手无动机", "恋爱线洗白一切罪行"],
            pacing="每章留一个可验证线索，每 4–5 章推翻一个信任对象。",
            satisfaction=["识破谎言", "自救成功", "真相反转", "关系重估"],
            mechanics=["物证先于第六感", "报警/媒体有现实后果"],
            world_hints=["家暴和操控要写清楚不是浪漫", "配角各有秘密但不浪费"],
        ),
    },
    "zhongtian": {
        "name": "种田", "group": "言情",
        **_p(
            selling_point="收成、铺子和人口是可见成长，日子越过越稳。",
            subgenres=_sg(("农家", "田地畜牧"), ("商贾", "铺面与渠道"),
                          ("空间/穿越", "现代知识落地要受阻")),
            taboos=["空间超市无限取货", "朝廷政策说改就改", "邻里全是恶毒脸谱"],
            pacing="一季一结算：粮、钱、人、名声。",
            satisfaction=["丰收", "开铺", "联姻互助", "灾年挺住"],
            mechanics=["气候、税、路是硬约束", "技术传播有守旧阻力"],
            world_hints=["账本比金手指可信", "亲戚是资源也是消耗"],
        ),
    },
    "niandai": {
        "name": "年代", "group": "言情",
        **_p(
            selling_point="票证、单位和舆论是墙，个人选择要撞墙。",
            subgenres=_sg(("工厂大院", "分配与面子"), ("知青", "回城与留乡"),
                          ("改革开放初", "下海")),
            taboos=["物资现代便利乱入", "政治运动当搞笑背景", "人物满口当代网络语"],
            pacing="政策/运动节点推动人物命运，个人线咬住大事件。",
            satisfaction=["分到房子/指标", "保住家人", "抓住一波机会"],
            mechanics=["票、介绍信、成分影响行动", "邻居眼睛是监控"],
            world_hints=["物质匮乏要具体", "体面和生存常冲突"],
        ),
    },
    "gouxie": {
        "name": "狗血言情", "group": "言情",
        **_p(
            selling_point="高密度误会、替身、旧爱，反转要提前埋，不能靠降智。",
            subgenres=_sg(("替身", "像谁"), ("私生子/隐婚", "身份炸弹"),
                          ("复仇", "爱恨同账")),
            taboos=["角色突然失忆赶剧情", "反派作恶无收益", "和解靠一哭"],
            pacing="每章一个情绪钩子，每 3 章一个中反转。",
            satisfaction=["当众打脸", "身份揭晓", "旧爱退场", "真心摊牌"],
            mechanics=["每个误会要有信息差来源", "配角反水要有利害"],
            world_hints=["狗血可以，逻辑链条要连得上", "苦要有人物自己的选择"],
        ),
    },
    "tishen": {
        "name": "替身文", "group": "言情",
        **_p(
            selling_point="被当成别人是伤，也是近距离看见对方的窗口。",
            subgenres=_sg(("白月光替身", "被当成另一个人"), ("职业替身", "合约"),
                          ("容貌相似", "认错")),
            taboos=["男主虐完无成长", "女主只忍不设边界", "真白月光突然圣女"],
            pacing="早期强化替代感，中期裂痕，后期必须回答「爱的是谁」。",
            satisfaction=["被叫对名字", "合约作废", "当众承认", "离开或留下的主动权"],
            mechanics=["对比场景反复出现但信息递增", "旁人起哄是压力"],
            world_hints=["替身身份要有社会代价", "原身未必要恶，但要占位置"],
        ),
    },
    "duozifu": {
        "name": "多子多福", "group": "言情",
        **_p(
            selling_point="孩子是剧情引擎：养、护、教，改变大人站队。",
            subgenres=_sg(("带球跑", "独自抚养"), ("一胎多宝", "热闹与资源"),
                          ("团宠", "长辈战线")),
            taboos=["孩子工具化只会叫爸", "忽略养育成本", "仇人针对孩子无警方/族人反应"],
            pacing="孩子成长节点（病、学、认亲）带动大人线。",
            satisfaction=["认亲", "护犊", "对前任/家族打脸", "成家"],
            mechanics=["医疗、户籍、学费是实打实障碍", "每个孩子性格分开写"],
            world_hints=["亲戚眼光是日常战场", "甜要建立在劳动上"],
        ),
    },
    "guize": {
        "name": "规则怪谈", "group": "其他",
        **_p(
            selling_point="规则即地图：遵守、钻空、付出代价，逻辑比惊吓重要。",
            subgenres=_sg(("公寓/医院", "空间规则"), ("乡野祠堂", "民俗规则"),
                          ("网络游戏", "副本规则")),
            taboos=["规则事后改口", "用勇气空喊破局", "每章只吓人无新规则"],
            pacing="每章验证或改写一条规则，过渡章不超过一章。",
            satisfaction=["钻空存活", "揭开规则作者", "带人出去", "规则反噬敌人"],
            mechanics=["规则条目读者能列表", "违反必有对应惩罚",
                       "信息来源不可全信"],
            world_hints=["日常物品会变成触发器", "幸存者口述带偏见"],
        ),
    },
    "xuanyi-naodong": {
        "name": "悬疑脑洞", "group": "其他",
        **_p(
            selling_point="非常规前提 + 可解谜面，读者应能跟推理。",
            subgenres=_sg(("概念杀人", "手法非常规"), ("叙事诡计", "不可靠叙述"),
                          ("社会实验", "规则困局")),
            taboos=["凶手靠超能力且事前无提示", "线索只在最后才出现", "脑洞压过动机"],
            pacing="线索密度高于动作，每章给可记录信息。",
            satisfaction=["假说被推翻", "动机落地", "手法揭晓"],
            mechanics=["物证时间线自洽", "多个嫌疑人都要有机会"],
            world_hints=["设定再怪也要有内在经济", "角色智商保持稳定"],
        ),
    },
    "xuanyi-lingyi": {
        "name": "悬疑灵异", "group": "其他",
        **_p(
            selling_point="超自然现象有自己的账，查案和安魂两条线。",
            subgenres=_sg(("都市怪谈", "个案"), ("民俗驱邪", "仪式"),
                          ("连环怨", "因果链")),
            taboos=["鬼无逻辑只跳脸", "科学调查完全失效却不解释", "受害者工具化"],
            pacing="现象→调查→误判→更完整的规则。",
            satisfaction=["对上真名", "仪式成功", "活人侧真相", "怨解"],
            mechanics=["接触有污染或代价", "仪式材料稀缺"],
            world_hints=["地方志和口述史是线索", "白天的社会线不能丢"],
        ),
    },
    "kesulu": {
        "name": "克苏鲁", "group": "其他",
        **_p(
            selling_point="知得越多越危险，胜利往往是推迟崩溃。",
            subgenres=_sg(("调查社团", "文献与实地"), ("海港/极地", "地理压迫"),
                          ("梦境渗透", "现实被改写")),
            taboos=["主角无代价打神", "神话生物当普通怪物刷", "疯狂只写哈哈笑"],
            pacing="信息有毒：每揭一层付心智或人际关系代价。",
            satisfaction=["活着离开", "毁掉一份文献", "保住一个普通人", "理解但选择遗忘"],
            mechanics=["心智/理智值式衰退要可见", "知识不能无成本传播"],
            world_hints=["宇宙冷漠，人类机构帮不上或更糟", "暗示优于展览"],
        ),
    },
    "zhihu": {
        "name": "知乎短篇", "group": "其他",
        **_p(
            selling_point="问答体钩子强，正文像亲历者在讲一件超规格的事。",
            subgenres=_sg(("行业黑幕", "知情人"), ("超自然经历", "我遇到了"),
                          ("历史揭秘", "档案口吻")),
            taboos=["标题党正文空", "人设突然全知", "结尾鸡汤收束"],
            pacing="开头抛非常规结论，中段补证据，结尾留一个未解。",
            satisfaction=["细节对上", "身份反转", "读者觉得「有人经历过」"],
            mechanics=["第一人称限知", "数字和流程要像行业"],
            world_hints=["评论区气质可作配角声音", "一篇一个核心事件"],
        ),
    },
    "kangzhan": {
        "name": "抗战谍战", "group": "其他",
        **_p(
            selling_point="情报、牺牲和身份，一次失误换一条线的命。",
            subgenres=_sg(("地下党", "潜伏"), ("军统/敌特", "对线"),
                          ("民间武装", "补给与汉奸")),
            taboos=["历史事件胡编战果", "刑讯当娱乐", "汉奸脸谱无利益"],
            pacing="一次行动一个闭环，间隙写身份压力。",
            satisfaction=["送出情报", "除掉内鬼", "撤离成功", "代价被看见"],
            mechanics=["通信一次性、易暴露", "经费和证件是瓶颈"],
            world_hints=["地名战役需自洽", "普通人的怕是真实的"],
        ),
    },
    "lishi-gudai": {
        "name": "历史古代", "group": "其他",
        **_p(
            selling_point="典章、补给和人事，权谋靠制度而不是开挂。",
            subgenres=_sg(("朝堂", "党争与边事"), ("军旅", "粮草先行"),
                          ("地方官", "刑名钱粮")),
            taboos=["穿越知识无阻力", "官职品级混乱", "万人军无后勤"],
            pacing="一案/一仗/一折奏章有结果，再推更大的局。",
            satisfaction=["办成一案", "守住一座城", "人事任命", "读懂圣意"],
            mechanics=["文书、驿传、季节影响决策", "情面和法冲突"],
            world_hints=["称谓官职稳定", "民生数字比口号有力"],
        ),
    },
    "lishi-naodong": {
        "name": "历史脑洞", "group": "其他",
        **_p(
            selling_point="历史骨架 + 非常规变量，变量要改写已知结局的路径。",
            subgenres=_sg(("穿越执政", "制度实验"), ("平行史", "关键点分叉"),
                          ("器物金手指", "技术扩散")),
            taboos=["古人瞬间现代化无阻力", "名场面原样复读", "变量从不失败"],
            pacing="每次用变量都引发制度或舆论反弹。",
            satisfaction=["改写一场著名失败", "技术落地", "被时代反噬再调整"],
            mechanics=["识字率、利益集团、运输是刹车", "金手指有损耗"],
            world_hints=["读者要看得出「哪一点被拧弯了」", "名人出场要有事做"],
        ),
    },
    "heian": {
        "name": "黑暗题材", "group": "其他",
        **_p(
            selling_point="压迫结构清楚，人物选择脏，胜利带罪。",
            subgenres=_sg(("犯罪纪实感", "团伙与警察"), ("人性实验", "绝境道德"),
                          ("暴政日常", "活下去")),
            taboos=["虐只为虐无主题", "突然光明结局洗地", "受害者无声音"],
            pacing="压力只增不减，间歇给虚假出口再收回。",
            satisfaction=["暂时活命", "揭开结构", "很小的正义", "拒绝廉价救赎"],
            mechanics=["暴力有身体和社会后果", "同盟会背叛"],
            world_hints=["黑暗要具体到制度，不靠形容词", "读者需要一个可跟随的动机"],
        ),
    },
}

def empty_fields() -> dict[str, Any]:
    return {
        "selling_point": "",
        "subgenres": [],
        "taboos": [],
        "pacing": "",
        "satisfaction": [],
        "mechanics": [],
        "world_hints": [],
    }


def get_root(pack_id: str | None) -> dict[str, Any] | None:
    if not pack_id:
        return None
    root = PACKS.get(pack_id)
    return deepcopy(root) if root else None


def catalog_entries() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for _group, ids in GROUPS:
        for pid in ids:
            root = PACKS[pid]
            item = {"id": pid, "name": root["name"], "group": root["group"]}
            item.update(deepcopy({k: root[k] for k in FIELD_KEYS}))
            out.append(item)
    return out


def display_genre(primary_id: str | None, secondary_id: str | None) -> str:
    primary = get_root(primary_id)
    if primary is None:
        return UNSELECTED_NAME
    name = str(primary["name"])
    secondary = get_root(secondary_id)
    if secondary is not None:
        return f"{name}+{secondary['name']}"
    return name


def _copy_fields(root: dict[str, Any]) -> dict[str, Any]:
    return deepcopy({k: root.get(k, empty_fields()[k]) for k in FIELD_KEYS})


def compose_fields(primary_id: str | None, secondary_id: str | None) -> dict[str, Any]:
    """主题材整份；辅题材叠加 mechanics / satisfaction / selling_point，禁忌并集。"""
    primary = get_root(primary_id)
    if primary is None:
        return empty_fields()
    fields = _copy_fields(primary)
    secondary = get_root(secondary_id)
    if secondary is None:
        return fields
    extra_sell = str(secondary.get("selling_point") or "").strip()
    if extra_sell:
        base = str(fields.get("selling_point") or "").strip()
        tag = f"辅题材（{secondary['name']}）：{extra_sell}"
        fields["selling_point"] = f"{base}\n{tag}" if base else tag
    fields["mechanics"] = list(fields["mechanics"]) + list(secondary.get("mechanics") or [])
    fields["satisfaction"] = list(fields["satisfaction"]) + list(secondary.get("satisfaction") or [])
    seen = set(fields["taboos"])
    merged = list(fields["taboos"])
    for item in secondary.get("taboos") or []:
        if item not in seen:
            seen.add(item)
            merged.append(item)
    fields["taboos"] = merged
    return fields


def _clean_fields(raw: dict[str, Any] | None) -> dict[str, Any]:
    src = raw or {}
    out = empty_fields()
    if isinstance(src.get("selling_point"), str):
        out["selling_point"] = src["selling_point"].strip()
    if isinstance(src.get("pacing"), str):
        out["pacing"] = src["pacing"].strip()
    for key in ("taboos", "satisfaction", "mechanics", "world_hints"):
        val = src.get(key)
        if isinstance(val, list):
            out[key] = [str(x).strip() for x in val if str(x).strip()]
    subs = src.get("subgenres")
    cleaned: list[dict[str, str]] = []
    if isinstance(subs, list):
        for item in subs:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            hook = str(item.get("hook") or "").strip()
            if name:
                cleaned.append({"name": name, "hook": hook})
    out["subgenres"] = cleaned
    return out


def validate_selection(primary_id: str | None, secondary_id: str | None) -> None:
    if primary_id and primary_id not in PACKS:
        raise ValueError(f"未知主题材：{primary_id}")
    if secondary_id:
        if not primary_id:
            raise ValueError("辅题材必须先有主题材")
        if secondary_id not in PACKS:
            raise ValueError(f"未知辅题材：{secondary_id}")
        if secondary_id == primary_id:
            raise ValueError("辅题材不能与主题材相同")


def build_book_pack(primary_id: str | None, secondary_id: str | None,
                    overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    validate_selection(primary_id, secondary_id)
    fields = compose_fields(primary_id, secondary_id)
    if overrides:
        cleaned = _clean_fields(overrides)
        for key in FIELD_KEYS:
            if key in overrides:
                fields[key] = cleaned[key]
    primary = get_root(primary_id)
    secondary = get_root(secondary_id)
    pack = {
        "source_id": primary_id,
        "source_name": primary["name"] if primary else UNSELECTED_NAME,
        "secondary_id": secondary_id,
        "secondary_name": secondary["name"] if secondary else None,
        **fields,
    }
    pack["baseline"] = deepcopy({k: pack[k] for k in (*FIELD_KEYS,)})
    return pack


def is_managed_pack(pack: Any) -> bool:
    return isinstance(pack, dict) and bool(pack) and "baseline" in pack


def public_pack(pack: Any) -> dict[str, Any]:
    if not is_managed_pack(pack):
        return {}
    out = {k: deepcopy(pack.get(k)) for k in (
        "source_id", "source_name", "secondary_id", "secondary_name", *FIELD_KEYS,
    )}
    return out


def apply_field_edits(pack: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    if not is_managed_pack(pack):
        raise ValueError("本书创建时未选题材包")
    updated = deepcopy(pack)
    raw = overrides or {}
    cleaned = _clean_fields(raw)
    for key in FIELD_KEYS:
        if key in raw:
            updated[key] = cleaned[key]
    return updated


def restore_baseline(pack: dict[str, Any]) -> dict[str, Any]:
    if not is_managed_pack(pack):
        raise ValueError("本书创建时未选题材包")
    updated = deepcopy(pack)
    updated.update(_clean_fields(pack.get("baseline") if isinstance(pack.get("baseline"), dict) else {}))
    return updated


def fields_have_content(fields: dict[str, Any] | None) -> bool:
    data = fields or {}
    if str(data.get("selling_point") or "").strip() or str(data.get("pacing") or "").strip():
        return True
    for key in ("taboos", "satisfaction", "mechanics", "world_hints", "subgenres"):
        if data.get(key):
            return True
    return False


def format_prompt(pack: Any) -> str:
    if not is_managed_pack(pack) and not (isinstance(pack, dict) and fields_have_content(pack)):
        return ""
    data = pack if isinstance(pack, dict) else {}
    parts: list[str] = []
    label = data.get("source_name") or UNSELECTED_NAME
    extra = data.get("secondary_name")
    title = f"{label}+{extra}" if extra else str(label)
    parts.append(f"题材：{title}")
    if data.get("selling_point"):
        parts.append(f"核心卖点：{data['selling_point']}")
    subs = data.get("subgenres") or []
    if isinstance(subs, list) and subs:
        bits = []
        for item in subs:
            if isinstance(item, dict) and item.get("name"):
                hook = f"（{item['hook']}）" if item.get("hook") else ""
                bits.append(f"{item['name']}{hook}")
        if bits:
            parts.append("流派提示：" + "；".join(bits))
    if data.get("pacing"):
        parts.append(f"节奏：{data['pacing']}")
    for label_cn, key in (("爽点", "satisfaction"), ("禁忌", "taboos"),
                          ("机制/数值", "mechanics"), ("世界观法则", "world_hints")):
        items = [str(x).strip() for x in (data.get(key) or []) if str(x).strip()]
        if items:
            parts.append(f"{label_cn}：" + "；".join(items))
    return "\n".join(parts)


def taboo_hints(pack: Any) -> list[str]:
    if not isinstance(pack, dict):
        return []
    return [str(x).strip() for x in (pack.get("taboos") or []) if str(x).strip()]


def volume_span_for(pack: Any) -> int:
    """本书题材对应的「大约多少章一卷」。快节奏短卷，慢节奏长卷。"""
    if not isinstance(pack, dict):
        return VOLUME_SPAN_DEFAULT
    sid = pack.get("source_id") or pack.get("primary_id")
    if sid in _FAST_PACKS:
        return VOLUME_SPAN_FAST
    if sid in _SLOW_PACKS:
        return VOLUME_SPAN_SLOW
    return VOLUME_SPAN_DEFAULT


def suggest_volume_count(chapter_count: int, pack: Any) -> int:
    """按题材卷跨度估算卷数，夹在 3–20，避免大纲 JSON 爆炸。"""
    span = max(1, volume_span_for(pack))
    n = max(3, int(round(int(chapter_count or 0) / span)))
    return min(n, 20)
