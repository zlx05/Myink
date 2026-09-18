"""跨章接续回归：真实前章、提示词证据、修订后记忆和有限审核。"""
import uuid

import pytest

from myink.db import tenant_session
from myink.memory.recall import build_context, _tail_of
from myink.models import Chapter, Event
from myink.schemas import ChapterPlan
from myink.validation.continuity import (
    check_transition_anchor,
    opening_excerpt,
    repair_generated_transition_anchor,
)
from myink.workflow import nodes, prompts
from myink.workflow.chapter_graph import build_chapter_graph


TAIL = '沈砚已经跨进体育馆。铁门在身后关上。他将受伤的周野扶到长椅边。'
OPENING = '沈砚推开铁门，门轴发出刺耳的声音。'
CONTEXT = {
    'short_context': [{'kind': 'prev_chapter_tail', 'chapter': 5, 'tail': TAIL}],
    'recent_openings': [{'chapter': 5, 'text': OPENING}],
    'entity_snapshots': [{'name': '周野', 'state': {'injury': '左腿受伤'}}],
}
PLAN = {'goals': ['救治周野'], 'expected_events': ['寻找医生'], 'transition': {
    'mode': 'continue', 'anchor_quote': '他将受伤的周野扶到长椅边。',
    'pending_action': '安排救治', 'opening_beat': '周野按住渗血的伤口，向医生求助', 'bridge': '',
}}


def test_recall_uses_exact_predecessor_even_with_current_and_future(temp_project, fake_embedder):
    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        for seq, content in [(4, '更早章头'), (5, TAIL), (6, '当前旧稿'), (7, '未来剧情')]:
            db.add(Chapter(project_id=pid, chapter_seq=seq, status='confirmed', content=content))
            db.add(Event(project_id=pid, source_chapter=seq, summary=f'事件{seq}', confidence=1))
        db.flush()
        ctx = build_context(db, project_id=pid, chapter_seq=6)
    assert next(x['tail'] for x in ctx.short_context if x['kind'] == 'prev_chapter_tail') == TAIL
    assert [x['chapter'] for x in ctx.recent_openings] == [5, 4]
    assert {x['chapter'] for x in ctx.mid_term_events} == {4, 5}


def test_missing_predecessor_does_not_pretend_older_chapter_is_previous(temp_project):
    pid = uuid.UUID(temp_project)
    with tenant_session(temp_project) as db:
        db.add(Chapter(project_id=pid, chapter_seq=1, status='confirmed', content=TAIL))
        db.flush()
        ctx = build_context(db, project_id=pid, chapter_seq=3)
    assert not any(row['kind'] == 'prev_chapter_tail' for row in ctx.short_context)
    assert ctx.recent_openings[0]['chapter'] == 1


def test_excerpt_preserves_scene_not_title_and_tail_keeps_action_context():
    assert opening_excerpt('# 第五章 进门\n\n---\n' + OPENING) == OPENING
    long = '前文' * 600 + '\n' + TAIL * 12
    tail = _tail_of(long)
    assert len(tail) <= 800
    assert tail.endswith(TAIL)
    assert len(tail) > 300
    assert opening_excerpt(None) == ''
    assert _tail_of('妹妹接过工具。\n' + TAIL) == '妹妹接过工具。\n' + TAIL


def test_all_creation_stages_receive_boundary_evidence():
    stages = [prompts.plan_messages(CONTEXT), prompts.write_messages(CONTEXT, PLAN),
              prompts.audit_messages('待审正文', PLAN, CONTEXT, 6),
              prompts.revise_messages('待修正文', [{'conflict_key': 'entry-replay', 'suggestion': '不要再次进门'}],
                                      6, context=CONTEXT, plan=PLAN,
                                      style_profile={'pov': '第三人称限知'}, target_words=1200)]
    for messages in stages:
        text = '\n'.join(m['content'] for m in messages)
        assert TAIL in text
        assert OPENING in text
        assert '近期章节开头' in text
    revised = str(stages[-1])
    assert '救治周野' in revised and 'entry-replay' in revised
    assert '第三人称限知' in revised and '1200' in revised


