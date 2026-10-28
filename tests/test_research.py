"""Research section: report labels read as words, the wire folds a headline filed twice and never shows
feed entities. (Today's report excerpt is tested in test_overview.py.)"""

from albatross.agents import reports
from albatross.api.app import fold_duplicates
from albatross.ingest.news import _clean, _title


def test_report_labels_are_words():
    assert reports.words("pullback_in_uptrend") == "pullback in uptrend"
    assert reports.words("ltf_analyst") == "LTF analyst"
    assert reports.words("quant") == "quant"
    assert reports.words(None) is None
    # free-form ids (a caller's handle) stay exactly as logged
    assert reports.words("feed:@some_caller") == "feed:@some_caller"


def test_prompt_asks_for_plain_words():
    assert "never snake_case" in reports.SYSTEM


def row(id_, title, source, url):
    return {"id": id_, "title": title, "source": source, "url": url, "classification": None}


def test_wire_folds_a_headline_filed_twice():
    rows = [row(3, "Now Accepting Bitcoin: Café Hub", "bitcoinmagazine", "https://b/culture/x"),
            row(2, "Now accepting bitcoin — café hub!", "decrypt", "https://d/x"),
            row(1, "Now Accepting Bitcoin: Café Hub", "bitcoinmagazine", "https://b/news/x"),
            row(0, "Something else", "coindesk", "https://c/y")]
    out = fold_duplicates(rows)
    assert [r["id"] for r in out] == [3, 0]
    assert out[0]["also"] == [{"source": "decrypt", "url": "https://d/x"},
                              {"source": "bitcoinmagazine", "url": "https://b/news/x"}]
    assert out[1]["also"] == []


def test_titles_lose_feed_entities():
    assert _title(" Cboe's New S&amp;P Deal ") == "Cboe's New S&P Deal"
    assert fold_duplicates([row(1, "S&amp;P deal", "decrypt", "https://d/z")])[0]["title"] == "S&P deal"
    assert _clean("<p>Fees &#8217;up&#8217;&nbsp;3x</p>") == "Fees ’up’ 3x"
