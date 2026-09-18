"""作者拒绝、三级路由、非修仙校验的闭环回归。"""
import uuid
from types import SimpleNamespace
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from myink.db import tenant_session
from myink.models import Chapter, MemoryCandidate, Character, CharacterState
from myink.workflow import nodes
from myink.workflow.chapter_graph import route_after_audit, build_chapter_graph
from myink.workflow.review import resolve_review, same_proposal
from myink.api.routes_candidates import reject_candidate, RejectCandidateIn


@pytest.mark.parametrize('verdict,rev,plan,expected', [
    ('pass',2,1,'persist'), ('rewrite',0,1,'revise'), ('rewrite',2,0,'needs_review'),
    ('replan',2,0,'replan_chapter'), ('replan',0,1,'needs_review'), (None,0,0,'needs_review')])
def test_independent_route_budgets(verdict,rev,plan,expected):
    assert route_after_audit({'audit_verdict': {'verdict':verdict,'replan_target':'chapter'},
                             'revision_count':rev,'replan_count':plan}) == expected


def test_rejected_setting_revises_and_reaudits(temp_project, monkeypatch):
    tid=str(uuid.uuid4())
    with tenant_session(temp_project) as db:
        db.add(Chapter(project_id=temp_project,chapter_seq=1,status='awaiting_review'))
        row=MemoryCandidate(project_id=temp_project,source_chapter=1,kind='fact',
                            payload={'content':'伤势无故痊愈'},status='pending')
        db.add(row); db.flush(); cid=str(row.id)
        db.add(MemoryCandidate(project_id=temp_project,source_chapter=1,kind='event',
                              payload={'summary':'旧稿事件'},status='pending'))
    reject_candidate(temp_project,cid,RejectCandidateIn(reason='需要治疗过程'))
    calls=[]
    def revise(s):
        assert '需要治疗过程' in str(s['context'])
        calls.append('revise')
        return {'draft':'他继续包扎伤口，仍无法行走。','revision_count':1}
    def extract(s):
        assert '包扎' in s['draft']; calls.append('extract'); return {'candidates':[]}
    def validate(s):
        calls.append('validate'); return {'report':{'summary':{}},'unresolved':[]}
    def audit(s):
        calls.append('audit'); return {'audit_verdict':{'verdict':'pass'}}
    for name,fn in [('revise',revise),('extract',extract),('validate',validate),('audit',audit)]:
        monkeypatch.setattr(nodes,'node_'+name,fn)
    monkeypatch.setattr(nodes,'node_summarize',lambda s:{})
    graph=build_chapter_graph(checkpointer=InMemorySaver())
    out=resolve_review(graph,{'project_id':temp_project,'chapter_seq':1,'draft':'伤势无故痊愈',
                             'needs_review':True,'candidates':[]},task_id=tid)
    assert calls==['revise','extract','validate','audit']
    assert not out['needs_review']
    with tenant_session(temp_project) as db:
        assert '包扎' in db.query(Chapter).filter_by(chapter_seq=1).one().content
        rows=db.query(MemoryCandidate).all()
        assert all(r.status=='rejected' for r in rows)
        assert db.get(MemoryCandidate,uuid.UUID(cid)).review['applied']
        assert any((r.review or {}).get('superseded') for r in rows)


def test_pending_candidates_cannot_be_silently_accepted(temp_project):
    with tenant_session(temp_project) as db:
        db.add(MemoryCandidate(project_id=temp_project,source_chapter=1,kind='fact',payload={'content':'待判断'}))
    result=resolve_review(None,{'project_id':temp_project,'chapter_seq':1,'draft':'原稿'},task_id=str(uuid.uuid4()))
    assert result['needs_review']


def test_non_fantasy_still_checks_resurrection(temp_project, monkeypatch):
    with tenant_session(temp_project) as db:
        c=Character(project_id=temp_project,name='测试角色',realm_cap='普通人'); db.add(c); db.flush(); cid=str(c.id)
        db.add(CharacterState(project_id=temp_project,character_id=cid,chapter_seq=1,source_chapter=1,
                              field='alive',new_value='false'))
    monkeypatch.setattr(nodes,'run_ledger_l2',lambda *a,**kw:[])
    result=nodes.node_validate({'project_id':temp_project,'chapter_seq':2,'settings':{},'draft':'角色复活',
        'candidates':[{'kind':'character_state','source_chapter':2,'payload':{'character_id':cid,'field':'alive',
                        'old_value':'false','new_value':'true'},'confidence':1}]})
    assert result['report']['summary']['critical'] > 0


def test_proposal_identity_ignores_extraction_metadata():
    assert same_proposal('character_card',{'name':'张三','personality':'谨慎'}, {'name':' 张三 ','personality':'冷静'})
    assert same_proposal('character_state',{'character_id':'a','field':'injury','new_value':'痊愈','confidence':.8},
                         {'character_id':'a','field':'injury','new_value':'痊愈','confidence':.9})
    assert not same_proposal('character_state',{'field':'injury','new_value':'痊愈'}, {'field':'injury','new_value':'重伤'})


def test_replan_retains_actionable_feedback():
    from myink.workflow.chapter_graph import node_reset_replan
    state={'context':{'short_context':[{'text':'前情'}]},
           'audit_verdict':{'verdict':'replan','reasons':['目标重复上一章'],'findings':[]}}
    result=node_reset_replan(state)
    assert '目标重复上一章' in str(result['context'])
    assert state['context']['short_context']==[{'text':'前情'}]
    assert result['replan_count']==1 and result['draft'] is None


def test_audit_pass_with_major_finding_does_not_bypass_route():
    assert route_after_audit({'audit_verdict':{'verdict':'pass','findings':[{'severity':'major'}]}})=='revise'


def test_review_pool_does_not_count_as_completed_chapter(temp_project):
    from myink.models import AgentRun
    from myink.api.routes_tasks import _batch_done_chapters
    tid=str(uuid.uuid4())
    with tenant_session(temp_project) as db:
        nodes.record_plain(db,project_id=temp_project,task_id=tid+':ch1',node='persist',detail={'status':'auto_confirm'})
        nodes.record_plain(db,project_id=temp_project,task_id=tid+':ch2',node='persist',detail={'status':'awaiting_review'})
        db.flush()
        assert _batch_done_chapters(db,tid)==1