def test_transition_schema_and_anchor_verification():
    plan = ChapterPlan(**PLAN).model_dump()
    check_transition_anchor(plan, CONTEXT)
    plan['transition']['anchor_quote'] = '不存在的原文'
    with pytest.raises(ValueError, match='逐字引用'):
        check_transition_anchor(plan, CONTEXT)
    with pytest.raises(ValueError, match='必须为空'):
        check_transition_anchor(plan, {})
    plan['transition'].update(anchor_quote='', mode='time_jump', bridge='')
    with pytest.raises(ValueError, match='bridge'):
        check_transition_anchor(plan, {})
    check_transition_anchor({'goals': ['旧计划']}, CONTEXT)


def test_generated_transition_anchor_repair_is_narrow_and_exact():
    plan = {
        'goals': ['继续追踪'],
        'transition': {
            'mode': 'continue', 'anchor_quote': '意思相近但并非原文',
            'pending_action': '追踪', 'opening_beat': '起身', 'bridge': '',
        },
    }

    repaired = repair_generated_transition_anchor(plan, CONTEXT)

    assert repaired is not plan
    assert repaired['goals'] == plan['goals']
    assert repaired['transition']['anchor_quote'] in TAIL
    assert plan['transition']['anchor_quote'] == '意思相近但并非原文'
    check_transition_anchor(repaired, CONTEXT)


def test_revision_reextracts_memory_and_clears_old_findings(monkeypatch):
    order = []
    for name in ('load_state', 'recall', 'plan_cast', 'plan_chapter', 'summarize'):
        monkeypatch.setattr(nodes, f'node_{name}', lambda state: {})
    monkeypatch.setattr(nodes, 'node_write', lambda state: {'draft': '旧稿'})
    def extract(state):
        order.append('extract:' + state['draft'])
        return {'candidates': [{'text': state['draft']}]}
    def validate(state):
        order.append('validate:' + state['draft'])
        return {'report': {'summary': {}}, 'unresolved': []}
    def audit(state):
        order.append('audit:' + state['draft'])
        assert state['candidates'] == [{'text': state['draft']}]
        assert not state['unresolved']
        return {'audit_verdict': {'verdict': 'rewrite' if state['draft'] == '旧稿' else 'pass'},
                'unresolved': [{'conflict_key': 'old'}] if state['draft'] == '旧稿' else []}
    monkeypatch.setattr(nodes, 'node_extract', extract)
    monkeypatch.setattr(nodes, 'node_validate', validate)
    monkeypatch.setattr(nodes, 'node_audit', audit)
    monkeypatch.setattr(nodes, 'node_revise', lambda state: {'draft': '新稿', 'revision_count': 1})
    monkeypatch.setattr(nodes, 'node_persist', lambda state: {'persisted': True})
    result = build_chapter_graph().invoke({'project_id': str(uuid.uuid4()), 'chapter_seq': 6})
    assert result['persisted']
    assert order == ['extract:旧稿', 'validate:旧稿', 'audit:旧稿', 'extract:新稿', 'validate:新稿', 'audit:新稿']


def test_failed_semantic_audit_stays_for_review_and_explicit_resume_finishes(temp_project):
    state = {'project_id': temp_project, 'chapter_seq': 1, 'draft': TAIL, 'candidates': [],
             'report': {'summary': {}}, 'audit_verdict': {'verdict': 'rewrite'}}
    assert nodes.node_persist(state)['needs_review']
    with tenant_session(temp_project) as db:
        ch = db.query(Chapter).filter_by(project_id=temp_project, chapter_seq=1).one()
        assert ch.status == 'awaiting_review'
        assert ch.content == TAIL, '待确认只阻止设定落库，不应隐藏已经生成的正文'
    result = nodes.finalize_awaiting_review(state, task_id=str(uuid.uuid4()))
    assert not result['needs_review']
