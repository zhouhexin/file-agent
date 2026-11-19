const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const elements = new Map();
function element(selector) {
  if (!elements.has(selector)) {
    const classes = new Set(['hidden']);
    elements.set(selector, {
      textContent: '', innerHTML: '', value: '',
      attributes: {}, setAttribute(key, value) {this.attributes[key] = value;}, focus() {},
      elements: {employee_no: {readOnly: false}},
      classList: {add: c => classes.add(c), remove: c => classes.delete(c),
        contains: c => classes.has(c), toggle(c, force) {if (force) classes.add(c); else classes.delete(c);}},
      reset() {}, querySelector() {return {disabled: false};}
    });
  }
  return elements.get(selector);
}
const storage = new Map();
const context = vm.createContext({
  document: {querySelector: element},
  localStorage: {removeItem() {}},
  sessionStorage: {getItem: key => storage.get(key), setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k)},
  fetch: async url => ({ok: true, json: async () => url === '/api/me'
    ? {role: 'admin', name: '管理员'}
    : url.startsWith('/api/admin/staff') ? {total: 0, staff: []}
    : {active_staff_count: 1, registered_count: 1, sync: {enabled: true, message: '同步成功'}}}),
  console,
});

async function run() {
  const source = fs.readFileSync(path.join(__dirname, '../app/static/portal.js'), 'utf8');
  vm.runInContext(source, context);
  await vm.runInContext("token='admin-token';refresh()", context);
  assert.equal(element('#admin').classList.contains('hidden'), false);
  vm.runInContext("renderResearch({status:'READY',research_total:'100.00',filename:'source.xlsx',items:[{title:'<private>',note:''}]})", context);
  assert.ok(element('#researchItems').innerHTML.includes('&lt;private&gt;'));
  assert.ok(element('#researchItems').innerHTML.includes('data-label="论文/成果名称"'));
  assert.ok(element('#researchItems').innerHTML.includes('data-label="资助金额"'));
  assert.equal(vm.runInContext("tableCell('使用登记原文', '<long record>')", context),
    '<td data-label="使用登记原文">&lt;long record&gt;</td>');
  assert.ok(vm.runInContext("tableCell('核对说明', '待核对', true)", context).includes('class="review-cell"'));
  assert.ok(!element('#researchInfo').textContent.includes('source.xlsx'));
  vm.runInContext("fundingDates={total:'2026-11-06T12:34:56+00:00',balance:'2026-11-05T18:10:00Z'};selectFundingTab('total')", context);
  assert.equal(element('#totalView').classList.contains('hidden'), false);
  assert.equal(element('#balanceView').classList.contains('hidden'), true);
  assert.equal(element('#updated').textContent, '更新时间：2026年11月6日');
  vm.runInContext("selectFundingTab('balance')", context);
  assert.equal(element('#balanceView').classList.contains('hidden'), false);
  assert.equal(element('#totalView').classList.contains('hidden'), true);
  assert.equal(element('#balanceTab').attributes['aria-selected'], 'true');
  assert.equal(element('#updated').textContent, '更新时间：2026年11月5日');
  vm.runInContext("authMode(true)", context);
  assert.equal(element('#showRegister').attributes['aria-pressed'], 'true');
  element('#details').textContent = 'private teacher data';
  element('#metrics').textContent = 'private amount';
  vm.runInContext('logout()', context);
  assert.equal(element('#admin').classList.contains('hidden'), true);
  assert.equal(element('#staffAdmin').classList.contains('hidden'), true);
  assert.equal(element('#researchPanel').classList.contains('hidden'), true);
  assert.equal(element('#researchInfo').textContent, '');
  assert.equal(element('#account').classList.contains('hidden'), true);
  assert.equal(element('#fundingTabs').classList.contains('hidden'), true);
  assert.equal(element('#details').textContent, '');
  assert.equal(element('#metrics').textContent, '');
  assert.equal(element('#adminStatus').textContent, '');
  assert.equal(element('#auth').classList.contains('hidden'), false);
  vm.runInContext("token='teacher-token';refresh()", context);
  vm.runInContext('logout()', context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(element('#admin').classList.contains('hidden'), true);
  assert.equal(element('#account').classList.contains('hidden'), true);
  let requests = 0;
  let domReady;
  const stalePage = vm.createContext({
    document: {
      readyState: 'loading',
      querySelector: selector => selector.startsWith('#research') ? null : element(selector),
      addEventListener: (event, callback) => {if (event === 'DOMContentLoaded') domReady = callback;}
    },
    localStorage: {removeItem() {}},
    sessionStorage: {getItem: () => 'old-token', removeItem() {}},
    fetch: () => {requests++; throw Error('No API calls allowed for an incomplete page');},
    console,
  });
  vm.runInContext(source, stalePage);
  assert.equal(requests, 0);
  assert.equal(typeof domReady, 'function');
  assert.doesNotThrow(() => domReady());
  await vm.runInContext('refresh()', stalePage);
  assert.equal(requests, 0);
  assert.ok(element('#message').textContent.includes('Ctrl+F5'));
  assert.doesNotThrow(() => vm.runInContext('clearViews()', stalePage));
  assert.doesNotThrow(() => vm.runInContext('renderResearch({status:"READY"})', stalePage));
  console.log('Page checks passed: logout clears private views; in-flight responses do not restore them.');
  console.log('Stale page checks passed: DOM-ready initialization and missing-element handling.');
}
run().catch(error => {console.error(error); process.exitCode = 1;});
