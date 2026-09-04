from .db_models import Payment


class PaymentService:
    def process(self, amount):
        payment = Payment()
        payment.status = "pending"
        if payment.status == "pending":
            payment.status = "captured"
        return payment.status

    def mark_refunded(self, payment: Payment) -> str:
        payment.status = "refunded"
        return payment.status
