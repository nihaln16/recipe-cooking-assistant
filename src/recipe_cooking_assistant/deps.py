from __future__ import annotations

from uuid import uuid4

from fastapi import Request

from recipe_cooking_assistant.config import Settings
from recipe_cooking_assistant.db import Database


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_session_id(request: Request) -> str:
    session_id = request.session.get("session_id")
    if not session_id:
        session_id = str(uuid4())
        request.session["session_id"] = session_id
    return session_id
