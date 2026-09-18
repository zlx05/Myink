"""大上下文回归：保留硬约束、可选记忆裁剪、工具轮超限不发送请求。"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from myink.context_budget import ContextBudgetExceeded, estimate_tokens, fit_prompt
from myink.workflow import prompts, nodes


def test_optional_memory_trimmed_without_mutating_shared_context():
    context = {
        'long_term_facts': [{'content': '不得复活', 'is_hard': True}],
        'mid_term_events': [{'summary': '事件' * 200, 'chapter': i} for i in range(40)],
        'short_context': [{'kind': 'user_instruction', 'text': '保持第一人称'}],
    }
    original = deepcopy(context)
    budget = estimate_tokens(prompts._plan_messages({**context, 'mid_term_events': []})) + 500
    result = fit_prompt(context, prompts._plan_messages, budget)
    assert estimate_tokens(result) <= budget
    assert '不得复活' in str(result)
    assert '保持第一人称' in str(result)
    assert context == original


def test_required_content_over_budget_is_never_silently_truncated():
    context = {'long_term_facts': [{'content': '完整硬约束' * 2000, 'is_hard': True}]}
    with pytest.raises(ContextBudgetExceeded, match='CONTEXT_BUDGET_EXCEEDED'):
        fit_prompt(context, prompts._plan_messages, 2000)


@pytest.mark.parametrize('node', ['plan', 'write', 'extract', 'audit', 'revise'])
def test_public_prompt_builders_enforce_final_message_budget(monkeypatch, node):
    monkeypatch.setattr(prompts, 'settings', SimpleNamespace(request_token_budget=9000))
    context = {
        'long_term_facts': [{'content': '规则' * 300, 'is_hard': False} for _ in range(30)],
        'entity_snapshots': [{'name': '人物', 'state': {'location': '地名' * 300}} for _ in range(30)],
        'mid_term_events': [{'summary': '事件' * 300} for _ in range(30)],
        'open_foreshadows': [{'description': '伏笔' * 300} for _ in range(30)],
    }
    result = {
        'plan': lambda: prompts.plan_messages(context),
        'write': lambda: prompts.write_messages(context, {}),
        'extract': lambda: prompts.extract_messages('完整正文', 1, context),
        'audit': lambda: prompts.audit_messages('完整正文', {}, context, 1),
        'revise': lambda: prompts.revise_messages('完整正文', [], 1, context=context),
    }[node]()
    assert estimate_tokens(result) <= 8000
    if node in ('extract', 'audit', 'revise'):
        assert '完整正文' in str(result)


def test_tool_results_over_budget_stop_before_calling_provider(monkeypatch):
    monkeypatch.setattr(nodes, 'settings', SimpleNamespace(request_token_budget=200))
    chain = SimpleNamespace(chain=['test-model'], generate=Mock())
    result = nodes._bounded_generate(chain, [
        {'role': 'system', 'content': '规则'},
        {'role': 'tool', 'tool_call_id': 'call1', 'content': '结果' * 200},
    ])
    assert 'CONTEXT_BUDGET_EXCEEDED' in result.error
    chain.generate.assert_not_called()


def test_small_prompt_is_unchanged():
    context = {'long_term_facts': [{'content': '不得复活', 'is_hard': True}]}
    assert prompts.plan_messages(context) == prompts._plan_messages(context)


@pytest.mark.parametrize('node', ['write', 'audit'])
def test_prompt_reserves_actual_tool_schema_size(monkeypatch, node):
    from myink.workflow.tools import READ_TOOLS
    monkeypatch.setattr(prompts, 'settings', SimpleNamespace(request_token_budget=12000))
    context = {'entity_snapshots': [{'name': f'人物{i}', 'state': {'location': '地点' * 200}}
                                     for i in range(40)],
               'short_context': [{'kind': 'prev_chapter_tail', 'chapter': 1, 'tail': '把工具给我。'}]}
    messages = (prompts.write_messages(context, {}) if node == 'write' else
                prompts.audit_messages('妹妹递来螺丝刀。', {}, context, 2))
    assert '把工具给我。' in str(messages)
    assert estimate_tokens({'messages': messages, 'tools': READ_TOOLS}) <= 11000


def test_manual_memory_correction_returns_budget_error_without_model_call(monkeypatch):
    model = Mock()
    monkeypatch.setattr(nodes, 'make_chain', model)
    candidates, error = nodes.extract_candidates_from_draft(
        None, project_id='test', chapter_seq=1, draft='正文' * 50000)
    assert candidates == [] and 'CONTEXT_BUDGET_EXCEEDED' in error
    model.assert_not_called()


@pytest.mark.parametrize('chars', [3500, 6000, 10000])
def test_full_length_chapter_survives_all_post_write_stages(chars):
    draft=('正文中的完整因果和对白。'*1000)[:chars]
    ctx={'short_context':[{'kind':'prev_chapter_tail','tail':'前章接续'*160}],
         'long_term_facts':[{'content':'禁止死者无因复活','is_hard':True}],
         'recent_openings':[{'chapter':i,'text':'旧章开头'*44} for i in range(4)]}
    for messages in (prompts.extract_messages(draft,5,ctx), prompts.audit_messages(draft,{},ctx,5),
                     prompts.revise_messages(draft,[],5,context=ctx)):
        assert draft in '\n'.join(m['content'] for m in messages)
        assert estimate_tokens(messages) < 64000
