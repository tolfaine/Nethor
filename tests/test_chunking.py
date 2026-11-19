import random

from assistant.bot import FENCE, chunk_message


def fences_balanced(chunk: str) -> bool:
    return sum(1 for line in chunk.split("\n") if line.strip().startswith(FENCE)) % 2 == 0


def test_short_text_is_one_chunk() -> None:
    assert chunk_message("hello") == ["hello"]


def test_empty_text_gives_no_chunks() -> None:
    assert chunk_message("   \n ") == []


def test_long_plain_text_splits_on_lines_under_limit() -> None:
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    chunks = chunk_message(text)
    assert len(chunks) > 1
    assert all(len(c) <= 2000 for c in chunks)
    assert "\n".join(chunks) == text


def test_fence_cut_by_a_break_is_closed_and_reopened() -> None:
    code = "\n".join(f"print({i})  # " + "y" * 40 for i in range(120))
    text = f"Intro\n```python\n{code}\n```\nOutro"
    chunks = chunk_message(text)
    assert len(chunks) > 1
    assert all(len(c) <= 2000 and fences_balanced(c) for c in chunks)
    assert chunks[1].startswith("```python\n")
    assert chunks[0].endswith("\n```")


def test_single_huge_line_is_hard_split() -> None:
    chunks = chunk_message("z" * 5000)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == "z" * 5000


def test_random_mixed_content_respects_limit_and_fences() -> None:
    rng = random.Random(7)
    lines: list[str] = []
    for _ in range(600):
        roll = rng.random()
        lines.append("```js" if roll < 0.05 else "x" * rng.randint(0, 300))
    if sum(1 for line in lines if line.startswith(FENCE)) % 2:
        lines.append(FENCE)
    for chunk in chunk_message("\n".join(lines)):
        assert len(chunk) <= 2000
        assert fences_balanced(chunk)
