// Unit tests for the pure front-end code. Run: node --test tests/parsers.test.js
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const P = require(path.join(__dirname, '..', 'web', 'parse.js'));
const MD = require(path.join(__dirname, '..', 'web', 'markdown.js'));

// Tuesday 6 October 2026, 15:00 local time.
const NOW = new Date(2026, 9, 6, 15, 0, 0);

test('parseDue: words, weekdays, offsets', () => {
  const cases = {
    today: '2026-10-06', tod: '2026-10-06', TODAY: '2026-10-06',
    tomorrow: '2026-10-07', tom: '2026-10-07',
    mon: '2026-10-12', tue: '2026-10-13', wed: '2026-10-07', sun: '2026-10-11',
    friday: '2026-10-09',
    '+3d': '2026-10-09', '+2w': '2026-10-20', '+0d': '2026-10-06',
  };
  for (const [spec, want] of Object.entries(cases)) assert.equal(P.parseDue(spec, NOW), want, spec);
});

test('parseDue: absolute and German dates', () => {
  assert.equal(P.parseDue('2026-10-01', NOW), '2026-10-01');
  assert.equal(P.parseDue('2027-1-5', NOW), '2027-01-05');
  assert.equal(P.parseDue('24.12.', NOW), '2026-12-24');
  assert.equal(P.parseDue('24.12', NOW), '2026-12-24');
  assert.equal(P.parseDue('24.12.2026', NOW), '2026-12-24');
  assert.equal(P.parseDue('1.3.', NOW), '2027-03-01', 'past date without year means next year');
  assert.equal(P.parseDue('6.10.', NOW), '2026-10-06', 'today is not in the past');
  assert.equal(P.parseDue('5.10.', NOW), '2027-10-05');
  assert.equal(P.parseDue('29.2.', NOW), null, '2026 and 2027 have no 29 Feb');
});

test('parseDue: times and invalid input', () => {
  assert.equal(P.parseDue('fri@14:30', NOW), '2026-10-09T14:30');
  assert.equal(P.parseDue('tom@9', NOW), '2026-10-07T09:00');
  assert.equal(P.parseDue('@14:30', NOW), '2026-10-06T14:30');
  assert.equal(P.parseDue('24.12.@8:05', NOW), '2026-12-24T08:05');
  for (const bad of ['', 'foo', '31.02.', '2026-13-01', 'fri@25:00', 'fri@14:60', 'fri@', '+3x', '32.1.']) {
    assert.equal(P.parseDue(bad, NOW), null, JSON.stringify(bad));
  }
});

test('parseQuickAdd: tokens become fields', () => {
  const r = P.parseQuickAdd('Buy milk #home !h ^tom', { now: NOW });
  assert.deepEqual(r, { title: 'Buy milk', labels: ['home'], priority: 'high', due: '2026-10-07', errors: [] });
});

test('parseQuickAdd: what is not a token stays in the title', () => {
  let r = P.parseQuickAdd('#3 is not a label', { now: NOW });
  assert.equal(r.title, '#3 is not a label');
  assert.deepEqual(r.labels, []);
  r = P.parseQuickAdd('C# rocks #dev.', { now: NOW });
  assert.equal(r.title, 'C# rocks');
  assert.deepEqual(r.labels, ['dev']);
  r = P.parseQuickAdd('Call ^nonsense now', { now: NOW });
  assert.equal(r.title, 'Call ^nonsense now');
  assert.deepEqual(r.errors, ['^nonsense']);
  assert.equal(r.due, null);
  r = P.parseQuickAdd('!important stays !l', { now: NOW });
  assert.equal(r.title, '!important stays');
  assert.equal(r.priority, 'low');
  r = P.parseQuickAdd('!med task !L', { now: NOW });
  assert.equal(r.priority, 'low', 'last priority wins');
  assert.equal(P.parseQuickAdd('x !m', { now: NOW }).priority, 'medium');
});

