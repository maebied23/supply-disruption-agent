from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from sdra.policy import Policy
from sdra.schema import Money

CENT = Decimal("0.01")


def to_base(m: Money, policy: Policy) -> Decimal:
    return m.amount * policy.fx_rates_to_base[m.currency]


def floor_cents(x: Decimal) -> Decimal:
    """Round toward minus infinity, so a limit is never loosened by rounding."""
    return x.quantize(CENT, rounding=ROUND_FLOOR)


def cents(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)
