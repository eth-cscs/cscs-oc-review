// Test-only HTTPS provider: streaming, tool calls, stalled requests, and credential assertions.
import https from 'node:https';
import fs from 'node:fs';
import crypto from 'node:crypto';

// Record upstream observations without storing request authentication.
const log = event => fs.appendFileSync('/fixture/requests.jsonl', JSON.stringify(event) + '\n');
let inspected = false;
let releaseStream;
// Serve control endpoints or emit the requested deterministic inference stream.
const server = https.createServer({
  key: fs.readFileSync('/fixture/key.pem'), cert: fs.readFileSync('/fixture/cert.pem'),
}, async (request, response) => {
  if (request.method === 'GET' && request.url === '/release') {
    releaseStream?.();
    response.end('released');
    return;
  }
  if (request.method === 'GET' && request.url === '/requests') {
    response.end(fs.existsSync('/fixture/requests.jsonl') ? fs.readFileSync('/fixture/requests.jsonl') : '');
    return;
  }
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const raw = Buffer.concat(chunks);
  if (request.headers.authorization !== `Bearer ${process.env.CANARY_KEY}`) {
    response.writeHead(401).end('upstream authentication failed');
    return;
  }
  const body = JSON.parse(raw);
  log({path: request.url, method: request.method, bytes: raw.length,
    digest: crypto.createHash('sha256').update(raw).digest('hex'),
    clientAuthRemoved: !request.headers['x-api-key'] && !request.headers['proxy-authorization'],
    model: body.model, max_tokens: body.max_tokens, reasoning_effort: body.reasoning_effort,
    final: body.tool_choice === 'none', messages: body.probe ? undefined : body.messages});
  if (body.probe === 'error') {
    response.writeHead(429, {'Content-Type': 'application/json'}).end('{"error":"fixture rate limit"}');
    return;
  }
  response.writeHead(200, {'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache'});
  response.flushHeaders();
  // Encode one OpenAI-compatible streaming delta.
  const event = (delta, finish = null) => response.write(`data: ${JSON.stringify({
    id: 'fixture', object: 'chat.completion.chunk', created: 1, model: body.model,
    choices: [{index: 0, delta, finish_reason: finish}],
  })}\n\n`);
  event({role: 'assistant', reasoning: 'fixture reasoning'});
  if (body.probe) {
    event({content: 'first'});
    if (body.probe === 'stall') {
      // Expose upstream cancellation when the downstream client disconnects.
      response.on('close', () => log({aborted: true}));
      return;
    }
    // Hold the second chunk until the client observes and releases the first.
    await new Promise(resolve => { releaseStream = resolve; });
    event({content: 'second'}, 'stop');
  } else if (!inspected && body.tool_choice !== 'none') {
    inspected = true;
    const command = `node -e 'const fs=require("node:fs"); let writable=false; try { fs.writeFileSync("/input/pr-source/app.py", "changed"); writable=true; } catch {} const boundary={realKeyPresent:!!process.env.CSCS_INFERENCE_KEY,githubTokenPresent:!!process.env.GITHUB_TOKEN,proxyTokenPresent:!!process.env.CSCS_REVIEW_PROXY_TOKEN,proxyConfigVisible:fs.existsSync("/etc/caddy/proxy.json"),sourceWritable:writable,uid:process.getuid()}; console.log("PROXY_BOUNDARY:"+Buffer.from(JSON.stringify(boundary)).toString("base64")); console.log(fs.readFileSync("/input/pr-source/app.py","utf8"));'`;
    event({tool_calls: [{index: 0, id: 'inspect-source', type: 'function',
      function: {name: 'shell', arguments: JSON.stringify({command})}}]}, 'tool_calls');
  } else {
    event({content: body.tool_choice === 'none'
      ? '## Summary\n\nCredential proxy fixture review.\n' : 'Inspection complete.'}, 'stop');
  }
  response.write('data: [DONE]\n\n');
  response.end();
});
server.listen(9443, '0.0.0.0');
