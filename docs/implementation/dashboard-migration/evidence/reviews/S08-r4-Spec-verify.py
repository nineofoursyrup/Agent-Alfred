"""Bounded independent Spec binding: no product execution or writes."""
from pathlib import Path
import hashlib
import json
import subprocess

W=Path('/Users/nineofour/.codex/worktrees/dashboard-s08/Agent-Alfred')
E=Path('/Users/nineofour/.codex/dashboard-migration-20261009/S08')
R=E/'r4';O=E.parent/'reviews'
H='51159d3854db388a6c178f8fea0b560fcda0bd13'
T='a7779e06b2592939767764828fd7c80b5d13a904'
B='87aab611e4a040ad0edc9956a43f6dff5d4944f3'
P='2951bf6892d9c5a7b43dedcb412ee54c938b2977'
K='c34d8462ae6cdf3eb332784972db9521ebf8c070'
F='tests/browser/shell-startup.spec.js'
errors=[]
def git(*args):return subprocess.check_output(['git',*args],cwd=W)
def blob(path,ref=H):return git('show',ref+':'+path)
def sha(data):return hashlib.sha256(data).hexdigest()
def read(path):return json.loads(path.read_text())
def check(value,label):
    if not value:errors.append(label)

check(git('rev-parse','HEAD').decode().strip()==H,'head')
check(git('rev-parse',H+'^{tree}').decode().strip()==T,'tree')
check(git('show','-s','--format=%P',H).decode().split()==[P,B],'merge parents')
check(not git('status','--porcelain'),'clean worktree')
check(git('diff','--name-only',P,H).decode().splitlines()==[F],'only startup test changed; production/config/dependencies identical')
check(git('diff','--numstat',P,H).decode().strip()=='30\t0\t'+F,'30 added test lines')
check(git('diff',P,H)==(R/'r3-to-r4.diff').read_bytes(),'exact r3-r4 diff')
check(git('diff',B,H)==(R/'candidate.diff').read_bytes(),'exact full candidate diff')
manifest=read(R/'candidate-files.json')
check(manifest['head']==H and manifest['tree']==T and manifest['base']==B,'manifest identity')
check(set(git('diff','--name-only',B,H).decode().splitlines())=={i['path'] for i in manifest['files']},'complete 21-file manifest')
for item in manifest['files']:
    data=blob(item['path']);check(sha(data)==item['sha256'] and len(data)==item['bytes'],item['path']+': bytes')
# Applying exactly the already-reviewed S08 collapsed-detail adaptations to
# accepted S02 r8 reproduces the final test byte-for-byte.
replacement="    await expect(page.locator(\"[data-model='opencode-go:deepseek-v4-flash']\")).toBeVisible();\n    // Models fields are mounted inside the initially collapsed details section.\n    await expect(page.getByRole('textbox',{name:'显示名',exact:true,includeHidden:true}).first()).toBeAttached();"
old="    await expect(page.getByRole('textbox',{name:'显示名',exact:true}).first()).toBeVisible();"
check(blob(F).decode()==blob(F,B).decode().replace(old,replacement),'accepted new startup test exact; three old S08 adaptations retained')
scope=read(R/'scope-delta.json')
for item in [*scope['inherited_artifacts'],*scope['raw_source_documents_unchanged']]:
    data=Path(item['path']).read_bytes();check(sha(data)==item['sha256'] and len(data)==item['bytes'],item['path']+': inherited raw identity')
for directory in ['docs/design/dashboard-implementation','docs/design/issue-87','docs/design/issue-90','docs/design/issue-91','docs/design/issue-92','docs/adr']:
    check(not git('diff',P,H,'--',directory),directory+': contracts unchanged')
coverage=read(E/'r3/source-coverage.json')
check(len(coverage['source_items'])==55 and len(coverage['acceptance_items'])==19,'original 55/19 inherited')
for key in ['source_items','acceptance_items','integration_chains']:check(all(i['result']=='NOT RUN' for i in coverage[key]),key+': whole NOT RUN')
inventory=read(E/'r3/resource-inventory.json')
for item in inventory['resources']:
    data=blob(item['file']);check(sha(data)==item['sha256'] and len(data)==item['bytes'],item['file']+': inherited resource')
check(len(inventory['resources'])==24 and len(inventory['edges'])==52,'24 resources / 52 inherited edges')
check(subprocess.run(['git','merge-base','--is-ancestor',K,H],cwd=W).returncode==0,'retain clearing commit')
check(blob('src/agent_alfred/settings_commands.py',K)==blob('src/agent_alfred/settings_commands.py'),'retain clearing production blob')
archive=read(R/'r3-candidate/ARCHIVE-MANIFEST.json')
for item in archive['files']:
    data=(E/item['path']).read_bytes();check(sha(data)==item['sha256'] and len(data)==item['bytes'],item['path']+': original/copy archive')
for name in ['S08-r3-Spec.md','S08-r3-Spec-details.md','S08-r3-Spec-verification.json']:
    check((O/name).read_bytes()==(R/'r3-candidate/reviews'/name).read_bytes(),name+': own original unchanged')
log=(R/'logs/02-startup-composition.log').read_text()
for text in ['5 passed (4.7s)','actual-startup-server-exits [0,0]','first native snapshot arriving after MainBar editing','initial entry barrier','initial state barrier','pending native panel-history transition','same-revision reconnect']:check(text in log,'startup log: '+text)
reconnect=json.loads(next(line.removeprefix('actual-startup-reconnect ') for line in log.splitlines() if line.startswith('actual-startup-reconnect ')))
check(reconnect['runtime']['connected'] and reconnect['pages']==1 and reconnect['posts']==[] and reconnect['reads']==['/api/models'],'actual reconnect observations')
check(len(reconnect['snapshots'])==2 and reconnect['snapshots'][0]==reconnect['snapshots'][1],'same revision native snapshots')
check(subprocess.run(['git','diff','--check',B,H],cwd=W).returncode==0,'full diff whitespace')
check(not git('status','--porcelain'),'final clean')
result={'metadata_result':'PASS' if not errors else 'FAIL','head':H,'tree':T,'base':B,'previous_head':P,'full_files':len(manifest['files']),'delta':'one startup test file, +30/-0','increment_sha256':sha(git('diff',P,H)),'full_diff_sha256':sha(git('diff',B,H)),'startup_test_sha256':sha(blob(F)),'startup_log_sha256':sha(log.encode()),'startup_result':'5 PASS; original r3 four cases plus exact accepted first-snapshot/breakpoint regression','actual_reconnect':reconnect,'inherited_artifacts':len(scope['inherited_artifacts']),'raw_documents':len(scope['raw_source_documents_unchanged']),'source_graph_sha256':sha((E/'r3/source-coverage.json').read_bytes()),'source_count':55,'AC_count':19,'resources':24,'edges':52,'archive_entries':len(archive['files']),'must_retain':K,'product_runs_by_reviewer':0,'boundaries':'Only r4 affected Spec binding. Future S03/S11 combination, overall source/AC/G, final CI/install/native200/IME NOT RUN; historical BFCache BLOCKED. No product/Git/ledger/tracker writes.','errors':errors}
(O/'S08-r4-Spec-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False,indent=2))
