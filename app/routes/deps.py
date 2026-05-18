from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from app.models.user import User


def require_admin(request: Request, db: Session) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/admin/login"})
    user = db.get(User, user_id)
    if user is None or not user.is_active or user.role != "admin":
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/admin/login"})
    return user