test('parseQuickAdd: labels keep the existing spelling and are unique', () => {
  const r = P.parseQuickAdd('x #Home #home #NEW', { now: NOW, labels: ['HOME'] });
  assert.deepEqual(r.labels, ['HOME', 'NEW']);
  assert.equal(r.title, 'x');
  assert.deepEqual(P.parseQuickAdd('#a,b', { now: NOW }).labels, []);
  assert.deepEqual(P.parseQuickAdd('#' + 'x'.repeat(41), { now: NOW }).labels, []);
});

test('parseSubtaskTitle: only the task labels, plus dates', () => {
  const r = P.parseSubtaskTitle('Call Bob #work #other ^fri@9:15', ['Work'], NOW);
  assert.deepEqual(r, { title: 'Call Bob #other', labels: ['Work'], due: '2026-10-09T09:15' });
  assert.deepEqual(P.parseSubtaskTitle('Plain #3', [], NOW), { title: 'Plain #3', labels: [], due: null });
});

test('labelQueryAt and suggestLabels', () => {
  assert.deepEqual(P.labelQueryAt('Buy #ho', 7), { start: 4, end: 7, prefix: 'ho' });
  assert.deepEqual(P.labelQueryAt('Buy #ho milk', 6), { start: 4, end: 7, prefix: 'h' });
  assert.equal(P.labelQueryAt('Buy ho', 6), null);
  assert.equal(P.labelQueryAt('#3', 2), null);
  assert.deepEqual(P.labelQueryAt('#', 1), { start: 0, end: 1, prefix: '' });
  const names = ['home', 'Homework', 'shop', 'phone', 'work'];
  assert.deepEqual(P.suggestLabels('ho', names), ['home', 'Homework', 'phone', 'shop']);
  assert.deepEqual(P.suggestLabels('ho', names, ['HOME']), ['Homework', 'phone', 'shop']);
  assert.deepEqual(P.suggestLabels('', names, [], 2), ['home', 'Homework']);
});

test('splitPastedLines: one subtask per line', () => {
  const text = '- milk\n- [ ] eggs\n- [x] bread\n3.5 kg flour\n-v flag\n\n---\n* * *\n- \n-\n- [ ]\n'
    + '1. step one\r\nfoo\rbar\u2028baz\u2029qux\n  *   spaced   out  \n[x] checked';
  assert.deepEqual(P.splitPastedLines(text), [
    { title: 'milk', done: false },
    { title: 'eggs', done: false },
    { title: 'bread', done: true },
    { title: '3.5 kg flour', done: false },
    { title: '-v flag', done: false },
    { title: 'step one', done: false },
    { title: 'foo', done: false },
    { title: 'bar', done: false },
    { title: 'baz', done: false },
    { title: 'qux', done: false },
    { title: 'spaced out', done: false },
    { title: 'checked', done: true },
  ]);
  assert.deepEqual(P.splitPastedLines('[x]no-space\n1.5 litres'), [
    { title: '[x]no-space', done: false }, { title: '1.5 litres', done: false }]);
});

test('date labels, states and groups', () => {
  assert.equal(P.dueLabel('2026-10-06', NOW), 'Today');
  assert.equal(P.dueLabel('2026-10-07T09:00', NOW), 'Tomorrow 09:00');
  assert.equal(P.dueLabel('2026-10-09', NOW), 'Fri');
  assert.equal(P.dueLabel('2026-10-12', NOW), 'Mon');
  assert.equal(P.dueLabel('2026-10-13', NOW), '13 Oct');
  assert.equal(P.dueLabel('2026-10-05', NOW), '5 Oct');
  assert.equal(P.dueLabel('2027-01-16', NOW), '16 Jan 2027');
  assert.equal(P.dueState('2026-10-05', NOW), 'overdue');
  assert.equal(P.dueState('2026-10-06', NOW), 'today');
  assert.equal(P.dueState('2026-10-06T14:00', NOW), 'overdue');
  assert.equal(P.dueState('2026-10-06T16:00', NOW), 'today');
  assert.equal(P.dueState('2026-10-07', NOW), 'future');
  assert.equal(P.dueGroup('2026-10-01', NOW), 'overdue');
  assert.equal(P.dueGroup('2026-10-06', NOW), 'today');
  assert.equal(P.dueGroup('2026-10-12', NOW), 'week');
  assert.equal(P.dueGroup('2026-10-13', NOW), 'later');
  assert.equal(P.dueGroup(null, NOW), 'none');
  assert.equal(P.stampLabel('2026-10-06T08:15:00', NOW), 'Today 08:15');
  assert.equal(P.stampLabel('2026-10-05T23:59:00', NOW), 'Yesterday 23:59');
  assert.equal(P.stampLabel('2025-12-24T10:00:00', NOW), '24 Dec 2025 10:00');
});

