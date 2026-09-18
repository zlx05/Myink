"""合成长篇真实模型评测：全流程生成 → 人工拒绝 → 修订复审。只写隔离库。"""
from __future__ import annotations
import argparse
import json
import uuid
from pathlib import Path
from langgraph.checkpoint.memory import InMemorySaver
from myink.config import settings
from myink.db import new_session, tenant_session
from myink.models import Project, ProjectSettings, Character, Chapter, MemoryCandidate, AgentRun, User
from myink.workflow.chapter_graph import build_chapter_graph
from myink.workflow.review import resolve_review
from myink.api.routes_candidates import reject_candidate, RejectCandidateIn

p=argparse.ArgumentParser()
p.add_argument('--live',action='store_true',required=True)
p.add_argument('--output',default='docs/evaluations/workflow-recovery.json')
a=p.parse_args()
if ':15432/' not in settings.database_url:
    raise SystemExit('只允许写入隔离测试数据库 15432')
with new_session() as db:
    user=db.query(User).first()
    project=Project(user_id=user.id,title='合成评测：雨夜检修',genre='现实',target_words=3000)
    db.add(project);db.flush();pid=str(project.id);db.commit()
with tenant_session(pid) as db:
    db.add(ProjectSettings(project_id=pid,world_rules={},hard_constraints=[
        '故事发生在现实世界，没有超能力；人物伤势须有合理恢复过程。'],style_profile={}))
    for name in ('林川','许宁'):
        db.add(Character(project_id=pid,name=name,realm_cap='普通人',personality='谨慎负责的检修员'))
tid=str(uuid.uuid4());graph=build_chapter_graph(checkpointer=InMemorySaver())
result=graph.invoke({'project_id':pid,'chapter_seq':1,'task_id':tid,
    'user_instruction':'暴雨导致山间观测站停电。林川左膝扭伤，与许宁协作检查供电，最后找到漏水的接线盒。全章仅两名人物，避免另建人物。'},
    config={'configurable':{'thread_id':tid},'recursion_limit':64})
if result.get('error'):
    raise RuntimeError(result['error'])
first={'chars':len(result.get('draft') or ''),'needs_review':result.get('needs_review')}
# 人为植入一个明确违反现实设定的结尾，验证拒绝会实际改正文。
bad='林川忽然获得了瞬间移动的超能力，眨眼就到了十公里外。'
result['draft'] += '\n'+bad
with tenant_session(pid) as db:
    ch=db.query(Chapter).filter_by(chapter_seq=1).one();ch.status='awaiting_review'
    cand=MemoryCandidate(project_id=pid,source_chapter=1,kind='fact',payload={'content':bad})
    db.add(cand);db.flush();cid=str(cand.id)
reject_candidate(pid,cid,RejectCandidateIn(reason='现实题材不允许瞬间移动；用人物实际检修动作推进。'))
result=resolve_review(graph,result,task_id=tid)
with tenant_session(pid) as db:
    runs=db.query(AgentRun).filter_by(task_id=tid).order_by(AgentRun.id).all()
    report={'fixture':'纯合成现实题材，不含用户小说内容','initial':first,
            'final_chars':len(result.get('draft') or ''),'error':result.get('error'),
            'needs_review':result.get('needs_review'),'bad_sentence_removed':bad not in result.get('draft',''),
            'runs':[{'node':r.node,'input_tokens':r.input_tokens,'output_tokens':r.output_tokens,
                     'error':r.error,'detail':r.detail} for r in runs]}
Path(a.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({k:v for k,v in report.items() if k!='runs'},ensure_ascii=False))
if result.get('error') or not report['bad_sentence_removed']:
    raise SystemExit(1)
