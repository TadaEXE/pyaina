"""Triaina: declarative functional block-diagram language over balanced
ternary signals with a discrete tick scheduler."""

from .compiler import Program, Compiler, Diag
from .host import run_bench, run_tests, build_bench_rt, parse_keyboard_record
from .runtime import Runtime, Pacer, Record

__all__ = ['Program', 'Compiler', 'Diag', 'run_bench', 'run_tests',
           'build_bench_rt', 'parse_keyboard_record', 'Runtime', 'Pacer', 'Record']
__version__ = '0.2.0'
