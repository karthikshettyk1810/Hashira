from .models import Payment
from .utils import is_positive


class PaymentResult:
    def __init__(self, success):
        self.success = success


class PaymentService:
    def process(self, amount):
        if self.validate(amount):
            payment = Payment(amount, "captured")
            return PaymentResult(True)
        return PaymentResult(False)

    def validate(self, amount):
        return is_positive(amount)
