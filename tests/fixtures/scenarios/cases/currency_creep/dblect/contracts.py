from dblect import ModelContract
from dblect.demo import Currency, Money


class RawPayments(ModelContract):
    # Payments now arrive in several currencies; the source records which.
    dbt_model = "raw_payments"
    value: Money.columns(amount="amount", currency="currency")


class StgPayments(ModelContract):
    # This contract was written a year ago, when every payment was in USD, and
    # nobody revisited it when the source went international.
    dbt_model = "stg_payments"
    amount: Money.refine(currency=Currency.USD)
