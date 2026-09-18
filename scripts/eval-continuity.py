"""小样本真实模型接续评测。显式 --live 才调用配置的模型；不读写任何小说数据库。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from myink.providers.deepseek import DeepSeekProvider
from myink.schemas import AuditVerdict, ChapterPlan
from myink.validation.continuity import check_transition_anchor
from myink.workflow import prompts
from myink.workflow.nodes import _check_draft, _parse_json


CASES = [
    {
        'name': 'completed_entry_replayed', 'expected': 'rewrite',
        'tail': '沈砚已经跨进体育馆，铁门在身后关上。周野的伤腿还在流血，他扶着周野坐到长椅上，向值班医生挥手。',
        'draft': '体育馆的铁门半掩着。沈砚站在门外推了推，门被铁链拴住了。他敲门报上姓名，等守卫解开铁链，才扶着周野走进去。值班医生正在长椅旁，沈砚向医生求助。医生拿来纱布，替周野包扎了伤腿。',
        'goal': '在体育馆内救治周野',
    },
    {
        'name': 'pending_threat_dropped', 'expected': 'rewrite',
        'tail': '兽爪从门缝探了进来，门闩正一点点弯折。沈砚抵住铁门，朝妹妹伸手：“把那把螺丝刀给我，快！”妹妹抓起工具。',
        'draft': '晚上十一点刚过，沈棠把最后一口糖水喂给沈砚。他躺在长椅上，揉了揉酸痛的肩。体育馆里很安静，兄妹聊起从前的晚饭，接着清点了明天要带的食物。沈砚打了个哈欠，决定先睡一会儿。',
        'goal': '兄妹守住体育馆后门',
    },
    {
        'name': 'natural_same_scene_continuation', 'expected': 'pass',
        'tail': '兽爪从门缝探了进来，门闩正一点点弯折。沈砚抵住铁门，朝妹妹伸手：“把那把螺丝刀给我，快！”妹妹抓起工具。',
        'draft': '螺丝刀落进掌心，沈砚反手把刀杆插进门闩下方的锁孔。铁门又撞了一下，刀杆弯了，却卡住了继续外滑的门闩。“去搬长椅。”他对沈棠说。妹妹拖来长椅，两人把椅背顶在门上。那只兽爪终于缩了回去，兄妹趁撞击声暂歇，退到立柱后，轮流盯着后门。',
        'goal': '兄妹守住体育馆后门',
    },
    {
        'name': 'justified_recovery_and_time_jump', 'expected': 'pass',
        'tail': '守卫将袭击者赶出院子，反锁了门。医生说止血成功，需要留观。沈砚听见妹妹答应守着他，随后因失血昏了过去。',
        'draft': '床头的输液袋已换过一次。沈砚睁开眼，先摸到左臂新缠的绷带，妹妹从椅子上直起身：“天亮了，医生说你没再出血。”院门外有守卫交班的脚步声。他让妹妹扶自己坐起，问清药品只够用到中午，便在地图上圈出了附近药房的位置。',
        'goal': '沈砚恢复意识并准备寻找药品',
    },
    {
        'name': 'mechanical_opening_repeated', 'expected': 'rewrite',
        'tail': '沈砚把搜来的药品放进箱子，医生示意他去院内找守卫领取当天的任务。',
        'history': '清晨的冷风贴着门缝钻进来，沈砚揉了揉酸痛的肩，掀开毯子坐起。屋里很静，只有远处传来几声咳嗽。他看了一眼窗外灰白的天，伸手摸了摸衣袋里的匕首。',
        'draft': '清晨的凉风顺着门缝钻进来，沈砚揉着发酸的肩，掀开毯子坐了起来。房间很安静，远处只有几声咳嗽。他望了一眼窗外灰白的天空，伸手摸向衣袋里的匕首。随后他走到院里，找到守卫，领到了今天巡查围墙的任务。',
        'goal': '领取当天巡查任务',
    },
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='确认调用已配置的真实模型（产生正常 API 用量）')
    parser.add_argument('--model', default='deepseek-v4-flash')
    parser.add_argument('--output', type=Path, default=Path('docs/evaluations/continuity-smoke.json'))
    args = parser.parse_args()
    if not args.live:
        parser.error('真实模型评测需要显式 --live')
    provider = DeepSeekProvider()
    results = {'timestamp': datetime.now(timezone.utc).isoformat(), 'model': args.model,
               'scope': '5 个合成片段 + 1 次规划/写作/审核；非长篇质量或相似度改善的统计证明',
               'calls': [], 'cases': []}

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def call(label, messages, json_mode=True):
        response = provider.generate(messages, model_id=args.model, max_tokens=2400,
                                     temperature=0.2, json_mode=json_mode, disable_thinking=True)
        results['calls'].append({'label': label, 'input_tokens': response.input_tokens,
                                 'output_tokens': response.output_tokens, 'duration_ms': response.duration_ms,
                                 'error': response.error})
        save()
        if response.error:
            raise RuntimeError(f'{label}: {response.error}')
        print(f'{label}: input={response.input_tokens}, output={response.output_tokens}', flush=True)
        return response.content

    def context(case):
        return {'short_context': [{'kind': 'prev_chapter_tail', 'chapter': 5, 'tail': case['tail']}],
                'recent_openings': [{'chapter': 4, 'text': case['history']}] if case.get('history') else []}

    for case in CASES:
        plan = {'goals': [case['goal']], 'expected_events': [case['goal']]}
        verdict = AuditVerdict(**_parse_json(call(case['name'], prompts.audit_messages(
            case['draft'], plan, context(case), 6)))).model_dump(mode='json')
        matched = verdict['verdict'] == case['expected']
        results['cases'].append({**case, 'actual': verdict, 'matched': matched})
        save()
        print(f"  expected={case['expected']}, actual={verdict['verdict']}, matched={matched}", flush=True)

    case = CASES[1]
    ctx = context(case)
    plan = ChapterPlan(**_parse_json(call('plan', prompts.plan_messages(ctx, case['goal'])))).model_dump(mode='json')
    check_transition_anchor(plan, ctx)
    draft = _check_draft(call('write', prompts.write_messages(ctx, plan, target_words=800), False))
    verdict = AuditVerdict(**_parse_json(call('generated_audit', prompts.audit_messages(draft, plan, ctx, 6)))).model_dump(mode='json')
    results['generation'] = {'tail': case['tail'], 'plan': plan, 'draft': draft, 'audit': verdict}
    save()
    if verdict['verdict'] != 'pass':
        revised = _check_draft(call('revise', prompts.revise_messages(
            draft, verdict['findings'], 6, context=ctx, plan=plan, target_words=800), False))
        second = AuditVerdict(**_parse_json(call('revised_audit', prompts.audit_messages(revised, plan, ctx, 6)))).model_dump(mode='json')
        results['generation']['revision'] = {'draft': revised, 'audit': second}
        save()
    print(f"Saved {args.output}; matched={sum(c['matched'] for c in results['cases'])}/{len(CASES)}", flush=True)


if __name__ == '__main__':
    main()
