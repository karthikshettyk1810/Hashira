from sqlalchemy import text


def count_pending(session):
    return session.execute(
        text("SELECT count(*) FROM payments WHERE status = 'pending'")
    ).scalar()
