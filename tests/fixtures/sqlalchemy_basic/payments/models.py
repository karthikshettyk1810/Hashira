from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String(100))


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    status = Column(String(20))
    account_id = Column(Integer, ForeignKey("accounts.id"))
