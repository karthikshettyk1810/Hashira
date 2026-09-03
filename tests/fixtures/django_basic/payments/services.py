from .models import Payment


class PaymentService:
    def process(self, amount):
        payment = Payment()
        payment.status = "pending"
        if payment.status == "pending":
            payment.status = "captured"
        return payment.status
