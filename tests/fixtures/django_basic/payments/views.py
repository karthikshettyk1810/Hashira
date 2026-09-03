from django.http import JsonResponse
from django.views import View

from .services import PaymentService


class CheckoutView(View):
    def post(self, request):
        service = PaymentService()
        result = service.process(request.POST.get("amount"))
        return JsonResponse({"status": result})
