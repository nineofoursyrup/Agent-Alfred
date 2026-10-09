"""Independent fixed ci-r2 binding audit; no service/test/product mutations."""
import ast
from collections import Counter
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import posixpath
import re
import subprocess

E=Path('/Users/nineofour/.codex/dashboard-migration-20261009')
C=E/'S08/ci-r2'
W=Path('/Users/nineofour/.codex/worktrees/dashboard-s08/Agent-Alfred')
H='abb3817cee1c6491094f589c2c25c5257d5a1ac8'
T='cbe04daa248ffe4020316544f9c7ee5630fadafd'
B='da7d6b84899b19d690d574c74397a45b0239078b'
P='e6a91d0d8c5eae5d75829e543a79140f9e060935'
KEEP='c34d8462ae6cdf3eb332784972db9521ebf8c070'
errors=[]
report={'axis':'Spec','scope':'Only accepted S05/listener synchronization and evidence binding increment','head':H,'tree':T,'base':B,'previous':P}

def git(*args):
    return subprocess.check_output(['git','-C',str(W),*args])

def sha(data):
    return hashlib.sha256(data).hexdigest()

def load(path):
    return json.loads(path.read_text())

def check(ok,message):
    if not ok: errors.append(message)

def entries(items,root):
    for item in items:
        data=(root/item['path']).read_bytes()
        check(len(data)==item['bytes'] and sha(data)==item['sha256'],'evidence mismatch '+item['path'])
    return len(items)

candidate=load(C/'candidate-files.json')
check(git('rev-parse',H+'^{tree}').decode().strip()==T,'tree')
check(git('rev-parse','HEAD').decode().strip()==H,'live HEAD')
check(not git('status','--porcelain=v1'),'dirty working tree')
parents=git('show','-s','--format=%P',H).decode().split()
check(parents==[P,B],'exact merge parents')
base_tree=git('rev-parse',B+'^{tree}').decode().strip()
check(base_tree==candidate['base_tree'],'base tree binding')
src_tree=git('rev-parse',H+':src').decode().strip()
check(src_tree==git('rev-parse',B+':src').decode().strip()=='22706c002421b5393ae4a830b989afa53e68b33b','whole src equals accepted')
for ancestor in [P,B,KEEP]:
    check(subprocess.run(['git','-C',str(W),'merge-base','--is-ancestor',ancestor,H]).returncode==0,'ancestor '+ancestor)
backend=git('rev-parse',H+':src/agent_alfred/settings_commands.py').decode().strip()
check(backend=='b43452e7f063546cdff569636add6aba3c9aef96','clear backend retained')
paths=git('diff','--name-only',B,H).decode().splitlines()
check(paths==['tests/browser/mcp.spec.js'],'candidate scope')
check(git('diff','--numstat',B,H).decode().strip()=='4\t0\ttests/browser/mcp.spec.js','four additions only')
check(git('diff',B,H)==(C/'candidate.diff').read_bytes(),'candidate diff')
check(git('diff',P,H)==(C/'ci-r1-to-ci-r2.diff').read_bytes(),'composition diff')
check(not git('diff','--check',B,H) and not git('diff','--check',P,H),'diff checks')
changed=git('diff','--name-only',P,H).decode().splitlines()
check(changed==candidate['changes_vs_previous'] and len(changed)==8,'eight accepted changes')
for path in changed:
    check(git('show',H+':'+path)==git('show',B+':'+path),'non-accepted change '+path)
owned=['src/agent_alfred/ops/static/'+n for n in ['models.js','connections.js','settings-focus.js','settings.css']]+['src/agent_alfred/settings_commands.py','tests/browser/mcp.spec.js','tests/browser/mcp_server.py','tests/browser/settings_server.py','playwright.config.js','package.json','package-lock.json']
for path in owned:
    check(git('show',H+':'+path)==git('show',P+':'+path),'S08/test/dependency byte change '+path)
test=git('show',H+':tests/browser/mcp.spec.js')
check(sha(test)=='f4785fbace403d5b8b8189a102400975e192a7349919b17533bd12a4ea80577a','exact previously reviewed test')
report['candidate']={'parents':parents,'base_tree':base_tree,'src_tree':src_tree,'backend_blob':backend,'accepted_changed_paths':changed,'owned_unchanged_paths':owned,'test_sha256':sha(test),'diff_sha256':sha((C/'candidate.diff').read_bytes()),'working_tree_clean':True}

report['manifest_bindings']={}
for label,path,root,key in [
    ('ci_r2',C/'MANIFEST.json',E,'files'),
    ('ci_r1_archive',C/'ci-r1-candidate/ARCHIVE-MANIFEST.json',E,'files'),
    ('ci_r1_in_place',E/'S08/ci-r1/MANIFEST.json',E/'S08/ci-r1','files'),
    ('PR_original_failure',E/'ci/after-s08/pr-browser-first-failure/manifest.json',E,'artifacts'),
    ('push_original_failure',E/'ci/after-s08/push-browser-first-failure/manifest.json',E/'ci/after-s08/push-browser-first-failure','files')]:
    report['manifest_bindings'][label]={'path':str(path),'count':entries(load(path)[key],root),'sha256':sha(path.read_bytes())}
