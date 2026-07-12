"""What gets SAID versus what gets shown.

The whole spoken/written contract is a convention — "first paragraph is spoken" —
enforced by one function. If it breaks, Snappy reads bullet points and markdown
asterisks out loud.
"""

from assistant import spoken_part, _sources_from


class Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_only_the_first_paragraph_is_spoken():
    answer = (
        "About two percent. Five shares would run you roughly two thousand dollars.\n\n"
        "**Your portfolio:** $100,000 total, all cash\n"
        "- SPCX last price: $145.42\n"
        "- 5 shares = $727.10"
    )
    said = spoken_part(answer)

    assert said == (
        "About two percent. Five shares would run you roughly two thousand dollars."
    )
    # The detail must NOT be read aloud — a speech synthesiser says "asterisk".
    assert "**" not in said
    assert "$" not in said
    assert "-" not in said


def test_answer_with_no_detail_still_speaks():
    assert spoken_part("You have about a hundred thousand in cash.") == (
        "You have about a hundred thousand in cash."
    )


def test_empty_answer_does_not_crash():
    assert spoken_part("") == ""


def test_sources_are_pulled_from_search_results():
    content = [
        Block(type="text", text="..."),
        Block(type="web_search_tool_result", content=[
            Block(url="https://nasdaqprivatemarket.com/spacex", title="SpaceX"),
            Block(url="https://hiive.com/spacex", title="Hiive"),
        ]),
    ]
    assert [s["url"] for s in _sources_from(content)] == [
        "https://nasdaqprivatemarket.com/spacex",
        "https://hiive.com/spacex",
    ]


def test_no_search_means_no_sources():
    content = [Block(type="text", text="You own no Apple.")]
    assert _sources_from(content) == []


def test_malformed_search_result_is_skipped_not_fatal():
    """A result block without a url shouldn't take down the answer."""
    content = [Block(type="web_search_tool_result", content=[Block(title="no url here")])]
    assert _sources_from(content) == []
