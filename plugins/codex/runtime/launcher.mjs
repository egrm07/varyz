// No dependency install, source indexing or cloud call occurs during MCP startup.
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { delimiter, dirname, join } from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const args = process.argv.slice(2);
const client = args[0];
if (!['claude-code', 'codex'].includes(client)) process.exit(2);
const win = process.platform === 'win32';
const candidates = [...(process.env.PATH || '').split(delimiter), join(homedir(), '.local', 'bin')];
const executable = candidates.map(p => join(p, win ? 'varyz.exe' : 'varyz')).find(existsSync);
const config = process.env.VARYZ_CONFIG || join(homedir(), '.varyz', 'config.toml');

function child(command, argv, hook = false, env = process.env) {
  const proc = spawn(command, argv, { stdio: 'inherit', shell: false, windowsHide: true, env });
  proc.on('error', () => { process.stderr.write('Varyz could not start; run the setup skill.\n'); process.exit(hook ? 0 : 1); });
  proc.on('exit', code => process.exit(hook ? 0 : code ?? 1));
  for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => proc.kill(signal));
}

if (args[1] === '--install') {
  // Explicit owner/agent action only. Installer opens its existing consent page.
  if (args.slice(2).some(a => a !== '--dry-run')) process.exit(2);
  const dry = args.includes('--dry-run');
  const script = join(root, 'install', win ? 'install.ps1' : 'install.sh');
  const installEnv = { ...process.env };
  // A stale standalone-install opt-in must never preselect plugin sources.
  delete installEnv.VARYZ_BOOTSTRAP_APPROVE;
  delete installEnv.VARYZ_BOOTSTRAP_DRY_RUN;
  child(win ? 'powershell.exe' : 'sh', win
    ? ['-NoProfile', '-File', script, '-Web', ...(dry ? ['-DryRun'] : [])]
    : [script, '--web', ...(dry ? ['--dry-run'] : [])], false, installEnv);
} else if (args[1] === '--hook') {
  if (!['session-start', 'user-prompt', 'stop'].includes(args[2])) process.exit(0);
  if (!executable || !existsSync(config)) process.exit(0);
  child(executable, ['plugin', 'hook', '--client', client, '--event', args[2], '--config', config], true);
} else if (args.length === 1 && executable && existsSync(config)) {
  child(executable, ['plugin', 'serve', '--client', client, '--config', config]);
} else if (args.length === 1) {
  // Minimal MCP server remains discoverable before Python or Varyz is installed.
  const status = { installed: Boolean(executable), configured: existsSync(config),
    action: 'Help with the current task using the varyz-get-started workflow. Preferences can be applied in this conversation but are not saved by this bootstrap. Offer optional local setup when selected history would help; run varyz-setup only after the user requests installation. Restart this plugin after setup.',
    capabilities: { persistent_preferences: false, archive_retrieval: false },
    local_companion: 'Retrieve background from documents and supported agent history the user selects. Setup installs runtime dependencies, local models and a background service. No cloud account is required. Retrieved excerpts may be provided to the connected AI host.',
    hosted_limit: 'The hosted connection uses confirmed preferences; it cannot search the local archive.' };
  const send = object => process.stdout.write(JSON.stringify(object) + '\n');
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  lines.on('line', line => {
    if (Buffer.byteLength(line) > 65536) { send({jsonrpc:'2.0',id:null,error:{code:-32600,message:'Request too large'}}); return; }
    let msg;
    try { msg = JSON.parse(line); } catch { send({jsonrpc:'2.0',id:null,error:{code:-32700,message:'Parse error'}}); return; }
    if (!msg || typeof msg !== 'object' || Array.isArray(msg)) { send({jsonrpc:'2.0',id:null,error:{code:-32600,message:'Invalid request'}}); return; }
    if (msg.id === undefined) return;
    let result;
    if (msg.method === 'initialize') result = { protocolVersion: '2024-11-05', capabilities:{tools:{}},
      serverInfo:{name:'varyz-setup',version:'0.1.4'}, instructions:status.action };
    else if (msg.method === 'ping') result = {};
    else if (msg.method === 'tools/list') result = { tools:[{name:'plugin_status',description:'Read local installation readiness and setup instructions. Does not install or upload anything.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true,destructiveHint:false,openWorldHint:false}}] };
    else if (msg.method === 'tools/call' && msg.params?.name === 'plugin_status') result = {content:[{type:'text',text:JSON.stringify(status)}]};
    else { send({jsonrpc:'2.0',id:msg.id,error:{code:-32601,message:'Method or tool unavailable before setup'}}); return; }
    send({jsonrpc:'2.0',id:msg.id,result});
  });
} else process.exit(2);
