import {createHash} from 'node:crypto';
import {readFile,writeFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';
const {Stream}=await import(pathToFileURL(resolve('src/agent_alfred/ops/static/stream.js')).href);

class ControlledSource {
  listeners=new Map();readyState=1;
  constructor(url){this.url=url;}
  addEventListener(name,callback){this.listeners.set(name,callback);}
  emit(name,data){this.listeners.get(name)?.({data:JSON.stringify(data)});}
  frame(data){this.listeners.get('domain_event')?.({data});}
  close(){this.readyState=2;}
}
globalThis.EventSource=ControlledSource;
const nativeCrypto=globalThis.crypto;
const records=[];
async function probe(mode){
  let releaseDigest,digestStarted;
  const started=new Promise(resolve=>{digestStarted=resolve;});
  const held=new Promise(resolve=>{releaseDigest=resolve;});
  Object.defineProperty(globalThis,'crypto',{configurable:true,value:{subtle:{digest:async(...args)=>{digestStarted();await held;return nativeCrypto.subtle.digest(...args);}}}});
  let connected=false,revision=-1;
  const events=[];
  const stream=new Stream((kind,body,first)=>{
    events.push({kind,first,sourceState:stream.source.readyState,revision:body.state_revision});
    if(kind==='state_patch' && (first||body.state_revision>revision)){connected=true;revision=body.state_revision;}
  },()=>{connected=false;});
  stream.connect(null);stream.source.emit('state_patch',{state_revision:1});
  await new Promise(setImmediate);
  const payload=JSON.stringify({fixture:true});
  const canonical=`{"event":"fixture.event","event_id":"event-1","payload":${payload}}`;
  const hash=createHash('sha256').update(canonical).digest('hex');
  stream.source.frame(`{"seq":1,"event":"fixture.event","event_id":"event-1","chunk_index":0,"chunk_count":1,"event_sha256":"${hash}","payload":${payload}}`);
  stream.source.emit('state_patch',{state_revision:2});
  await started;
  if(mode==='error_epoch_control')stream.source.listeners.get('error')();
  if(mode==='generation_control')stream.generation++;
  stream.source.close();connected=false;
  const atClose={connected,revision,sourceState:stream.source.readyState};
  releaseDigest();await new Promise(setImmediate);await new Promise(setImmediate);
  return {mode,atClose,afterQueuedWork:{connected,revision,sourceState:stream.source.readyState},events};
}
for(const mode of ['native_close_only','error_epoch_control','generation_control'])records.push(await probe(mode));
Object.defineProperty(globalThis,'crypto',{configurable:true,value:nativeCrypto});
if(!records[0].afterQueuedWork.connected||records[1].afterQueuedWork.connected||records[2].afterQueuedWork.connected)throw new Error('Controlled queue hypothesis did not match');
const streamBytes=await readFile('src/agent_alfred/ops/static/stream.js');
const appBytes=await readFile('src/agent_alfred/ops/static/app.js');
const report={checked_at:new Date().toISOString(),node_version:process.version,source_sha256:{stream:createHash('sha256').update(streamBytes).digest('hex'),app:createHash('sha256').update(appBytes).digest('hex')},experiment:'Unmodified Stream module with controlled EventSource and a held real SHA-256 digest. Callback models the same-instance newer revision acceptance in app.js. No HTTP/browser/provider interaction.',scope_limit:'This proves a possible pre-existing close/queue path. It does not prove that any recorded browser failure followed this ordering, and does not replace any gate.',records};
await writeFile('docs/implementation/dashboard-claude-design/evidence/V05/auxiliary/stream-queue-probe-r2.json',JSON.stringify(report,null,2)+'\n');
console.log(JSON.stringify(records));
