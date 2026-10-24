import json
from pathlib import Path
import runpy
import subprocess
import sys

try:
    runpy.run_path('scripts/check_mcp_installations.py', run_name='__main__')
except subprocess.CalledProcessError as error:
    data = {'command': error.cmd, 'exit_code': error.returncode, 'stdout': error.stdout, 'stderr': error.stderr}
    path = Path(sys.argv[sys.argv.index('--output') + 1]).with_suffix('.error.json')
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    print('V12_INSTALL_DIAGNOSTIC ' + json.dumps(data, ensure_ascii=False), file=sys.stderr, flush=True)
    raise
