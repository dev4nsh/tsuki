import base64
import os
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from uuid import uuid4

import fastapi
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from jose import jwt
from passlib.context import CryptContext
from pydantic import BaseModel

from tsuki.config import secrets
from tsuki.routers.database import *
from tsuki.routers.models import User


auth = APIRouter(prefix="/auth", tags=["Authorization"])
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(parent_dir, "templates"))
password_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


class Login(BaseModel):
    username: str
    password: str


def create_access_token(username: str):
    token = jwt.encode(
        {"user": username, "iat": datetime.now(), "iss": secrets.ISSUER},
        secrets.SECRET_KEY,
        algorithm="HS256",
    )
    return token


async def create_verification_id(username: str) -> str:
    token = jwt.encode(
        {
            "user": username,
            "iat": datetime.now(),
            "exp": datetime.now() + timedelta(days=2),
            "iss": secrets.ISSUER,
        },
        secrets.SECRET_KEY,
        algorithm="HS256",
    )
    _id = uuid4().hex
    # Create a short url to mask JWT
    await create_short_url(token, _id)
    return _id


async def get_current_user(request: fastapi.Request) -> User | None:
    try:
        token: str = request.session["Authorization"]
        payload = jwt.decode(token, secrets.SECRET_KEY, algorithms=["HS256"])
        username: str = payload.get("user")
        if not username:
            return None
    except:
        return None
    user = await read_user(username)
    if not user:
        return None
    return user


@auth.post("/signup", response_class=HTMLResponse)
async def signup(request: fastapi.Request):
    form = await request.form()
    user = User(**form)
    user_data = await read_user(user.username)
    if user_data:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error": "403 Forbidden",
                "message": "Account already exists with the username/email.",
            },
        )
    # Hash the user's password
    user.password = password_ctx.hash(user.password)
    result = await create_user(user)
    if not result:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error": "400 Bad Request",
                "message": "Unable to create user, please try again later.",
            },
        )
    return await send_verification_mail(request, user, "Account created successfully.")


@auth.post("/login", response_class=HTMLResponse)
async def login(request: fastapi.Request):
    form = await request.form()
    user = Login(**form)
    user_data = await read_user(user.username)
    if not user_data:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error": "401 Unauthorized",
                "message": "User does not exist.",
            },
        )
    if not password_ctx.verify(user.password, user_data.password):
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error": "401 Unauthorized",
                "message": "Incorrect password.",
            },
        )
    access_token = create_access_token(user.username)
    request.session["Authorization"] = access_token
    return templates.TemplateResponse(
        "response.html", {"request": request, "message": "Logged in successfully."}
    )


@auth.get("/verify", response_class=HTMLResponse)
async def send_verification_mail(
    request: fastapi.Request,
    user: User = Depends(get_current_user),
    custom_message: str = "",
):
    if not user:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error": "401 Unauthorized",
                "message": "User not logged in.",
            },
        )
    if user.verified:
        return templates.TemplateResponse(
            "verification.html",
            {"request": request, "message": "Account already verified."},
        )
    credentials = Credentials.from_authorized_user_info(
        {
            "token": secrets.TOKEN,
            "refresh_token": secrets.REFRESH_TOKEN,
            "token_uri": secrets.TOKEN_URI,
            "client_id": secrets.CLIENT_ID,
            "client_secret": secrets.CLIENT_SECRET,
            "expiry": secrets.EXPIRY,
        },
        ["https://mail.google.com/"],
    )
    if not credentials.valid:
        credentials.refresh(Request())

    with open(os.path.join(parent_dir, "templates", "verify.html")) as infile:
        verification_template = infile.read()
    verification_id = await create_verification_id(user.username)
    message = MIMEText(
        verification_template.format(
            user.username,
            user.email,
            f"{request.base_url._url}auth/verify/{verification_id}",
        ),
        "html",
    )
    message["to"] = user.email
    message["from"] = f"Tsuki <{secrets.EMAIL}>"
    message["subject"] = "Verify your Tsuki account"
    message_encoded = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
    service = build("gmail", "v1", credentials=credentials)
    service.users().messages().send(userId="me", body=message_encoded).execute()
    return templates.TemplateResponse(
        "verification.html",
        {
            "request": request,
            "message": f"{custom_message} Verification mail sent to {user.email}",
        },
    )


@auth.get("/verify/{token_id}", response_class=HTMLResponse)
async def verify_user(request: fastapi.Request, token_id: str):
    credentials_error = templates.TemplateResponse(
        "verification.html",
        {"request": request, "message": "Could not validate credentials."},
    )
    try:
        token = await read_short_url(token_id)
        if not token:
            return credentials_error
        await delete_short_url(token_id)
        payload = jwt.decode(token, secrets.SECRET_KEY, algorithms=["HS256"])
        username: str = payload.get("user")
        if not username:
            return credentials_error
    except:
        return credentials_error
    user = await read_user(username)
    if user.verified:
        return templates.TemplateResponse(
            "verification.html",
            {"request": request, "message": "Account already verified."},
        )
    # Update user's verification status
    result = await update_user(username, {"verified": True})
    if not result:
        return credentials_error
    return templates.TemplateResponse(
        "verification.html",
        {"request": request, "message": "Account verified successfully."},
    )