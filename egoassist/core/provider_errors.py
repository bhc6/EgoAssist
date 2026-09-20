"""Safe provider failure metadata shared by transport and retry policies."""
import re


class ProviderRequestError(RuntimeError):
    def __init__(self, status, code=None, request_id=None):
        code = str(code or "Unknown")
        self.code = code if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code) else "Unknown"
        self.status = int(status)
        self.request_id = str(request_id)[:100] if request_id else None
        self.retryable = self.status in (408, 429) or self.status >= 500
        self.account_blocked = self.code.casefold() in {
            "arrearage", "accessdenied", "invalidapikey", "invalid_api_key",
            "unauthorized", "forbidden", "accountoverdue", "accountblocked",
        } or self.status in (401, 402, 403)
        if self.account_blocked:
            self.retryable = False
        message = ("Provider account billing is unavailable; resolve billing and reconnect."
                   if self.code.casefold() == "arrearage" else
                   "Provider access denied; check account billing, API key and model access, then reconnect."
                   if self.account_blocked else f"Provider request failed (HTTP {self.status}, {self.code}).")
        super().__init__(message)

    def public(self):
        return {"httpStatus": self.status, "code": self.code, "requestId": self.request_id,
                "retryable": self.retryable, "accountBlocked": self.account_blocked, "message": str(self)}


def access_failure(message):
    text = str(message).casefold()
    if "arrearage" in text or "overdue payment" in text:
        return ProviderRequestError(402, "Arrearage")
    if any(term in text for term in ("access denied", "accessdenied", "invalid api key", "invalidapikey", "unauthorized")):
        return ProviderRequestError(403, "AccessDenied")
    return None
