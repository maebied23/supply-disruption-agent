"""Grounding: a claim's quantity and date must be readable from its quoted evidence."""

from datetime import date

import pytest

from sdra.intake.grounding import check, dates_in, numbers_in

AROUND = date(2026, 11, 3)


@pytest.mark.parametrize("text, expected", [
    ("The remaining 200 units will follow on 20 December.", date(2026, 12, 20)),
    ("delivery on December 20th, 2026", date(2026, 12, 20)),
    ("arrives the 5th of Jan", date(2027, 1, 5)),           # no year: next occurrence
    ("new ETA 2026-12-01", date(2026, 12, 1)),
    ("ETA 25/12/2026", date(2026, 12, 25)),                 # only one reading is a real date
    ("سيتم التسليم في ٢٠ ديسمبر", date(2026, 12, 20)),        # Arabic month, Arabic-Indic digits
    ("الشحنة في 7 كانون الثاني 2027", date(2027, 1, 7)),       # Levantine month name
])
def test_dates_are_read(text, expected):
    sure, _ = dates_in(text, AROUND)
    assert expected in sure


def test_numeric_date_with_two_readings_is_ambiguous():
    assert check(["now arriving 03/12"], None, date(2026, 12, 3), AROUND) == [
        "date 2026-12-03 is one reading of an ambiguous date"]
    assert check(["now arriving 03/12"], None, date(2027, 3, 12), AROUND) == [
        "date 2027-03-12 is one reading of an ambiguous date"]


def test_relative_dates_are_not_grounded():
    assert check(["the rest will follow two weeks later"], None, date(2026, 11, 24), AROUND) == [
        "date 2026-11-24 not in the quoted text"]


def test_misread_values_are_caught():
    quote = ["we can only deliver 300 of the 500 units on 10 November"]
    assert check(quote, 300, date(2026, 11, 10), AROUND) == []
    assert check(quote, 200, None, AROUND) == ["quantity 200 not in the quoted text"]
    assert check(quote, None, date(2026, 11, 1), AROUND) == ["date 2026-11-01 not in the quoted text"]


def test_numbers_ignore_iso_dates_and_read_separators():
    assert numbers_in("1,200 units on 2026-11-10, ref ١٥٠") == {1200, 150}
