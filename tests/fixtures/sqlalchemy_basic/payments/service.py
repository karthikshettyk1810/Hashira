from .models import Payment
from .repository import PaymentRepository


class PaymentService:
    def process(self, amount):
        payment = Payment()
        payment.status = "pending"
        if payment.status == "pending":
            payment.status = "captured"
        repo = PaymentRepository()
        repo.save(payment)
        return payment.status
