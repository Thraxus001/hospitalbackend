import base64
import re
from datetime import datetime

import httpx
from fastapi import HTTPException

from ..config import settings
from .slots import TZ

BASE = {"sandbox": "https://sandbox.safaricom.co.ke", "production": "https://api.safaricom.co.ke"}


def normalize_phone(raw: str) -> str:
    d = re.sub(r"\D", "", raw)
    if d.startswith("0"):
        d = "254" + d[1:]
    elif len(d) == 9:
        d = "254" + d
    if not re.fullmatch(r"254[17]\d{8}", d):
        raise HTTPException(422, "Invalid Kenyan phone number")
    return d


def stk_push(phone: str, amount: int, reference: str, description: str) -> str:
    """Send an M-Pesa STK prompt. Returns the CheckoutRequestID."""
    base = BASE[settings.mpesa_env]
    with httpx.Client(timeout=20) as client:
        tok = client.get(
            f"{base}/oauth/v1/generate?grant_type=client_credentials",
            auth=(settings.mpesa_consumer_key, settings.mpesa_consumer_secret),
        )
        if tok.status_code != 200:
            raise HTTPException(502, "M-Pesa authentication failed")
        ts = datetime.now(TZ).strftime("%Y%m%d%H%M%S")
        password = base64.b64encode(f"{settings.mpesa_shortcode}{settings.mpesa_passkey}{ts}".encode()).decode()
        res = client.post(
            f"{base}/mpesa/stkpush/v1/processrequest",
            headers={"Authorization": f"Bearer {tok.json()['access_token']}"},
            json={
                "BusinessShortCode": settings.mpesa_shortcode,
                "Password": password,
                "Timestamp": ts,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": amount,
                "PartyA": phone,
                "PartyB": settings.mpesa_shortcode,
                "PhoneNumber": phone,
                "CallBackURL": settings.mpesa_callback_url,
                "AccountReference": reference,
                "TransactionDesc": description,
            },
        )
    data = res.json()
    if res.status_code != 200 or data.get("ResponseCode") != "0":
        raise HTTPException(502, data.get("errorMessage") or data.get("ResponseDescription") or "STK push failed")
    return data["CheckoutRequestID"]
