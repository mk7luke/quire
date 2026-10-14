"""Imposition invariants. Run: python tests/test_impose.py"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pad_count, plan_book, waste_choice  # noqa: E402


def _seen(plan: dict) -> list[int]:
    nums = []
    for quire in plan["quires"]:
        for sheet in quire["sheets"]:
            nums.extend(sheet["front"])
            nums.extend(sheet["back"])
    return nums


def test_classic_eight() -> None:
    plan = plan_book(8, 8)
    sheets = plan["quires"][0]["sheets"]
    assert sheets[0]["front"] == (8, 1)
    assert sheets[0]["back"] == (2, 7)
    assert sheets[1]["front"] == (6, 3)
    assert sheets[1]["back"] == (4, 5)
    assert sheets[0]["role"] == "outside"
    assert sheets[1]["role"] == "center"


def test_four_and_twelve() -> None:
    four = plan_book(4, 4)["quires"][0]["sheets"][0]
    assert four["front"] == (4, 1)
    assert four["back"] == (2, 3)
    assert sorted(_seen(plan_book(12, 12))) == list(range(1, 13))


def test_each_page_once() -> None:
    for written, quire in ((1, 4), (6, 8), (10, 8), (16, 16), (17, 8), (64, 16)):
        plan = plan_book(written, quire)
        seen = _seen(plan)
        assert sorted(seen) == list(range(1, plan["padded"] + 1))
        assert len(seen) == len(set(seen))
        assert plan["pdf_pages"] == plan["padded"] // 2


def test_second_quire_is_local() -> None:
    plan = plan_book(10, 8)
    assert plan["padded"] == 16
    assert plan["quires"][1]["sheets"][0]["front"] == (16, 9)
    assert plan["quires"][1]["sheets"][0]["back"] == (10, 15)


def test_waste() -> None:
    assert pad_count(6, 8) == 8
    assert waste_choice(10, 8)[2] == (2, 4)
    assert waste_choice(8, 8)[2] is None


if __name__ == "__main__":
    test_classic_eight()
    test_four_and_twelve()
    test_each_page_once()
    test_second_quire_is_local()
    test_waste()
    print("impose ok")
