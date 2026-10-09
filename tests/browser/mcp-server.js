import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {once} from 'node:events';

// Controls change only the loopback fixture's input files and IO barriers.
// Discovery, identity, authorization and cleanup remain the real Host paths.
export async function mcpServer(args=[]) {
  const child=spawn('.venv/bin/python',['tests/browser/mcp_server.py',...args]);
  const lines=createInterface({input:child.stdout});
  let errors='';child.stderr.on('data',data=>errors+=data);
  const exited=once(child,'exit');
  const value=await Promise.race([once(lines,'line').then(([line])=>JSON.parse(line)),
    exited.then(()=>{throw new Error(errors);})]);
  return {...value, async close(){child.kill('SIGTERM');await exited;lines.close();if(child.exitCode!==0)throw new Error(errors);}};
}

export const control=async(server,body)=>(await fetch(server.control,
  body?{method:'POST',body:JSON.stringify(body)}:{})).json();
