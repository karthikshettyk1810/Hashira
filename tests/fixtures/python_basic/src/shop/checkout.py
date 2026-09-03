from .notifications import send_receipt
from .payments import PaymentService


class CheckoutService:
    def checkout(self, order):
        payment = PaymentService()
        result = payment.process(order.total)

        if result.success:
            send_receipt(order)

        return result
