import subprocess
import sys
checks=[('01-ruff-r2',['uv','run','ruff','check']),('02-skills-r2',['uv','run','python','scripts/check_skills.py']),('03-env-r2',['uv','run','python','scripts/check_env_example.py']),('05-typecheck-r2',['npm','run','typecheck']),('07-diff-r2',['zsh','-c','git diff --check && git diff --check origin/main...HEAD']),('04-pytest-r2',['uv','run','--extra','mcp','pytest']),('08-install-r2',['zsh','-c','uv build && uv run python scripts/check_mcp_installations.py --output .scratch/v11/mcp-artifacts.json'])]
failed=[]
for name, command in checks:
 result=subprocess.run([sys.executable,'-B','.scratch/v11/gate.py',name,*command])
 if result.returncode: failed.append(name)
print('Failed gates:',failed,flush=True)
sys.exit(bool(failed))
