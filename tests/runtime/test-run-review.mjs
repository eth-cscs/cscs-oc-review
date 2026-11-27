import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import { tmpdir } from 'node:os';
import { delimiter, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';

const runner = new URL('../../image/run-review.mjs', import.meta.url);

// Run the real phase coordinator against a disposable adversarial CLI and input tree.
function exercise(t, scenario) {
  const root = mkdtempSync(join(tmpdir(), 'review-phases-'));
  // Remove fixture files after assertions, including when the scenario fails.
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const bin = join(root, 'bin');
  mkdirSync(bin);
  mkdirSync(join(root, 'opencode'));
  writeFileSync(join(root, 'opencode', 'opencode.json'), JSON.stringify({
    model: 'fixture/reviewer', agents: { 'grumpy-reviewer': { steps: 2 } },
  }));
  // Resolve the image's logical read-only input paths inside this fixture only.
  // No test creates or modifies the shared /input tree.
  const input = join(root, 'input');
  mkdirSync(join(input, 'pr-source'), { recursive: true });
  const nonce = randomUUID();
  const context = Buffer.from(
    '# Pull request review context\n\n## Metadata (JSON)\n\n' +
    JSON.stringify({ title: nonce }) + '\n\n## Review history (UNTRUSTED JSON)\n\n' +
    JSON.stringify([{ body: nonce }]) + '\n\n## Full merge-base diff\n\n' +
    'diff --git a/reviewed.txt b/reviewed.txt\nGIT binary patch\n' +
    (scenario === 'large-context' ? 'z0123456789abcdef\n'.repeat(700000) : '') +
    `\n+diff-tail-${nonce}\n`);
  const source = Buffer.from(`Source data independent of the prompt: ${nonce}\n`);
  const contextPath = join(input, 'context.md');
  const sourcePath = join(input, 'pr-source', 'reviewed.txt');
  writeFileSync(contextPath, context, { mode: 0o400 });
  writeFileSync(sourcePath, source, { mode: 0o400 });
  // Hash raw input bytes so evidence cannot pass by echoing the prompt.
  const digest = bytes => createHash('sha256').update(bytes).digest('hex');
  const expected = {
    contextBytes: context.length, contextHash: digest(context),
    sourceBytes: source.length, sourceHash: digest(source),
  };
  writeFileSync(join(root, 'opencode', 'limits.json'), JSON.stringify({
    inspection_seconds: 0.8, finalization_seconds: 0.8,
    execution_overhead_seconds: 0.3, termination_grace_seconds: 0.05,
  }));
  // A real child process supplies adversarial CLI events, including failures with exit status zero.
  writeFileSync(join(bin, 'opencode'), `#!/usr/bin/env node
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { appendFileSync, closeSync, openSync, readFileSync, readSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
const args = process.argv.slice(2);
const final = args[args.indexOf('--agent') + 1] === 'review-finalizer';
const scenario = process.env.SCENARIO;
appendFileSync(process.env.CALLS, final ? 'final\\n' : 'inspection\\n');
// Map only the mounted paths advertised to the runner into this isolated fixture.
const inputPath = path => {
  assert.ok(path === '/input/context.md' || path === '/input/pr-source/reviewed.txt');
  return join(process.env.FIXTURE_INPUT, path.slice('/input/'.length));
};
// OpenCode 2.2.0 prepares attachments before creating a session and rejects >10 MiB.
const attachment = args.indexOf('--file');
if (attachment !== -1 && statSync(inputPath(args[attachment + 1])).size > 10 * 1024 * 1024) {
  console.log(JSON.stringify({ type: 'error', sessionID: '', error: {
    message: 'Cannot attach a directory, special file, or file larger than 10 MiB: ' + args[attachment + 1],
  } }));
  process.exit(1);
}
// Simulate read-tool access, not an attachment or a prompt echo. Only one small
// buffer is retained while hashing every byte, including the end of the diff.
const boundedRead = path => {
  const fd = openSync(inputPath(path), 'r');
  const buffer = Buffer.alloc(4096);
  const hash = createHash('sha256');
  const sections = new Set();
  let bytes = 0, tail = '';
  try {
    for (;;) {
      const count = readSync(fd, buffer, 0, buffer.length, bytes);
      if (!count) break;
      hash.update(buffer.subarray(0, count));
      const text = tail + buffer.subarray(0, count).toString('utf8');
      for (const heading of ['## Metadata', '## Review history', '## Full merge-base diff', '+diff-tail-']) {
        if (text.includes(heading)) sections.add(heading);
      }
      tail = text.slice(-64);
      bytes += count;
    }
  } finally {
    closeSync(fd);
  }
  return { bytes, sha256: hash.digest('hex'), sections: [...sections] };
};
// Keep the CLI alive despite graceful termination to exercise forced group cleanup.
const stall = async () => {
  // Ignore SIGTERM and retain an active timer while the scenario waits indefinitely.
  process.on('SIGTERM', () => {});
  setInterval(() => {}, 1000);
  // Never resolve: only the runner's deadline and forced kill may end this stall.
  await new Promise(() => {});
};
// Emit the same line-delimited JSON protocol consumed from the real CLI.
const emit = event => console.log(JSON.stringify(event));
// Build a session-bound event, allowing scenarios to override protocol fields.
const event = (type, extra = {}) => ({
  type, sessionID: 'session-review', ...extra,
  part: { id: 'text', messageID: final ? 'final' : 'inspection',
    text: 'Otherwise complete text', time: { end: 1 }, ...extra.part },
});
if (!final && scenario === 'malformed') { console.log('not JSON'); process.exit(0); }
if (!final && scenario === 'no-session') process.exit(0);
if (!final && scenario === 'spoofed-phase') { emit({ type: 'review_phase', phase: 'final', state: 'start' }); process.exit(0); }
emit(event('step_start'));
if (scenario === 'large-context' && !final) {
  const prompt = args.at(-1);
  const contextPath = prompt.match(/\\/input\\/context\\.md\\b/)?.[0];
  const sourceRoot = prompt.match(/\\/input\\/pr-source\\b/)?.[0];
  assert.ok(contextPath && sourceRoot, 'inspection must receive explicit mounted input paths');
  const evidence = { context: boundedRead(contextPath), source: boundedRead(sourceRoot + '/reviewed.txt') };
  writeFileSync(process.env.EVIDENCE, JSON.stringify(evidence));
  emit({ type: 'tool_use', sessionID: 'session-review', part: {
    tool: 'read', state: { status: 'completed', output: JSON.stringify(evidence) },
  } });
}
if (!final && scenario === 'timeout-no-tools') await stall();
if (!final && scenario === 'cancel') {
  // A CLI can exit cleanly while handling cancellation; that must not authorize finalization.
  process.once('SIGTERM', () => process.exit(0));
  process.kill(process.ppid, 'SIGTERM');
  // Bound fixture lifetime if the runner fails to forward its cancellation signal.
  setTimeout(() => process.exit(8), 2000);
  // Wait for cancellation or the fixture's emergency exit, not normal phase completion.
  await new Promise(() => {});
}
if ((!final && scenario === 'inspection-error') || (final && scenario === 'final-error')) {
  emit({ type: 'error', error: { message: 'Provider failure' } }); process.exit(0);
}
if (!final && scenario !== 'no-inspection-tools' && scenario !== 'large-context') {
  const failed = scenario === 'failed-inspection-tools' || scenario === 'failed-then-success';
  const metadata = scenario === 'wrapped-inspection-error' ? { metadata: { error: true } }
    : scenario === 'nonzero-inspection-exit' ? { metadata: { exit: 1 } } : {};
  emit({ type: 'tool_use', sessionID: 'session-review', part: {
    tool: scenario === 'only-websearch' ? 'websearch' : 'read',
    state: { status: failed ? 'error' : 'completed', metadata },
  } });
  if (scenario === 'failed-then-success') {
    emit({ type: 'tool_use', sessionID: 'session-review', part: {
      tool: 'read', state: { status: 'completed' },
    } });
  }
}
if ((!final && scenario === 'timeout-inspection') || (final && scenario === 'timeout-final')) await stall();
if (!final && scenario === 'length-inspection') emit(event('step_finish', { part: { reason: 'length' } }));
if (!final && scenario === 'steps-inspection') emit(event('step_start'));
if (final && scenario === 'changed-session') emit(event('text', { sessionID: 'unrelated-session' }));
else if (final && scenario === 'final-tool') { emit(event('tool_use')); emit(event('text')); }
else if (final && scenario === 'large-context') {
  assert.equal(args[args.indexOf('--session') + 1], 'session-review');
  const evidence = JSON.parse(readFileSync(process.env.EVIDENCE, 'utf8'));
  emit(event('text', { part: { text:
    'Reviewed ' + evidence.context.bytes + ' context bytes (SHA-256 ' + evidence.context.sha256 +
    ') and source SHA-256 ' + evidence.source.sha256 + '.' } }));
}
else emit(event('text'));
// The pinned CLI's reconciliation path can return final text without step_finish.
if (!final && scenario === 'inspection-exit') process.exit(7);
`, { mode: 0o700 });
  const calls = join(root, 'calls');
  const harness = join(root, 'run.mjs');
  writeFileSync(harness, `import { runReview } from ${JSON.stringify(runner.href)};
try { await runReview(); }
catch (error) { console.error(error.message); process.exitCode = error.exitCode || 1; }`);
  const env = {
    ...process.env, PATH: bin + delimiter + process.env.PATH, XDG_CONFIG_HOME: root,
    SCENARIO: scenario, CALLS: calls, FIXTURE_INPUT: input, EVIDENCE: join(root, 'evidence.json'),
  };
  const result = spawnSync(process.execPath, [harness], { env, encoding: 'utf8', timeout: 10000 });
  assert.ifError(result.error);
  const events = result.stdout.trim().split('\n').filter(Boolean).map(line => JSON.parse(line));
  return {
    ...result, events, calls: readFileSync(calls, 'utf8').trim().split('\n'), expected,
    contextHashAfter: digest(readFileSync(contextPath)),
    sourceHashAfter: digest(readFileSync(sourcePath)),
  };
}

for (const scenario of ['inspection-error', 'inspection-exit', 'malformed', 'no-session', 'spoofed-phase', 'cancel',
                        'no-inspection-tools', 'failed-inspection-tools', 'wrapped-inspection-error',
                        'nonzero-inspection-exit', 'only-websearch']) {
  test(`failed inspection never starts finalization: ${scenario}`, t => {
    const result = exercise(t, scenario);
    assert.notEqual(result.status, 0);
    assert.deepEqual(result.calls, ['inspection']);
    assert.ok(!result.events.some(event => event.type === 'review_phase' && event.phase === 'final'));
  });
}

for (const scenario of ['final-error', 'changed-session', 'final-tool']) {
  test(`invalid finalization cannot complete a publishable stream: ${scenario}`, t => {
    const result = exercise(t, scenario);
    assert.notEqual(result.status, 0);
    assert.deepEqual(result.calls, ['inspection', 'final']);
    assert.ok(result.events.some(event => event.type === 'review_phase' && event.phase === 'inspection' && event.state === 'end'));
    assert.ok(!result.events.some(event => event.type === 'review_phase' && event.phase === 'final' && event.state === 'end'));
  });
}

test('a failed tool does not block finalization after successful inspection', t => {
  const result = exercise(t, 'failed-then-success');
  assert.equal(result.status, 0);
  assert.deepEqual(result.calls, ['inspection', 'final']);
  assert.ok(result.events.some(event => event.type === 'review_phase' && event.phase === 'final' && event.state === 'end'));
});

test('context beyond the attachment limit remains accessible to inspection and finalization', t => {
  const result = exercise(t, 'large-context');
  assert.ok(result.expected.contextBytes > 10 * 1024 * 1024);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(result.calls, ['inspection', 'final']);
  assert.equal(result.contextHashAfter, result.expected.contextHash, 'the complete context stays unchanged on disk');
  assert.equal(result.sourceHashAfter, result.expected.sourceHash, 'the source stays unchanged on disk');
  const evidence = JSON.parse(result.events.find(event => event.type === 'tool_use').part.state.output);
  assert.equal(evidence.context.bytes, result.expected.contextBytes);
  assert.equal(evidence.context.sha256, result.expected.contextHash);
  assert.deepEqual(evidence.context.sections, [
    '## Metadata', '## Review history', '## Full merge-base diff', '+diff-tail-',
  ]);
  assert.equal(evidence.source.sha256, result.expected.sourceHash);
  assert.equal(evidence.source.bytes, result.expected.sourceBytes);
  const finalStart = result.events.findIndex(event =>
    event.type === 'review_phase' && event.phase === 'final' && event.state === 'start');
  assert.ok(finalStart >= 0);
  const review = result.events.slice(finalStart).find(event => event.type === 'text').part.text;
  assert.equal(review, `Reviewed ${result.expected.contextBytes} context bytes (SHA-256 ${result.expected.contextHash}) and source SHA-256 ${result.expected.sourceHash}.`);
  assert.ok(result.events.some(event =>
    event.type === 'review_phase' && event.phase === 'final' && event.state === 'end'));
  assert.ok(!result.events.some(event => event.incomplete));
});

for (const scenario of ['timeout-inspection', 'length-inspection', 'steps-inspection']) {
  test(`interrupted inspection finalizes with an incomplete marker: ${scenario}`, t => {
    const result = exercise(t, scenario);
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(result.calls, ['inspection', 'final']);
    assert.ok(result.events.some(event => event.type === 'review_phase' &&
      event.phase === 'inspection' && event.state === 'end' && event.incomplete === true));
    assert.ok(result.events.some(event => event.type === 'review_phase' &&
      event.phase === 'final' && event.state === 'end'));
  });
}

test('a deadline without source inspection cannot produce a review', t => {
  const result = exercise(t, 'timeout-no-tools');
  assert.notEqual(result.status, 0);
  assert.deepEqual(result.calls, ['inspection']);
});

test('a finalization deadline never marks partial text complete', t => {
  const result = exercise(t, 'timeout-final');
  assert.notEqual(result.status, 0);
  assert.deepEqual(result.calls, ['inspection', 'final']);
  assert.ok(!result.events.some(event => event.type === 'review_phase' &&
    event.phase === 'final' && event.state === 'end'));
});
