from pathlib import Path
import json
import subprocess
root=Path.cwd(); scratch=root/'.scratch/v11'
wrapper='/Users/nineofour/.codex/skills/playwright/scripts/playwright_cli.sh'
for script,output in [('capture.js','capture-final.txt'),('runs-proof.js','runs-proof.txt')]:
    result=subprocess.run([wrapper,'--session','v11-final','run-code',(scratch/script).read_text()],cwd=scratch,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    (scratch/('final-'+script+'.raw')).write_text(result.stdout)
    if result.returncode: raise SystemExit(result.stdout)
    value=result.stdout.split('### Result\n',1)[1].split('\n### ',1)[0]
    parsed=json.loads(value)
    (scratch/output).write_text(json.dumps(parsed,ensure_ascii=False)+'\n')
    print(script,'complete',flush=True)