validation=load(C/'evidence-validation.json')
for name in validation['mirror_paths']:
    check((C/name).read_bytes()==(E/'S08'/name).read_bytes(),'mirror '+name)
prior_expected={'S08-ci-r1-Spec.md':'5da2254e3e2bde24758f01720e29ba2efb72f5a3ffc7380703d3591728ee8d42','S08-ci-r1-Spec-details.md':'ae49d5db22a74126c0e2faa35243168c7b1ad4fc9eb55e284236f19fc9d2d43f','S08-ci-r1-Spec-verification.json':'ee3b6ef4a4cf693b88cfc8e25a3cef7cb157784b3c5ff1e3182dd8c0906a65c8'}
for name,digest in prior_expected.items():
    check(sha((C/'ci-r1-candidate/reviews'/name).read_bytes())==digest,'prior independent Spec '+name)
report['prior_independent_Spec_bindings']=prior_expected

current=load(C/'source-coverage.json')
historical=load(E/'S08/r3/source-coverage.json')
frozen_bytes=git('show',H+':docs/design/issue-92/acceptance-map.json')
check(sha(frozen_bytes)==current['source_map_sha256']=='6689fa44948bdf01bc4e4fe2fc63d6532a0d3067df9c5b2b0f32ab166a1e30e7','frozen map hash')
frozen=json.loads(frozen_bytes)
source_index={item['source_id']:item for item in frozen['source_items']}
check(current['head']==H and current['tree']==T and current['base']==B,'current source table binding')
for name,count in [('source_items',55),('acceptance_items',19),('integration_chains',5)]:
    check(current[name]==historical[name] and len(current[name])==count,'historical record altered '+name)
for item in current['source_items']:
    for key,value in source_index[item['source_id']].items():
        if key!='slice_fragment_evidence':
            check(item[key]==value,'frozen source metadata '+item['source_id']+'/'+key)
check(current['result']=='NOT RUN','overall result upgraded')
entries([current['inheritance']['original_table'],current['inheritance']['ci_r1']],E)
report['source_inheritance']={'sources':55,'AC':19,'integration_chains':5,'original_records_equal_to_r3':True,'frozen_source_metadata_equal_except_separately_inherited_historical_fragments':True,'overall_result':'NOT RUN','current_table_sha256':sha((C/'source-coverage.json').read_bytes()),'r3_table_sha256':sha((E/'S08/r3/source-coverage.json').read_bytes())}

class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs=[]
    def handle_starttag(self,tag,attrs):
        self.refs.extend(value for key,value in attrs if key in ('href','src') and value)

prefix='src/agent_alfred/ops/static/'
seen=set();edges=[];queue=['index.html']
while queue:
    name=queue.pop(0)
    if name in seen: continue
    seen.add(name)
    text=git('show',H+':'+prefix+name).decode()
    if name.endswith('.html'):
        parser=LinkParser();parser.feed(text);refs=parser.refs
    elif name.endswith('.js'):
        refs=re.findall(r'''\b(?:import|export)\s+(?:[^;'\"]*?\bfrom\s*)?['\"]([^'\"]+)''',text)
        refs+=re.findall(r'''\bimport\s*\(\s*['\"]([^'\"]+)''',text)
    elif name.endswith('.css'):
        refs=re.findall(r'''url\(\s*['\"]?([^)'\"]+)''',text)
    else:
        refs=[]
    for ref in refs:
        if ref.startswith('/assets/'): target=ref[8:]
        elif ref.startswith(('./','../')): target=posixpath.normpath(posixpath.join(posixpath.dirname(name),ref))
        else: continue
        check(not target.startswith('../'),'escaping resource reference')
        edges.append((name,ref,target));queue.append(target)
inventory=load(C/'resource-inventory.json')
check(inventory['candidate']==H and inventory['tree']==T and inventory['base']==B,'resource identity')
check(len(seen)==inventory['resource_count']==33 and len(edges)==inventory['edge_count']==77,'33/77 current discovery')
check(Counter(edges)==Counter((r['from'],r['reference'],r['to']) for r in inventory['edges']),'resource edge multiset')
check(seen=={r['file'][len(prefix):] for r in inventory['resources']},'resource node set')
module=ast.parse(git('show',H+':src/agent_alfred/gateway/web/assets.py').decode())
registry=next(ast.literal_eval(n.value) for n in module.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='ASSETS' for t in n.targets))
check(set(registry)=={'/assets/'+name for name in seen if name!='index.html'},'ASSETS equal discovered closure')
http=load(C/'resource-http-identity.json');http_by_url={r['url']:r for r in http['resources']}
check(http['candidate']==H and http['tree']==T and http['result']=='PASS','HTTP evidence identity')
check(http['resource_count']==len(http['resources'])==len(http_by_url)==33,'HTTP resource count')
resource_rows=[]
for resource in inventory['resources']:
    data=git('show',H+':'+resource['file'])
    url=resource['url'];mime='text/html' if url=='/' else registry[url][1]
    check(len(data)==resource['bytes'] and sha(data)==resource['sha256'],'source resource '+url)
    read=http_by_url[url]
    check(resource['mime']==mime and read['status']==200 and read['content_type'].split(';')[0]==mime and read['sha256']==read['candidate_sha256']==sha(data),'HTTP status/MIME/hash '+url)
    resource_rows.append({'url':url,'sha256':sha(data),'bytes':len(data),'owner':resource['owner']})
