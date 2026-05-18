from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import verify_password
from app.models.user import User


def authenticate_admin(db: Session, email_or_username: str, password: str) -> User | None:
    user = db.scalar(
        select(User).where((User.email == email_or_username) | (User.username == email_or_username))
    )
    if user is None or not user.is_active or user.role != "admin":
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user
