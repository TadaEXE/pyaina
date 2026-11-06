"""Triaina: functional block diagram language over balanced ternary signals
with a discrete tick scheduler."""

from .compiler import Program, Compiler, Diag
from .host import run_bench, run_tests

__all__ = ['Program', 'Compiler', 'Diag', 'run_bench', 'run_tests']
__version__ = '0.1.0'
