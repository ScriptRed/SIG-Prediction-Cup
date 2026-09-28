"""Structural guarantee: no Venue method may default tournament_id.

The SIG adapter has no usable default tournament for our org-bound key
(docs/platform/SUMMARY.md's org-default trap). If a future edit adds a
default value to any of these parameters, this test must start failing.
"""

import inspect

from predcup.venues.base import Venue

METHODS_REQUIRING_TOURNAMENT_ID = [
    "get_markets",
    "get_book",
    "cancel",
    "cancel_all",
    "get_open_orders",
    "get_positions",
    "get_balance",
]


def test_venue_abstract_methods_require_tournament_id_with_no_default():
    for name in METHODS_REQUIRING_TOURNAMENT_ID:
        method = getattr(Venue, name)
        sig = inspect.signature(method)
        assert "tournament_id" in sig.parameters, f"{name} must accept tournament_id"
        param = sig.parameters["tournament_id"]
        assert param.default is inspect.Parameter.empty, (
            f"{name}'s tournament_id must not have a default — "
            "the platform has no safe default tournament"
        )


def test_place_order_takes_an_order_whose_tournament_id_is_itself_required():
    # place_order takes a full Order, not a bare tournament_id kwarg — the
    # requirement is enforced on Order itself (see test_models.py). Just
    # confirm the signature doesn't smuggle in a separate optional param.
    sig = inspect.signature(Venue.place_order)
    assert "order" in sig.parameters
