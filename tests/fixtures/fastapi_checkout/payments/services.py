class PaymentService:
    def process(self, amount):
        payment_status = "pending"
        if payment_status == "pending":
            payment_status = "captured"
        return payment_status
