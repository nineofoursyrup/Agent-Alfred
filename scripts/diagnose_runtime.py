"""Temporary stdlib-only boundary probe for CI-01."""
import sys
print('ci01 start', sys.executable, flush=True)
import faulthandler
faulthandler.dump_traceback_later(8, exit=True)
import sqlite3, _sqlite3, ctypes, subprocess, os
print('ci01 sqlite imported', _sqlite3.__file__, sqlite3.sqlite_version, flush=True)
lib = ctypes.CDLL(_sqlite3.__file__)
print('ci01 C library loaded', flush=True)
fn = lib.sqlite3_hard_heap_limit64
fn.argtypes = [ctypes.c_int64]
fn.restype = ctypes.c_int64
print('ci01 C heap', fn(-1), flush=True)
with sqlite3.connect(':memory:') as conn:
    print('ci01 DBAPI heap', conn.execute('PRAGMA hard_heap_limit').fetchone(), flush=True)
    print('ci01 source', conn.execute('SELECT sqlite_source_id()').fetchone(), flush=True)
for name, env in [('empty', {}), ('library', {'LD_LIBRARY_PATH':os.environ['LD_LIBRARY_PATH']})]:
    r=subprocess.run([sys.executable, '-c', 'import sqlite3;print(sqlite3.sqlite_version)'], env=env, capture_output=True, text=True, timeout=3)
    print('ci01 child', name, r.returncode, repr(r.stdout), repr(r.stderr), flush=True)
faulthandler.cancel_dump_traceback_later()
print('CI01_RUNTIME_COMPLETE', flush=True)
