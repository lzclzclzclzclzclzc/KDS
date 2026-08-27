from app.llm import _extract_json, _parse_score, _parse_vote


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


def test_parse_vote_empty_defaults_to_first_label_padding():
    choices, reason = _parse_vote("not json at all", OPTIONS, 2)
    assert choices == ["1", "1"]
    assert reason == ""


def test_parse_vote_reason_extraction():
    _, reason = _parse_vote('{"choices": ["1"], "reason": "最喜欢"}', OPTIONS, 1)
    assert reason == "最喜欢"
