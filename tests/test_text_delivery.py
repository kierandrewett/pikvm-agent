from pikvm_agent.pikvm.text import flatten_line_breaks


def test_line_break_boundary_emits_exactly_one_space() -> None:
    variants = (
        "word\nnext",
        "word \nnext",
        "word\n next",
        "word \n next",
        "word\r\nnext",
        "word \r\n next",
        "word\n\nnext",
    )

    assert {flatten_line_breaks(value) for value in variants} == {"word next"}


def test_same_line_spacing_is_not_silently_rewritten() -> None:
    assert flatten_line_breaks("word  next") == "word  next"


def test_leading_and_trailing_line_breaks_do_not_emit_spaces() -> None:
    assert flatten_line_breaks("\n  word\n") == "word"


def test_unrelated_leading_and_trailing_spaces_remain_literal() -> None:
    assert flatten_line_breaks(" leading\nnext ") == " leading next "
