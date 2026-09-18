"""纯合成逻辑冲突正反例，真实审核模型；--live 显式开启。"""
import argparse
import json
from pathlib import Path
from myink.providers.deepseek import DeepSeekProvider
from myink.workflow import prompts
from myink.workflow.nodes import _parse_json

p=argparse.ArgumentParser();p.add_argument('--live',action='store_true',required=True);a=p.parse_args()
cases=[
 ('first_visit',False,'昨晚林川已在北桥站办理登记，今天再次来到原站房。','这是林川有生以来第一次来到北桥站，此前他从未到过这里。',''),
 ('return_visit',True,'昨晚林川已在北桥站办理登记，今天再次来到原站房。','林川推开熟悉的站房门，取出昨晚留下的登记回执。',''),
 ('destroyed_item',False,'林川唯一的万用表已烧成无法修复的残骸，他尚未获取替代品。','林川拿起那只完好无损的旧万用表，接上探针，顺利测出了电压。',''),
 ('replacement_item',True,'林川唯一的万用表已烧成无法修复的残骸。','许宁从工具库领来一只新万用表。林川把旧表残骸收进废物箱，再用新表测量。',''),
 ('unfulfilled_promise',False,'林川承诺修好水泵，但检修尚未开始，水泵仍完全损坏。','检修还没开始，大家就确认林川已经兑现修好水泵的承诺，水泵已经恢复运作。',''),
 ('promise_completed',True,'林川承诺修好水泵，但检修尚未开始，水泵仍完全损坏。','林川拆开水泵，更换烧毁的电容，重新接线。试运行成功后，他才告诉许宁承诺完成了。',''),
 ('capability_rule',False,'林川检查了仪器：外接电源已断开，电池也已取出。','没有接入任何电源，仪器却持续正常测量，林川直接读出了实时电流值。','该仪器必须外接电源或装入电池才能测量，没有其他供能方式。'),
 ('capability_restored',True,'林川检查了仪器：外接电源已断开，电池也已取出。','许宁装入新电池，按下开关。林川等自检完成后，才读取测量结果。','该仪器必须外接电源或装入电池才能测量，没有其他供能方式。'),
 ('allegiance',False,'林川已正式退出北桥检修队，合同解除且未重新入职。','林川的合同并未解除，他始终是北桥检修队的现任正式员工，今天仍以队员身份领薪。',''),
 ('former_member',True,'林川已正式退出北桥检修队，合同解除且未重新入职。','林川以外部维修承包人的身份递交报价单，说明自己已经不属于检修队。','')]
provider=DeepSeekProvider();out=[]
for name,expected,tail,draft,rule in cases:
 ctx={'short_context':[{'kind':'prev_chapter_tail','tail':tail}],
      'long_term_facts':[{'content':rule,'is_hard':True}] if rule else []}
 response=provider.generate(prompts.audit_messages(draft,{},ctx,2),model_id='deepseek-v4-flash',max_tokens=2000,json_mode=True,disable_thinking=True,temperature=.1)
 if response.error:raise RuntimeError(response.error)
 verdict=_parse_json(response.content)
 matched=(verdict['verdict']=='pass')==expected
 out.append({'name':name,'expected_pass':expected,'verdict':verdict,'matched':matched,
             'input_tokens':response.input_tokens,'output_tokens':response.output_tokens})
 print(name,verdict['verdict'],matched,flush=True)
 Path('docs/evaluations/logic-smoke.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
if not all(r['matched'] for r in out): raise SystemExit(1)