// ---- Markdown ------------------------------------------------------------

test('markdown escapes HTML first', () => {
  assert.equal(MD.render('<script>alert(1)</script>'), '<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>');
  assert.equal(MD.render('a & b " \''), '<p>a &amp; b &quot; &#39;</p>');
  const html = MD.render('[x](https://a.com/"onmouseover="alert(1))');
  assert.ok(!/onmouseover="/.test(html), html);
  assert.ok(!MD.render('<img src=x onerror=alert(1)>').includes('<img'));
});

test('markdown emphasis follows the closing rule', () => {
  assert.equal(MD.render('*.pyc and *.log'), '<p>*.pyc and *.log</p>');
  assert.equal(MD.render('snake_case_name and a__b__c'), '<p>snake_case_name and a__b__c</p>');
  // CommonMark reads __init__.py as strong "init" followed by ".py".
  assert.equal(MD.render('__init__.py'), '<p><strong>init</strong>.py</p>');
  assert.equal(MD.render('**bold** *it* _it_ ~~gone~~'),
    '<p><strong>bold</strong> <em>it</em> <em>it</em> <del>gone</del></p>');
  assert.equal(MD.render('***both***'), '<p><strong><em>both</em></strong></p>');
  assert.equal(MD.render('2 * 3 * 4'), '<p>2 * 3 * 4</p>');
  assert.equal(MD.render('`a_b_c *x*`'), '<p><code>a_b_c *x*</code></p>');
});

test('markdown links and images', () => {
  assert.equal(MD.render('[a](https://x.com/a_b_c)'),
    '<p><a href="https://x.com/a_b_c" target="_blank" rel="noopener noreferrer">a</a></p>');
  assert.equal(MD.render('see https://x.com/_a_b_ now.'),
    '<p>see <a href="https://x.com/_a_b_" target="_blank" rel="noopener noreferrer">https://x.com/_a_b_</a> now.</p>');
  assert.equal(MD.render('(https://en.wikipedia.org/wiki/Foo_(bar))'),
    '<p>(<a href="https://en.wikipedia.org/wiki/Foo_(bar)" target="_blank" rel="noopener noreferrer">https://en.wikipedia.org/wiki/Foo_(bar)</a>)</p>');
  assert.equal(MD.render('[x](javascript:alert(1))'), '<p>[x](javascript:alert(1))</p>');
  assert.equal(MD.render('[mail](mailto:a@b.c)'),
    '<p><a href="mailto:a@b.c" target="_blank" rel="noopener noreferrer">mail</a></p>');
  assert.equal(MD.render('![shot](/images/0123456789abcdef0123456789abcdef.png)'),
    '<p><img src="/images/0123456789abcdef0123456789abcdef.png" alt="shot" loading="lazy"></p>');
  assert.equal(MD.render('![x](data:image/png;base64,AAAA)'), '<p>![x](data:image/png;base64,AAAA)</p>');
  assert.equal(MD.render('![x](mailto:a@b.c)'), '<p>![x](mailto:a@b.c)</p>');
  assert.ok(MD.render('<https://a.com>').includes('href="https://a.com"'));
  assert.equal(MD.render('[**b** _i_](https://a.com)'),
    '<p><a href="https://a.com" target="_blank" rel="noopener noreferrer"><strong>b</strong> <em>i</em></a></p>');
});

test('markdown blocks', () => {
  assert.equal(MD.render('# One\n## Two\n### Three\n#### Four'),
    '<h1>One</h1>\n<h2>Two</h2>\n<h3>Three</h3>\n<p>#### Four</p>');
  assert.equal(MD.render('line one\nline two\n\nnext'), '<p>line one<br>line two</p>\n<p>next</p>');
  assert.equal(MD.render('- a\n- b\n  - nested\n* c'), '<ul><li>a</li><li>b<ul><li>nested</li></ul></li><li>c</li></ul>');
  assert.equal(MD.render('3. three\n4. four'), '<ol start="3"><li>three</li><li>four</li></ol>');
  assert.equal(MD.render('- [ ] todo\n- [x] done'),
    '<ul><li class="md-check"><input type="checkbox" disabled tabindex="-1" aria-label="not done"> todo</li>'
    + '<li class="md-check done"><input type="checkbox" disabled checked tabindex="-1" aria-label="done"> done</li></ul>');
  assert.equal(MD.render('> quoted **text**\n> more'), '<blockquote><p>quoted <strong>text</strong><br>more</p></blockquote>');
  assert.equal(MD.render('a\n\n---\n\n* * *'), '<p>a</p>\n<hr>\n<hr>');
  assert.equal(MD.render('```js\nlet a = **b** < c;\n```\nafter'),
    '<pre><code>let a = **b** &lt; c;</code></pre>\n<p>after</p>');
  assert.equal(MD.render('~~~\n# not heading\n~~~'), '<pre><code># not heading</code></pre>');
  assert.equal(MD.render('3.5 kg flour'), '<p>3.5 kg flour</p>');
  assert.equal(MD.render(''), '');
  assert.equal(MD.render('x\uE0000\uE001y'), '<p>x0y</p>', 'placeholder characters from input are removed');
  assert.equal(MD.render('**a\uE0000\uE001**'), '<p><strong>a0</strong></p>');
});

test('stripEmphasis reads emphasis like the renderer', () => {
  assert.equal(MD.stripEmphasis('**Buy** milk'), 'Buy milk');
  assert.equal(MD.stripEmphasis('*.pyc and *.log'), '*.pyc and *.log');
  assert.equal(MD.stripEmphasis('`a_b_` and _em_ ~~x~~'), '`a_b_` and em x');
  assert.equal(MD.stripEmphasis('see https://x.com/_a_ now'), 'see https://x.com/_a_ now');
  assert.equal(MD.stripEmphasis('[lnk](http://a.com/_x_) _y_'), '[lnk](http://a.com/_x_) y');
  assert.equal(MD.stripEmphasis('snake_case_name'), 'snake_case_name');
});

test('checklist conversion finds lines outside code and removes exactly them', () => {
  const desc = 'Intro\n\n- [ ] one\n    - [x] **two**\n```\n- [ ] in code\n```\n1. [ ] three\n- not a check\n\n\nend  ';
  const items = MD.findChecklist(desc);
  assert.deepEqual(items.map((i) => [i.line, i.title, i.done]), [[2, 'one', false], [3, 'two', true], [7, 'three', false]]);
  assert.equal(MD.removeLines(desc, items), 'Intro\n\n```\n- [ ] in code\n```\n- not a check\n\n\nend  ');
  // If the description changed meanwhile, identical lines are still found.
  const changed = 'New first line\n' + desc;
  assert.equal(MD.removeLines(changed, items), 'New first line\nIntro\n\n```\n- [ ] in code\n```\n- not a check\n\n\nend  ');
  assert.deepEqual(MD.findChecklist('- [ ]   \n- [x]'), [], 'empty items are ignored');
  assert.deepEqual(MD.findChecklist('~~~\n- [ ] a\n~~~\n- [ ] b').map((i) => i.title), ['b']);
});
