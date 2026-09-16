import faulthandler
faulthandler.dump_traceback_later(25, exit=True)
print('ci01 direct start', flush=True)
from pathlib import Path
from tempfile import TemporaryDirectory
from agent_alfred.evals.deterministic.test_database_boundaries import test_mapped_error_checks_raw_source_before_mapping as boundary
from agent_alfred.evals.deterministic.test_database_console_sql import test_hard_heap_limit_applies_only_in_worker_process as heap, test_packaged_worker_entry_executes_select as packaged
for value, code in [(b'bad type', 'data_invalid'), ('x'*(2*1024*1024+1), 'input_too_large')]:
    with TemporaryDirectory() as state:
        print('ci01 before boundary', code, flush=True)
        boundary(Path(state), value, code)
        print('ci01 boundary passed', code, flush=True)
print('ci01 before heap', flush=True)
heap()
print('ci01 same engine heap passed', flush=True)
with TemporaryDirectory() as state:
    packaged(Path(state))
print('ci01 packaged worker passed', flush=True)
faulthandler.cancel_dump_traceback_later()
