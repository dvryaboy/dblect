from dblect import ModelContract
from dblect.demo import Money


class StgPayments(ModelContract):
    # Payments carry their own currency, left open here (the project is genuinely
    # multi-currency). Typing the staging layer is all the team did.
    dbt_model = "stg_payments"
    amount: Money.columns(amount="amount", currency="currency")
