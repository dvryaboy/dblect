from dblect import ModelContract, contract
from dblect.demo import Money


class StgPayments(ModelContract):
    dbt_model = "stg_payments"
    amount: Money.columns(amount="amount", currency="currency")

    @contract
    def one_currency_per_order(self):
        # Every payment on an order shares the order's currency, so summing an
        # order's payments is well defined. dblect trusts this and discharges the
        # per-order rollup.
        return self.order_id.determines(self.currency)
