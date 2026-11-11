"""AVRONE: a self-hosted, multi-agent chat and search system."""
__version__ = "0.153.0"

from . import logsafe as _logsafe  # noqa: E402

_logsafe.install()  # round 150: outside text in log arguments can't forge log lines
