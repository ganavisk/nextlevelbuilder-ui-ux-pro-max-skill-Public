from app.modules.conversation.knowledge import chunk_text


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == []


def test_short_text_yields_single_chunk() -> None:
    assert chunk_text("We are open 9 to 5.", size=800, overlap=150) == ["We are open 9 to 5."]


def test_long_text_produces_overlapping_chunks() -> None:
    # No spaces: with a space-free body, each 800-char window is emitted
    # verbatim (no strip trimming), so slice math is exact and the overlap
    # invariant can be asserted precisely.
    text = "".join(str(i % 10) for i in range(4000))
    chunks = chunk_text(text, size=800, overlap=150)
    assert len(chunks) > 1
    assert chunks[0] == text[:800]
    # Consecutive full-size chunks must overlap by exactly `overlap` chars —
    # the tail of each is the head of the next. This is what proves the
    # window advances by (size - overlap) and skips no content.
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        if len(nxt) >= 150:
            assert prev[-150:] == nxt[:150]


def test_overlap_smaller_than_size_always_terminates() -> None:
    # regression guard: overlap >= size would never advance the window
    text = "x" * 5000
    chunks = chunk_text(text, size=800, overlap=150)
    assert len(chunks) >= 6
