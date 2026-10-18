"""Golden checks: raw fixture -> expected canonical entry fields, per connector."""

from varyz.connectors.manual import ManualConnector
from varyz.domain import Kind, TimePrecision


def test_manual_when_year_precision():
    c = ManualConnector()
    item = ManualConnector.make_item("met Lena in Berlin", when="2019")
    norm = c.normalize(item)
    assert norm.kind == Kind.NOTE
    assert norm.occurred_start.year == 2019
    assert norm.time_precision == TimePrecision.YEAR
    assert norm.body == "met Lena in Berlin"


def test_a_note_you_typed_is_recorded_as_yours():
    """The flag was left at its default, so `varyz add` filed anonymous text.

    The Mirror's own-words queries -- goals, people, evidence selection -- all
    select on `authored_by_user = 1`, so a notebook was invisible to them.
    """
    norm = ManualConnector().normalize(ManualConnector.make_item("I moved to Berlin"))
    assert norm.authored_by_user is True


def test_manual_defaults_to_now():
    c = ManualConnector()
    norm = c.normalize(ManualConnector.make_item("quick thought"))
    assert norm.occurred_start is not None
    assert norm.time_precision == TimePrecision.EXACT
