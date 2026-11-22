"""Pesapal API 3.0 hosted checkout (cards + bank/mobile options enabled on your merchant account).

The patient is redirected to Pesapal's page, so card details never touch our servers. IPN calls are
unauthenticated, so we never trust them: every result is confirmed by asking Pesapal directly.
"""
import time
from decimal import Decimal

import httpx
from fastapi import HTTPException

from ..config import settings

BASE = {"sandbox": "https://cybqa.pesapal.com/pesapalv3/api", "production": "https://pay.pesapal.com/v3/api"}
_cache: dict = {"token": None, "exp": 0.0, "ipn_id": settings.pesapal_ipn_id or None}


def _url(path: str) -> str:
    return f"{BASE[settings.pesapal_env]}{path}"


def _headers(client: httpx.Client) -> dict:
    if not _cache["token"] or time.time() > _cache["exp"]:
        r = client.post(_url("/Auth/RequestToken"), json={
            "consumer_key": settings.pesapal_consumer_key, "consumer_secret": settings.pesapal_consumer_secret})
        data = r.json() if r.content else {}
        if r.status_code != 200 or not data.get("token"):
            raise HTTPException(502, "Card payments are temporarily unavailable")
        _cache["token"], _cache["exp"] = data["token"], time.time() + 240  # tokens live ~5 min
    return {"Authorization": f"Bearer {_cache['token']}", "Accept": "application/json"}


def _ipn_id(client: httpx.Client, headers: dict) -> str:
    if not _cache["ipn_id"]:
        r = client.post(_url("/URLSetup/RegisterIPN"), headers=headers, json={
            "url": f"{settings.public_base_url}/api/payments/pesapal/ipn", "ipn_notification_type": "GET"})
        if r.status_code != 200 or not r.json().get("ipn_id"):
            raise HTTPException(502, "Could not register payment notifications")
        _cache["ipn_id"] = r.json()["ipn_id"]
    return _cache["ipn_id"]


def submit_order(reference: str, amount: Decimal, description: str, full_name: str, email: str | None, phone: str) -> tuple[str, str]:
    """Returns (order_tracking_id, redirect_url)."""
    first, _, last = full_name.partition(" ")
    with httpx.Client(timeout=25) as client:
        h = _headers(client)
        r = client.post(_url("/Transactions/SubmitOrderRequest"), headers=h, json={
            "id": reference, "currency": "KES", "amount": float(amount), "description": description[:100],
            "callback_url": f"{settings.public_base_url}/api/payments/pesapal/return",
            "notification_id": _ipn_id(client, h),
            "billing_address": {"email_address": email or "", "phone_number": phone, "country_code": "KE",
                                "first_name": first or full_name, "last_name": last},
        })
    data = r.json() if r.content else {}
    if r.status_code != 200 or not data.get("redirect_url"):
        raise HTTPException(502, (data.get("error") or {}).get("message") or "Could not start card payment")
    return data["order_tracking_id"], data["redirect_url"]


def get_status(order_tracking_id: str) -> dict:
    with httpx.Client(timeout=25) as client:
        r = client.get(_url("/Transactions/GetTransactionStatus"), headers=_headers(client),
                       params={"orderTrackingId": order_tracking_id})
    if r.status_code != 200:
        raise HTTPException(502, "Could not verify payment status")
    return r.json()
