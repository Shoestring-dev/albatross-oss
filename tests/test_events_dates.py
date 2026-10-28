"""albatross/events/dates.py: the date words news uses, read the same way every time."""
from datetime import UTC, date, datetime

from albatross.events import dates

TUE = datetime(2026, 9, 29, 10, 46, tzinfo=UTC)           # a Tuesday, mid-morning UTC


def spans(words: str, published: datetime = TUE) -> list[tuple[str, str, str]]:
    return [(str(p.start), str(p.last), p.precision) for p in dates.read(words, published)]


def test_absolute_days():
    assert spans("Oct. 6") == [("2026-10-06", "2026-10-06", "day")]
    assert spans("October 7th, 2026") == spans("7 October") == spans("2026-10-07") == \
        [("2026-10-07", "2026-10-07", "day")]
    assert spans("tokens issued from Jan. 1, 2027") == [("2027-01-01", "2027-01-01", "day")]
    assert spans("Oct. 7-9") == [("2026-10-07", "2026-10-09", "day")]          # a conference's days
    assert spans("apply October 5 – but even Bitcoin") == [("2026-10-05", "2026-10-05", "day")]
    assert spans("Feb 30") == []


def test_a_yearless_date_is_the_first_one_from_a_week_back():
    assert spans("Sept. 25") == [("2026-09-25", "2026-09-25", "day")]         # last week, not next year
    assert spans("Sept. 20") == [("2027-09-20", "2027-09-20", "day")]         # 9 days back: next year's
    assert spans("in March") == [("2027-03-01", "2027-03-31", "month")]
    assert spans("Dec. 30", datetime(2027, 1, 3, tzinfo=UTC)) == [("2026-12-30", "2026-12-30", "day")]
    assert spans("Q3") == [("2026-07-01", "2026-09-30", "quarter")]            # still this quarter


def test_weekdays_can_mean_the_last_six_days_the_day_itself_or_the_next_week():
    assert spans("Tuesday") == [("2026-09-29", "2026-09-29", "day"), ("2026-10-06", "2026-10-06", "day")]
    assert spans("ahead of Wednesday's PCE report") == [("2026-09-23", "2026-09-23", "day"),
                                                        ("2026-09-30", "2026-09-30", "day")]
    assert spans("next Tuesday") == [("2026-10-06", "2026-10-06", "day"), ("2026-10-13", "2026-10-13", "day")]
    assert spans("last Friday") == [("2026-09-25", "2026-09-25", "day")]


def test_relative_words_count_from_the_new_york_day_too():
    late_us_evening = datetime(2026, 9, 30, 2, tzinfo=UTC)                 # 22:00 on the 29th in New York
    assert [p.start for p in dates.read("tomorrow", late_us_evening)] == [date(2026, 9, 30), date(2026, 10, 1)]
    assert [p.start for p in dates.read("tomorrow", TUE)] == [date(2026, 9, 30)]
    assert [p.start for p in dates.read("yesterday", TUE)] == [date(2026, 9, 28)]
    assert [p.start for p in dates.read("later today", TUE)] == [date(2026, 9, 29)]


def test_weeks_months_quarters():
    assert spans("next week") == [("2026-10-05", "2026-10-11", "week")]
    assert spans("later this week") == [("2026-09-28", "2026-10-04", "week")]
    assert spans("in two weeks") == [("2026-10-12", "2026-10-18", "week")]
    assert spans("in 3 days") == [("2026-10-02", "2026-10-02", "day")]
    assert spans("later this month") == [("2026-09-01", "2026-09-30", "month")]
    assert spans("next month") == [("2026-10-01", "2026-10-31", "month")]
    assert spans("next month", datetime(2026, 12, 5, tzinfo=UTC)) == [("2027-01-01", "2027-01-31", "month")]
    assert spans("mid-October") == spans("October 2026") == [("2026-10-01", "2026-10-31", "month")]
    assert spans("fourth quarter 2026") == spans("Q4") == [("2026-10-01", "2026-12-31", "quarter")]


def test_may_the_verb_is_not_a_month():
    assert spans("the token may launch") == []
    assert spans("in May") == [("2027-05-01", "2027-05-31", "month")]
    assert not dates.mentions_date("Bitcoin falls to $83,000 as the token may rally")
    assert dates.mentions_date("Ethereum schedules Glamsterdam upgrade on Sepolia for Oct. 6")


def test_words_it_does_not_know_read_as_nothing():
    assert spans("soon") == spans("by year-end") == spans("H1 2027") == []


def test_period_of_a_model_date():
    assert dates.period_of(date(2026, 10, 15), "month") == dates.Period(date(2026, 10, 1), date(2026, 10, 31), "month")
    assert dates.period_of(date(2026, 10, 15), "week").start == date(2026, 10, 12)
    assert dates.period_of(date(2026, 11, 2), "quarter").start == date(2026, 10, 1)
