"""Temporary signal observation; no changes to test assertions or deadlines."""
import faulthandler
import os
import sys
import pytest
parent = os.getpgrp()
real_killpg = os.killpg

def observed(group, sig):
    print('ci01 pytest killpg', group, sig, 'parent', parent, flush=True)
    if group == parent:
        raise OSError('CI01 safety guard: would kill parent process group')
    return real_killpg(group, sig)

os.killpg = observed
print('ci01 pytest start', os.getpid(), parent, flush=True)
faulthandler.dump_traceback_later(25, exit=True)
sys.exit(pytest.main(['-x', '-vv', '-s',
    'src/agent_alfred/evals/deterministic/test_database_boundaries.py::test_mapped_error_checks_raw_source_before_mapping',
    'src/agent_alfred/evals/deterministic/test_database_console_sql.py::test_hard_heap_limit_applies_only_in_worker_process',
    'src/agent_alfred/evals/deterministic/test_database_console_sql.py::test_packaged_worker_entry_executes_select']))
