"""对齐 inkos think-tag-stripper：只剥响应起始处的完整 think 块。"""

from myink.providers.think_tag_stripper import (
    LeadingThinkTagStripper,
    isolate_response_body,
    looks_like_unclosed_think,
    strip_leading_think_block,
)


def test_strip_complete_leading_block_and_following_whitespace():
    assert strip_leading_think_block("<think>推理</think>\n\n正文开始。") == "正文开始。"


def test_strip_leading_block_preceded_by_whitespace():
    assert strip_leading_think_block("\n<think>推理</think>正文") == "正文"


def test_leaves_mid_text_occurrences_untouched():
    text = "正文里介绍 <think> 标签的用法。"
    assert strip_leading_think_block(text) == text


def test_leaves_unterminated_leading_block_untouched():
    text = "<think>推理到一半被截断"
    assert strip_leading_think_block(text) == text
    assert looks_like_unclosed_think(text)


def test_plain_text_unchanged():
    assert strip_leading_think_block("普通正文。") == "普通正文。"


def test_stream_suppresses_leading_block_split_across_chunks():
    stripper = LeadingThinkTagStripper()
    emitted = [
        stripper.push("<th"),
        stripper.push("ink>推理A"),
        stripper.push("推理B</th"),
        stripper.push("ink>\n正文"),
        stripper.push("继续"),
    ]
    assert "".join(emitted) == "正文继续"
    assert stripper.flush() == ""


def test_stream_emits_once_prefix_diverges():
    stripper = LeadingThinkTagStripper()
    emitted = [
        stripper.push("<th"),
        stripper.push("ree>不是 think 标签"),
        stripper.push("，正文"),
    ]
    assert "".join(emitted) == "<three>不是 think 标签，正文"
    assert stripper.flush() == ""


def test_stream_passes_plain_text_immediately():
    stripper = LeadingThinkTagStripper()
    assert stripper.push("正文第一段") == "正文第一段"
    assert stripper.push(" 正文中间的 <think> 字样不受影响") == " 正文中间的 <think> 字样不受影响"
    assert stripper.flush() == ""


def test_isolate_never_merges_reasoning_into_prose():
    assert isolate_response_body("", reasoning="先推敲再写", json_mode=False) == ""
    assert isolate_response_body("<think>推理</think>\n他推开门。") == "他推开门。"
    assert isolate_response_body("", reasoning='{"verdict":"pass"}', json_mode=True) == '{"verdict":"pass"}'


def test_stream_flush_returns_unterminated_block():
    stripper = LeadingThinkTagStripper()
    assert stripper.push("<think>推理没有闭合") == ""
    flushed = stripper.flush()
    assert flushed == "<think>推理没有闭合"
    assert looks_like_unclosed_think(flushed)
