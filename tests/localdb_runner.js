/* Drives web/localdb.js for tests/test_localdb.py: one JSON command per
 * input line, one JSON answer per output line.
 *   {"op":"request","method":"POST","path":"/api/tasks","body":{...},"now":"2026-10-06T15:00:00"}
 *   {"op":"reminders","now":"..."}    -> due reminders (claimed)
 *   {"op":"upcoming","now":"..."}     -> reminders still to come
 *   {"op":"snapshot"} / {"op":"restore","snap":{...}}  (persistence round trip)
 *   {"op":"helper","name":"goldenColor","calls":[[0],[1]]}  -> results of a helper
 * A request body {"$bytes": "<base64>"} is sent as raw bytes (image upload).
 */
'use strict';
const path = require('node:path');
const readline = require('node:readline');
const L = require(path.join(__dirname, '..', 'web', 'localdb.js'));

let clock = new Date(2026, 9, 6, 15, 0, 0);
const setNow = (s) => {
  if (!s) return;
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})$/.exec(s);
  clock = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5]), Number(m[6]));
};
const local = L.openMemory({ now: () => clock, build: 'test' });

const rl = readline.createInterface({ input: process.stdin });
let chain = Promise.resolve();
rl.on('line', (line) => {
  chain = chain.then(async () => {
    const cmd = JSON.parse(line);
    setNow(cmd.now);
    let out;
    if (cmd.op === 'request') {
      let body = cmd.body;
      if (body && typeof body === 'object' && '$bytes' in body) body = new Uint8Array(Buffer.from(body.$bytes, 'base64'));
      const res = await local.api.handle(cmd.method, cmd.path, body);
      out = { status: res.status, data: res.data };
    } else if (cmd.op === 'reminders') {
      out = { items: local.store.dueReminders(clock) };
    } else if (cmd.op === 'upcoming') {
      out = { items: local.store.upcomingReminders(cmd.limit) };
    } else if (cmd.op === 'helper') {
      out = { result: cmd.calls.map((args) => L._[cmd.name](...args)) };
    } else if (cmd.op === 'snapshot') {
      out = { snap: L.snapshot(local.store.data) };
    } else if (cmd.op === 'restore') {
      L.restore(local.store, JSON.parse(JSON.stringify(cmd.snap)));
      out = { ok: true };
    } else {
      out = { error: 'unknown op ' + cmd.op };
    }
    process.stdout.write(JSON.stringify(out) + '\n');
  }).catch((e) => {
    process.stdout.write(JSON.stringify({ crash: String(e && e.stack || e) }) + '\n');
  });
});
