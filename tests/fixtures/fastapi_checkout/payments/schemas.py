from pydantic import BaseModel


class PaymentRequest(BaseModel):
    amount: int


class PaymentResponse(BaseModel):
    status: str
