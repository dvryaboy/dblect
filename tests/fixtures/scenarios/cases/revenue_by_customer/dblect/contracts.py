from dblect import ModelContract, contract
from dblect.demo import Money


class StgPayments(ModelContract):
    dbt_model = "stg_payments"
    value: Money.columns(amount="amount", currency="currency")

    @contract
    def one_currency_per_order(self):
        # The same per-order fix that discharges the order rollup. It holds the
        # currency constant within an order, not within a customer: a customer
        # spans many orders that can be in different currencies, so it does not
        # discharge this wider sum.
        return self.order_id.determines(self.currency)
