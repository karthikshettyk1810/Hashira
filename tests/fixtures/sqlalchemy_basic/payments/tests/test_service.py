from payments.service import PaymentService


class TestPaymentService:
    def test_process_marks_payment_captured(self):
        service = PaymentService()
        result = service.process(100)
        assert result == "captured"
