"""Human-timing distributions — bounds, persona range, chunk invariants."""

from __future__ import annotations

import random

from pikvm_agent.pikvm import timing


def _rng(seed: int) -> random.Random:
    return random.Random(seed)


def test_all_delays_stay_within_bounds() -> None:
    for seed in range(200):
        r = random.Random(seed)
        assert 22 <= timing.key_hold_ms(r) <= 130
        assert 55 <= timing.click_settle_ms(r) <= 260
        assert 35 <= timing.click_hold_ms(r) <= 140
        assert 35 <= timing.press_dwell_ms(r) <= 140
        assert 16 <= timing.chord_stagger_ms(r) <= 110
        assert 40 <= timing.chord_hold_ms(r) <= 150
        assert 0.10 <= timing.reaction_s(r) <= 0.55


def test_base_gap_is_a_plausible_typing_speed() -> None:
    # ~48-100 WPM (deliberately careful) -> per-char gap of roughly 120-250 ms.
    for seed in range(100):
        g = timing.base_gap_ms(random.Random(seed))
        assert 118 <= g <= 252


def test_inter_key_gap_skews_positive_and_pauses_at_boundaries() -> None:
    base = 70.0
    # A space sometimes triggers a long think-pause; over many draws the max after a
    # space should exceed the max after a plain letter.
    space_max = max(timing.inter_key_gap_ms("o", " ", base, random.Random(s)) for s in range(400))
    letter_max = max(timing.inter_key_gap_ms("o", "k", base, random.Random(s)) for s in range(400))
    assert space_max > letter_max
    # Repeated alpha key is slower on average than a normal transition.
    rep = sum(timing.inter_key_gap_ms("l", "l", base, random.Random(s)) for s in range(300)) / 300
    norm = sum(timing.inter_key_gap_ms("a", "k", base, random.Random(s)) for s in range(300)) / 300
    assert rep > norm


def test_word_chunks_join_invariant_and_bursts() -> None:
    text = "The quick brown fox jumps over the lazy dog and then keeps on typing a while."
    chunks = timing.word_chunks(text, target=20, rng=_rng(1))
    assert "".join(chunks) == text          # lossless
    assert len(chunks) > 1                    # actually split into bursts
    assert all(len(c) < 60 for c in chunks)   # none absurdly long


def test_word_chunks_short_text_is_one_piece() -> None:
    assert timing.word_chunks("hello", target=42) == ["hello"]
    assert timing.word_chunks("", target=42) == []
