from copperchat.errors import CopperChatError


class KicadUnavailable(CopperChatError):
    """The api-server failed to start, crashed, or was restarted after a hang."""

    code = "kicad_unavailable"
    http_status = 503


class KicadCallTimeout(KicadUnavailable):
    code = "kicad_timeout"
