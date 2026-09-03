from fastapi import FastAPI

from payments.routers import router

app = FastAPI()
app.include_router(router, prefix="/payments")
