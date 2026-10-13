"""``python -m ai_civ.web`` - the Mini App backend. Listens on WEB_HOST:WEB_PORT (127.0.0.1:8080 by default); a tunnel
(Cloudflare / ngrok) gives it a public HTTPS address, which is what @BotFather's Web App URL must point to."""

from __future__ import annotations

import logging

import uvicorn

from ai_civ.config import get_settings
from ai_civ.web.app import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    app = create_app(settings)  # fail-fast on a missing token before the port is opened
    uvicorn.run(
        app,
        host=settings.web_host,
        port=settings.web_port,
        proxy_headers=True,  # behind the tunnel: trust X-Forwarded-* from this machine only
        forwarded_allow_ips="127.0.0.1",
        log_level="info",
    )


if __name__ == "__main__":
    main()
