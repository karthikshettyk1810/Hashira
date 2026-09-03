"""Data entities. No ORM here on purpose -- the Python adapter must not
need Django to understand plain classes; a future DjangoAdapter enricher
is what would recognize `models.Model` as something more specific."""


class Order:
    def __init__(self, total):
        self.total = total


class Payment:
    def __init__(self, amount, status):
        self.amount = amount
        self.status = status
