import datetime, hashlib, json, os, pathlib, shutil, subprocess, sys, time
repo=pathlib.Path.cwd()
evidence=repo/'docs/implementation/dashboard-claude-design/evidence/V05'
label=sys.argv[1]
command=sys.argv[2:]
start=time.monotonic()
at=datetime.datetime.now(datetime.timezone.utc).isoformat()
source={str(p.relative_to(repo)):hashlib.sha256(p.read_bytes()).hexdigest() for root in ('src','tests','scripts') for p in (repo/root).rglob('*') if p.is_file() and '.pyc' != p.suffix and '__pycache__' not in p.parts}
record={'command':command,'cwd':str(repo),'started_at':at,'head':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'source_sha256':hashlib.sha256(json.dumps(source,sort_keys=True).encode()).hexdigest(),'environment':{'ALFRED_BROWSER_TEST_PORT':os.environ.get('ALFRED_BROWSER_TEST_PORT')},'files':source}
result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
record.update({'exit_code':result.returncode,'elapsed_seconds':round(time.monotonic()-start,2),'output':result.stdout.decode(errors='replace'),'output_sha256':hashlib.sha256(result.stdout).hexdigest()})
out=evidence/'gates'/f'{label}.json'
if out.exists(): raise RuntimeError('refusing to overwrite check record')
out.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
if result.returncode and ('playwright' in command or 'test:browser' in command):
    dest=evidence/'failures'/label
    dest.mkdir(parents=True,exist_ok=False)
    for p in (repo/'output/playwright/results').glob('*'):
        if p.is_dir() and not p.name.startswith(".") and ((p / "error-context.md").exists() or (p / "test-failed-1.png").exists()): shutil.copytree(p,dest/p.name)
    (dest/'record.json').write_text(json.dumps({'gate':str(out.relative_to(evidence)),'head':record['head'],'source_sha256':record['source_sha256']},indent=2)+'\n')
print(label, 'exit',result.returncode, record['elapsed_seconds'],'s',flush=True)
print(record['output'][-5000:],flush=True)
sys.exit(result.returncode)
