import json

import pytest

from app.harness import FinalFormatError, parse_final
from app.llm import _extract_json, _parse_score, _parse_turn, _parse_vote


# ---- _parse_score ----

def test_parse_score_pure_json():
    assert _parse_score('{"score": 65}') == 65


def test_parse_score_embedded_json_in_prose():
    assert _parse_score('I would rate it {"score": 80} overall.') == 80


def test_parse_score_string_value_with_percent():
    assert _parse_score('{"score": "72%"}') == 72


def test_parse_score_bool_rejected_returns_default():
    assert _parse_score('{"score": true}') == 50
    assert _parse_score('{"score": false}', default=42) == 42


def test_parse_score_clamps_below_zero():
    assert _parse_score('{"score": -5}') == 0


def test_parse_score_clamps_above_hundred():
    assert _parse_score('{"score": 150}') == 100


def test_parse_score_bare_integer_fallback():
    assert _parse_score("I give it 42 out of 100.") == 42


def test_parse_score_no_number_returns_default():
    assert _parse_score("no idea") == 50
    assert _parse_score("", default=33) == 33


# ---- _extract_json ----

def test_extract_json_fenced_block():
    text = '```json\n{"a": 1}\n```'
    assert _extract_json(text) == {"a": 1}


def test_extract_json_unfenced_object():
    assert _extract_json('{"a": 1, "b": 2}') == {"a": 1, "b": 2}


def test_extract_json_with_surrounding_prose():
    text = 'Here is my answer: {"a": 1} hope it helps'
    assert _extract_json(text) == {"a": 1}


def test_extract_json_invalid_returns_none():
    assert _extract_json("{not valid json}") is None


def test_extract_json_empty_returns_none():
    assert _extract_json("") is None
    assert _extract_json(None) is None


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "```JSON\r\n{}\r\n```",
                                    "说明：\n```json\n{}\n```\n以上是结果。", "\ufeff{}"])
@pytest.mark.parametrize("content", [
    '# 方案\n```python\nprint({"nested": "value"})\n```\n后文不能丢失',
    r'字面量 \n 与 \\、"引号"、花括号 { }、数组 [ ] 和 ``` 都须保留',
    '中文 🐱\t\r\n<script>const x = {a: "}"};</script>',
])
def test_json_strings_survive_markdown_and_escaping(wrapper, content):
    data = {"speech": "发言内也可以有 ``` 和 {括号}", "propose_end": False,
            "whiteboard": {"ops": [{"op": "append", "content": content}]}}
    text = wrapper.format(json.dumps(data, ensure_ascii=False))
    assert _extract_json(text) == data
    # Both direct and Harness parsers must preserve the same exact strings.
    for parser in (_parse_turn, parse_final):
        turn = parser(text)
        assert turn["speech"] == data["speech"]
        assert turn["whiteboard_ops"] == data["whiteboard"]["ops"]


@pytest.mark.parametrize("text", [
    '{"speech":"first"} {"speech":"second"}',
    '```json\n{"speech":"first"}\n```\n```json\n{"speech":"second"}\n```',
    '{"outer":{"speech":"nested must not be salvaged"}',
    '{"speech":"first","speech":"ambiguous"}',
    '{"speech":"x","whiteboard":{"ops":[],"ops":[]}}',
    '{"speech":"x","score":NaN}', '{"speech":"x","score":Infinity}',
    '{"speech":"x","score":1e999}', '{"speech":"x"}}', '{"speech":"x"}]',
    '{"speech":"unterminated', '{"speech":"literal\ncontrol character"}',
    [], 42,
])
def test_invalid_or_ambiguous_json_is_not_silently_salvaged(text):
    assert _extract_json(text) is None


@pytest.mark.parametrize("op", [[], {}, None, False, 12])
def test_malformed_whiteboard_op_is_format_error_not_type_error(op):
    with pytest.raises(FinalFormatError):
        parse_final(json.dumps({"speech": "x", "whiteboard": {"ops": [{"op": op}]}}))


def test_empty_replace_target_is_not_silently_accepted():
    with pytest.raises(FinalFormatError):
        parse_final('{"speech":"x","whiteboard":{"ops":[{"op":"replace","find":"","replace":"x"}]}}')


# ---- _parse_vote ----

OPTIONS = ["苹果", "香蕉", "橙子"]


def test_parse_vote_choices_key():
    choices, reason = _parse_vote('{"choices": ["1", "2"], "reason": "都好吃"}', OPTIONS, 2)
    assert choices == ["1", "2"]
    assert reason == "都好吃"


def test_parse_vote_votes_key():
    choices, _ = _parse_vote('{"votes": ["1"]}', OPTIONS, 1)
    assert choices == ["1"]


def test_parse_vote_selections_key():
    choices, _ = _parse_vote('{"selections": ["3"]}', OPTIONS, 1)
    assert choices == ["3"]


def test_parse_vote_single_choice_scalar():
    choices, _ = _parse_vote('{"choice": "2"}', OPTIONS, 1)
    assert choices == ["2"]


def test_parse_vote_numeric_label():
    choices, _ = _parse_vote('{"choices": ["1"]}', OPTIONS, 1)
    assert choices == ["1"]


def test_parse_vote_exact_option_text_mapping():
    choices, _ = _parse_vote('{"choices": ["香蕉"]}', OPTIONS, 1)
    assert choices == ["2"]


def test_parse_vote_digit_embedded_value():
    choices, _ = _parse_vote('{"choices": ["option 2"]}', OPTIONS, 1)
    assert choices == ["2"]


def test_parse_vote_list_of_dicts_items():
    choices, _ = _parse_vote(
        '{"choices": [{"choice": "1"}, {"option": "香蕉"}]}', OPTIONS, 2
    )
    assert choices == ["1", "2"]


def test_parse_vote_over_count_truncated():
    choices, _ = _parse_vote('{"choices": ["1", "2", "3"]}', OPTIONS, 2)
    assert choices == ["1", "2"]


def test_parse_vote_under_count_padded_with_first_choice():
    choices, _ = _parse_vote('{"choices": ["2"]}', OPTIONS, 3)
    assert choices == ["2", "2", "2"]


def test_parse_vote_empty_abstains_instead_of_selecting_first_option():
    choices, reason = _parse_vote("not json at all", OPTIONS, 2)
    assert choices == []
    assert reason == ""


def test_parse_vote_reason_extraction():
    _, reason = _parse_vote('{"choices": ["1"], "reason": "最喜欢"}', OPTIONS, 1)
    assert reason == "最喜欢"
