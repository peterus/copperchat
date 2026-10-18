from copperchat.errors import CopperChatError


class KicadUnavailable(CopperChatError):
    """The api-server failed to start, crashed, or was restarted after a hang."""

    code = "kicad_unavailable"
    http_status = 503


class KicadCallTimeout(KicadUnavailable):
    code = "kicad_timeout"


class LibrariesLoading(CopperChatError):
    """The api-server's symbol libraries did not finish loading in time."""

    code = "libraries_loading"
    http_status = 503
