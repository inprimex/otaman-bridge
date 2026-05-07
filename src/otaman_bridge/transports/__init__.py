"""Transport implementations.

**All transport-specific imports must live inside files in this package.**
``scripts/check_transport_boundary.py`` (and the matching pytest) fail the
build if ``telegram`` / ``slack_sdk`` / ``discord`` / ``matrix-nio`` are
imported anywhere else.

Built-in transports register themselves on import. To enable a transport,
import its module (typically done by the daemon based on
``accounts.<name>.transport`` config).
"""

from otaman_bridge.transports.null import NullTransport  # noqa: F401

# TelegramTransport import is soft — if python-telegram-bot isn't installed,
# the module raises ImportError only at constructor time, not import time.
# This lets maestro bridge run --transport null work without the dep.
try:
    from otaman_bridge.transports.telegram import TelegramTransport  # noqa: F401
    _HAS_TELEGRAM = True
except ImportError:  # pragma: no cover
    _HAS_TELEGRAM = False

__all__ = ["NullTransport"]
if _HAS_TELEGRAM:
    __all__.append("TelegramTransport")
