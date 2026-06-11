from dblect import ModelContract
from dblect.demo import Money


class StgPayments(ModelContract):
    # The staging layer is typed as multi-currency money; nobody added a contract
    # to the customer rollup downstream.
    dbt_model = "stg_payments"
    amount: Money.columns(amount="amount", currency="currency")
