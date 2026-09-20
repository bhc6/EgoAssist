from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from egoassist.realtime import SkillRuntime, SkillToolExecutor


@dataclass
class IdentityEndpoints:
    AUTH_COOKIE: Any
    active_skill: Any
    identity: Any
    invalidate_live_sessions: Any
    registry: Any
    runtime_for_user: Any
    user_key: Any
    user_routing: Any
    user_runtimes: Any
    user_tools: Any
    web: Any

    def auth_response(self, user: dict, token: str, request):
        response = self.web.json_response({"user": user})
        response.set_cookie(
            self.AUTH_COOKIE,
            token,
            max_age=self.identity.login_days * 86400,
            httponly=True,
            secure=request.secure,
            samesite="Lax",
            path="/",
        )
        return response

    async def post_register(self, request):
        payload = await request.json()
        user, token = self.identity.register(
            payload.get("username", ""), payload.get("password", "")
        )
        self.runtime_for_user(user["id"])
        return self.auth_response(user, token, request)

    async def post_login(self, request):
        payload = await request.json()
        user, token = self.identity.login(
            payload.get("username", ""), payload.get("password", "")
        )
        self.runtime_for_user(user["id"])
        return self.auth_response(user, token, request)

    async def post_logout(self, request):
        user = request.get(self.user_key)
        if user is not None:
            await self.invalidate_live_sessions(user_id=user["id"])
        self.identity.logout(request.cookies.get(self.AUTH_COOKIE))
        response = self.web.json_response({"ok": True})
        response.del_cookie(self.AUTH_COOKIE, path="/")
        return response

    async def get_me(self, request):
        return self.web.json_response({"user": request.get(self.user_key)})

    async def get_sessions(self, request):
        return self.web.json_response(
            {
                "sessions": self.identity.list_context_sessions(
                    request[self.user_key]["id"]
                )
            }
        )

    async def post_session(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json() if request.can_read_body else {}
        label = str(payload.get("label", "")).strip()
        self.user_runtimes[user_id] = SkillRuntime(self.registry, self.active_skill)
        self.user_routing[user_id] = self.registry.route("")
        self.user_tools[user_id] = SkillToolExecutor()
        session_id = self.identity.claim_context_session(user_id, label=label)
        return self.web.json_response(
            {"session": self.identity.get_context_session(user_id, session_id)},
            status=201,
        )
