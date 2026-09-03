from fastapi import APIRouter, Depends

from .dependencies import get_current_user
from .schemas import PaymentRequest, PaymentResponse
from .services import PaymentService

router = APIRouter()


@router.post("/checkout/", response_model=PaymentResponse)
def checkout(body: PaymentRequest) -> PaymentResponse:
    service = PaymentService()
    result = service.process(body.amount)
    return PaymentResponse(status=result)


@router.get("/me")
def me(user=Depends(get_current_user)):
    return {"name": user.name}