check(http['model_calls']==[] and http['fixture_exit_code']==0,'HTTP fixture outcome')
check(not inventory['missing_references'] and not inventory['unreachable_registry_assets'],'incomplete closure')
report['resources']={'count':33,'edges':77,'independent_discovery_matches':True,'discovery':'HTMLParser links and independent import/export/dynamic import/CSS reference matching against fixed Git objects, then AST ASSETS comparison','http_status_MIME_hash_matches':33,'model_calls':[],'fixture_exit_code':0,'origin':http['origin'],'instance_id':http['instance_id'],'rows':resource_rows}

checks=load(C/'checks.json')
check(checks['head']==H and checks['tree']==T and checks['overall_source_AC_G_result']=='NOT RUN','checks binding')
for result in checks['observed_checks']:
    entries([result['artifact']],E)
compatibility=(C/'logs/02-listener-compatibility-target.log').read_text()
check('Running 1 test using 1 worker' in compatibility and '1 passed (2.8s)' in compatibility,'one actual target run')
dimensions=[json.loads(line.split('S08 Connections central widths ',1)[1]) for line in compatibility.splitlines() if 'S08 Connections central widths ' in line]
check(len(dimensions)==1 and [r['viewport'] for r in dimensions[0]]==[[1440,900],[1280,800],[390,844],[320,800]],'one set four target viewports')
config=git('show',H+':playwright.config.js').decode()
check('retries: 0' in config or 'retries:0' in config,'no configured retries')
check(load(C/'compatibility-artifacts/.last-run.json')=={'status':'passed','failedTests':[]},'target artifact result')
ports=load(C/'ports-after.json')
check([row['port'] for row in ports['ports']]==list(range(17920,17930)),'cleanup exact ports')
check(all(row['raw_bind']=='PASS' and row['server_reuseaddr_bind']=='PASS' and row['connect_ex']==61 for row in ports['ports']),'final cleanup bind/refusal')
check(ports['matching_fixture_processes']==[],'fixture process cleanup')
remote=checks['remote_original_results']
check([(row['run'],row['result']) for row in remote]==[(37871594920,'FAIL'),(37871590592,'FAIL')],'original remote failures')
for row in remote: entries([row['manifest']],E)
failure_ids=[r['id'] for r in checks['observed_checks'] if r['result']=='FAIL']
check(len(failure_ids)==5,'first failures retained')
ownership=load(C/'ownership-and-applicability.json')
check(ownership['accepted_composition']['paths']==changed,'accepted ownership')
check(ownership['resource_ownership']==[{k:r[k] for k in ['file','url','owner','sha256']} for r in inventory['resources']],'resource ownership equality')
check('nested cleanup' in ownership['future_responsibilities']['S07'] and 'four-line wait' in ownership['future_responsibilities']['S07'],'future S07 union responsibility')
report['checks']={'new_listener_target':'1 PASS; retries 0','target_dimensions':dimensions[0],'prior_GET_probe_and_repeat5':'Inherited ci-r1 only, not rerun','HTTP_check':'Implementation evidence reused; not rerun by reviewer','first_failure_ids_retained':failure_ids,'original_remote_results':[(row['run'],row['result']) for row in remote],'cleanup_ports':10,'cleanup_processes':0,'lsof_warning_retained':bool(ports['listen_lsof_stderr'])}
report['boundaries']={'future_responsibilities':ownership['future_responsibilities'],'whole_source_AC_G':'NOT RUN','final_CI_install_native':'Not completed by this increment','no_new_product_runs_by_reviewer':True,'current_root_Standards_content_read':False,'product_Git_ledger_GitHub_writes':False}
report['reviewer_tooling_notes']=['Initial diagnostic lookup used absent frozen-map acceptance_items key. Read-only KeyError retained in conversation; correct schema uses source_items and current 19 acceptance records were compared intact with historical r3. No source/evidence original changed.']
report['result']='PASS' if not errors else 'FAIL'
report['errors']=errors
path=E/'reviews/S08-ci-r2-Spec-verification.json'
path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'result':report['result'],'errors':errors,'manifest_counts':{k:v['count'] for k,v in report['manifest_bindings'].items()},'sources':55,'AC':19,'resources':33,'edges':77,'output':str(path)},ensure_ascii=False,indent=2))
raise SystemExit(bool(errors))
