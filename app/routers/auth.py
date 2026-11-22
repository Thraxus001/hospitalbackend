from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import current_user
from ..models import User
from ..schemas import LoginIn, RegisterIn, TokenOut, UserOut
from ..security import create_token, hash_password, verify_password
from ..services.mpesa import normalize_phone

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenOut, status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    phone = normalize_phone(body.phone)
    if db.query(User).filter((User.phone == phone) | (User.email == body.email if body.email else False)).first():
        raise HTTPException(409, "Phone or email already registered")
    user = User(full_name=body.full_name, phone=phone, email=body.email, hashed_password=hash_password(body.password))
    db.add(user)
    db.commit()
    return TokenOut(access_token=create_token(user.id))


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.phone == normalize_phone(body.phone)).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(401, "Incorrect phone or password")
    return TokenOut(access_token=create_token(user.id))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user
