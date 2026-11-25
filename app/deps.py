import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .database import get_db
from .models import Doctor, User
from .security import decode_token

bearer = HTTPBearer(auto_error=False)


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)
) -> User:
    if not creds:
        raise HTTPException(401, "Not authenticated")
    try:
        user = db.get(User, decode_token(creds.credentials))
    except (jwt.PyJWTError, ValueError):
        raise HTTPException(401, "Invalid or expired token")
    if not user:
        raise HTTPException(401, "User not found")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admin only")
    return user


def current_doctor(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Doctor:
    if user.role != "doctor":
        raise HTTPException(403, "Doctor only")
    doctor = db.query(Doctor).filter(Doctor.full_name.ilike(user.full_name.strip())).first()
    if not doctor:
        raise HTTPException(404, "Doctor profile not found")
    return doctor
