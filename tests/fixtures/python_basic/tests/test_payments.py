from shop.payments import PaymentService


class TestPaymentService:
    def test_process_success(self):
        service = PaymentService()
        result = service.process(10)
        assert result.success
