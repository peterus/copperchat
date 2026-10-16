from __future__ import annotations

import sys

import uvicorn

from copperchat.config import check_model_credentials, get_settings
from copperchat.errors import ConfigError
from copperchat.web.app import create_app


def main() -> None:
    settings = get_settings()
    try:
        check_model_credentials(settings)
    except ConfigError as exc:
        print(f"CopperChat cannot start: {exc.message}", file=sys.stderr)
        raise SystemExit(2) from None
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
