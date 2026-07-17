"""Keep API response protections when requests are forwarded by the web server."""

import os


class SecurityHeaders:
    def __init__(self, app):
        self.app = app
        self.headers = {
            b"content-security-policy": b"frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
            b"permissions-policy": b"camera=(), microphone=(), geolocation=(), payment=(), usb=()",
            b"referrer-policy": b"strict-origin-when-cross-origin",
            b"x-content-type-options": b"nosniff",
            b"x-frame-options": b"DENY",
        }
        if os.getenv("ALPHADECK_SECURITY_HSTS", "").strip().lower() in {"1", "true", "yes", "on"}:
            self.headers[b"strict-transport-security"] = b"max-age=31536000; includeSubDomains"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def protected(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {name.lower() for name, _ in headers}
                message = {
                    **message,
                    "headers": [
                        *headers,
                        *(
                            (name, value)
                            for name, value in self.headers.items()
                            if name not in existing
                        ),
                    ],
                }
            await send(message)

        await self.app(scope, receive, protected)
