from .models import Payment


class PaymentRepository:
    def save(self, payment: Payment) -> Payment:
        return payment
