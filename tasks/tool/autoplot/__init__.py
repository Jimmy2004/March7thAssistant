"""Cloud dialogue automation, without desktop GUI dependencies."""
from ._config import EngineState

__all__ = ['EngineState', 'TerminalAutoplot']


def __getattr__(name):
    if name == 'TerminalAutoplot':
        from .terminal import TerminalAutoplot
        return TerminalAutoplot
    raise AttributeError(name)
