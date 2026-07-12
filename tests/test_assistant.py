"""The headline versus the detail.

The panel renders the first paragraph large and the rest as supporting detail. That
split is a convention enforced by one function, and it's what keeps the answer
readable at a glance instead of a wall of bullets.
"""

from assistant import headline, _sources_from


class Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_only_the_first_paragraph_is_the_headline():
    answer = (
        "About two percent. Five shares would run you roughly two thousand dollars.\n\n"
        "**Your portfolio:** $100,000 total, all cash\n"
        "- SPCX last price: $145.42\n"
        "- 5 shares = $727.10"
    )
    lead = headline(answer)

    assert lead == (
        "About two percent. Five shares would run you roughly two thousand dollars."
    )
    # The headline is prose. Markdown and bullets belong in the detail below it.
    assert "**" not in lead
    assert "-" not in lead


def test_answer_with_no_detail_is_all_headline():
    assert headline("You have about a hundred thousand in cash.") == (
        "You have about a hundred thousand in cash."
    )


def test_empty_answer_does_not_crash():
    assert headline("") == ""


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
