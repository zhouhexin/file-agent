let token = sessionStorage.getItem('tf_token') || '';
let staffOffset = 0;
let staffEditing = false;
let staffEntries = [];
let activeFundingTab = 'total';
let fundingDates = {};
localStorage.removeItem('tf_token');
const $ = selector => document.querySelector(selector);
const REQUIRED_SELECTORS = [
  '#message', '#auth', '#loginForm', '#registerForm', '#showLogin', '#showRegister',
  '#account', '#admin', '#staffAdmin', '#detailPanel', '#researchPanel',
  '#fundingTabs', '#totalTab', '#balanceTab', '#totalView', '#balanceView', '#balanceMetrics',
  '#metrics', '#details', '#updated', '#welcome', '#fundingStatus', '#adminStatus', '#syncStatus',
  '#staffRows', '#staffPageInfo', '#researchInfo', '#researchItems', '#resetForm', '#staffForm',
  '#saveStaff', '#cancelStaffEdit', '#staffPrevious', '#staffNext', '#logout',
  '#fundingForm', '#rosterForm'
];
const message = (text, error = false) => {
  const target = $('#message');
  if (target) {
    target.textContent = text;
    target.className = error ? 'danger' : '';
  }
};
function ensurePageReady() {
  if (REQUIRED_SELECTORS.every(selector => $(selector))) return true;
  const text = '页面与当前程序版本不一致，请重启查询服务后，按 Ctrl+F5 刷新页面。';
  if ($('#message')) message(text, true);
  else if (document.body) {
    const banner = document.createElement('p');
    banner.textContent = text;
    document.body.prepend(banner);
  }
  return false;
}
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[c]));

async function api(path, options = {}) {
  options.headers = {...options.headers, ...(token ? {Authorization: 'Bearer ' + token} : {})};
  const response = await fetch(path, {...options, cache: 'no-store'});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请检查输入后重试。');
    error.status = response.status;
    throw error;
  }
  return data;
}

function clearViews() {
  for (const selector of ['#account', '#admin', '#staffAdmin', '#detailPanel', '#researchPanel', '#fundingTabs', '#totalView', '#balanceView']) {
    $(selector)?.classList.add('hidden');
  }
  for (const selector of ['#metrics', '#balanceMetrics', '#details', '#updated', '#welcome', '#fundingStatus', '#adminStatus', '#syncStatus', '#staffRows', '#staffPageInfo', '#researchInfo', '#researchItems']) {
    const element = $(selector);
    if (element) element.textContent = '';
  }
  $('#resetForm')?.reset();
  fundingDates = {};
  staffEntries = [];
  resetStaffForm();
}

function logout(text = '已退出登录') {
  token = '';
  staffOffset = 0;
  activeFundingTab = 'total';
  sessionStorage.removeItem('tf_token');
  clearViews();
  $('#auth').classList.remove('hidden');
  $('#loginForm').reset();
  message(text);
}

function authMode(register) {
  $('#loginForm').classList.toggle('hidden', register);
  $('#registerForm').classList.toggle('hidden', !register);
  $('#showLogin').classList.toggle('active', !register);
  $('#showRegister').classList.toggle('active', register);
  $('#showLogin').setAttribute('aria-pressed', String(!register));
  $('#showRegister').setAttribute('aria-pressed', String(register));
}

function money(value) {
  if (value === '待核对') return value;
  if (value == null) return '暂无可用数据';
  return Number(value).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' 元';
}

function metric(label, value, featured = false) {
  return `<div class="metric${featured ? ' featured' : ''}">${escapeHtml(label)}<strong>${escapeHtml(money(value))}</strong></div>`;
}

function tableCell(label, value, review = false) {
  return `<td data-label="${escapeHtml(label)}"${review ? ' class="review-cell"' : ''}>${escapeHtml(value)}</td>`;
}

function dateOnly(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})(?:[T\s]|$)/.exec(String(value || ''));
  return match ? `${match[1]}年${Number(match[2])}月${Number(match[3])}日` : '';
}

function selectFundingTab(tab, focus = false) {
  activeFundingTab = tab === 'balance' ? 'balance' : 'total';
  for (const [name, buttonId, viewId] of [['total', '#totalTab', '#totalView'], ['balance', '#balanceTab', '#balanceView']]) {
    const selected = name === activeFundingTab;
    $(buttonId).classList.toggle('active', selected);
    $(buttonId).setAttribute('aria-selected', String(selected));
    $(buttonId).setAttribute('tabindex', selected ? '0' : '-1');
    $(viewId).classList.toggle('hidden', !selected);
    if (selected && focus) $(buttonId).focus();
  }
  const date = dateOnly(fundingDates[activeFundingTab]);
  $('#updated').textContent = date ? `更新时间：${date}` : '更新时间：暂无';
}

async function refresh() {
  if (!ensurePageReady()) return;
  clearViews();
  if (!token) { $('#auth').classList.remove('hidden'); return; }
  const activeToken = token;
  try {
    const me = await api('/api/me');
    if (token !== activeToken) return;
    $('#auth').classList.add('hidden');
    $('#account').classList.remove('hidden');
    $('#welcome').textContent = me.role === 'admin' ? '管理员' : `${me.name}（工号 ${me.employee_no}）`;
    if (me.role === 'admin') {
      const status = await api('/api/admin/status');
      if (token !== activeToken) return;
      $('#admin').classList.remove('hidden');
      $('#adminStatus').textContent = `有效教师 ${status.active_staff_count} 人 · 已注册 ${status.registered_count} 人`;
      const date = dateOnly(status.funding_import?.imported_at);
      $('#updated').textContent = date ? `更新时间：${date}` : '更新时间：暂无';
      $('#syncStatus').textContent = status.sync.enabled
        ? `自动同步：${status.sync.message || '等待首次同步'}`
        : '自动同步未开启，可手动导入余额表';
      $('#syncStatus').textContent += `；资助总额明细：${status.research_sync?.message || '等待首次同步'}`;
      $('#fundingForm').classList.toggle('hidden', status.sync.enabled);
      await loadStaff();
      if (token !== activeToken) return;
      $('#staffAdmin').classList.remove('hidden');
      return;
    }
    const data = await api('/api/my/funding');
    if (token !== activeToken) return;
    fundingDates = {total: data.research?.updated_at || data.updated_at, balance: data.updated_at};
    $('#fundingTabs').classList.remove('hidden');
    selectFundingTab(activeFundingTab);
    renderResearch(data.research);
    if (data.status === 'NO_FUNDING_DATA' || data.status === 'NO_FUNDING_RECORD') {
      $('#fundingStatus').textContent = data.message;
      $('#metrics').innerHTML = metric('科研成果资助金额', data.research?.research_total)
        + metric('其他资助金额', null) + metric('总资助金额', null, true);
      $('#balanceMetrics').innerHTML = metric('剩余资助金额', null, true)
        + metric('累计支出', null) + metric('总资助金额', null);
      return;
    }
    if (data.needs_review) {
      $('#fundingStatus').textContent = '存在待核对记录或金额不一致，余额待核对。';
    }
    if (data.sync_status === 'FAILED') {
      $('#fundingStatus').textContent += ' 最新数据同步暂未完成，当前显示最近一次成功同步的数据。';
    }
    $('#metrics').innerHTML = metric('科研成果资助金额', data.research_total)
      + metric('其他资助金额', data.other_total) + metric('总资助金额', data.total_funding, true);
    $('#balanceMetrics').innerHTML = metric('剩余资助金额', data.needs_review ? '待核对' : data.remaining, true)
      + metric('累计支出', data.total_spent) + metric('总资助金额', data.total_funding);
    $('#details').innerHTML = data.details.map(row => '<tr>' + tableCell('序号', row.sequence)
      + tableCell('使用登记原文', row.registration) + tableCell('日期', row.date)
      + tableCell('确认使用金额', money(row.usage_amount), row.needs_review)
      + tableCell('核对说明', (row.needs_review ? '待核对；' : '') + row.note, row.needs_review) + '</tr>').join('')
      || '<tr><td colspan="5">暂无使用登记</td></tr>';
    $('#detailPanel').classList.remove('hidden');
  } catch (error) {
    if (token !== activeToken) return;
    if (error.status === 401) logout(error.message);
    else message(error.message, true);
  }
}

function renderResearch(data) {
  if (!ensurePageReady()) return;
  $('#researchPanel').classList.remove('hidden');
  if (!data || data.status === 'NOT_SYNCED' || data.status === 'NOT_FOUND') {
    $('#researchInfo').textContent = data?.message || '资助总额明细尚未同步。';
    return;
  }
  $('#researchInfo').textContent = (data.message || '')
    + (data.sync_status === 'FAILED' ? '最新资助明细同步未完成，当前显示上次成功同步的数据。' : '')
    + '其他资助目前按余额表中的合计金额展示，逐项来源尚未记录。';
  const labels = ['序号', '年份', '成果类型', '期刊/会议', '论文/成果名称', '论文类型', '资助类型',
    '发表/召开时间', '资助类别', '资助金额', '地点', '备注'];
  $('#researchItems').innerHTML = data.items.map(row => '<tr>' + [row.sequence, row.year, row.kind, row.venue,
    row.title, row.paper_type, row.funding_type, row.published_at, row.category, money(row.amount), row.location,
    (row.needs_review ? '待核对；' : '') + row.note].map((value, index) => tableCell(labels[index], value,
      Boolean(row.needs_review) && (index === 9 || index === 11))).join('') + '</tr>').join('')
    || '<tr><td colspan="12">暂无逐项资助明细。</td></tr>';
}

async function submitJson(path, form) {
  return api(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.fromEntries(new FormData(form)))});
}

function resetStaffForm() {
  staffEditing = false;
  const form = $('#staffForm');
  if (form) {
    form.reset();
    form.elements.employee_no.readOnly = false;
  }
  const button = $('#saveStaff');
  if (button) button.textContent = '新增教师';
}

async function loadStaff() {
  const activeToken = token;
  const data = await api('/api/admin/staff?offset=' + staffOffset + '&limit=50');
  if (token !== activeToken) return;
  staffEntries = data.staff;
  $('#staffRows').innerHTML = data.staff.map((person, index) => '<tr>' + tableCell('工号', person.employee_no)
    + tableCell('姓名', person.name) + tableCell('状态', person.active ? '启用' : '停用')
    + tableCell('注册情况', person.registered ? '已注册' : '未注册')
    + `<td data-label="操作"><button type="button" data-index="${index}">编辑</button></td></tr>`).join('')
    || '<tr><td colspan="5">暂无教师，请在上方新增。</td></tr>';
  $('#staffPageInfo').textContent = `共 ${data.total} 位教师；当前第 ${Math.floor(staffOffset / 50) + 1} 页`;
  $('#staffPrevious').disabled = staffOffset === 0;
  $('#staffNext').disabled = staffOffset + data.staff.length >= data.total;
}

function initializePage() {
  if (!ensurePageReady()) return;
  for (const [selector, tab] of [['#totalTab', 'total'], ['#balanceTab', 'balance']]) {
    $(selector).onclick = () => selectFundingTab(tab);
    $(selector).onkeydown = event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      selectFundingTab(event.key === 'Home' ? 'total' : event.key === 'End' ? 'balance' : activeFundingTab === 'total' ? 'balance' : 'total', true);
    };
  }
  $('#cancelStaffEdit').onclick = resetStaffForm;
  $('#staffRows').onclick = event => {
    const button = event.target.closest('button[data-index]');
    if (!button) return;
    const person = staffEntries[Number(button.dataset.index)];
    if (!person) return;
    const fields = $('#staffForm').elements;
    fields.employee_no.value = person.employee_no;
    fields.employee_no.readOnly = true;
    fields.name.value = person.name;
    fields.active.value = String(person.active);
    staffEditing = true;
    $('#saveStaff').textContent = '保存修改';
  };
  $('#staffForm').onsubmit = async event => {
    event.preventDefault();
    const fields = event.target.elements;
    const payload = {employee_no: fields.employee_no.value, name: fields.name.value, active: fields.active.value === 'true'};
    $('#saveStaff').disabled = true;
    try {
      const result = await api('/api/admin/staff', {method: staffEditing ? 'PUT' : 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      message(result.message);
      await refresh();
    } catch (error) { message(error.message, true); }
    finally { $('#saveStaff').disabled = false; }
  };
  $('#staffPrevious').onclick = async () => {
    staffOffset = Math.max(0, staffOffset - 50);
    try { await loadStaff(); } catch (error) { message(error.message, true); }
  };
  $('#staffNext').onclick = async () => {
    staffOffset += 50;
    try { await loadStaff(); } catch (error) { message(error.message, true); }
  };

  $('#showLogin').onclick = () => authMode(false);
  $('#showRegister').onclick = () => authMode(true);
  $('#logout').onclick = () => logout();
  $('#loginForm').onsubmit = async event => {
    event.preventDefault();
    try {
      const result = await submitJson('/api/login', event.target);
      token = result.access_token;
      sessionStorage.setItem('tf_token', token);
      event.target.reset();
      message('登录成功');
      await refresh();
    } catch (error) { message(error.message, true); }
  };
  $('#registerForm').onsubmit = async event => {
    event.preventDefault();
    if (event.target.password.value !== event.target.confirm_password.value) {
      message('两次输入的密码不一致。', true); return;
    }
    try {
      const result = await submitJson('/api/register', event.target);
      event.target.reset();
      message(`${result.message} 对应姓名：${result.name}`);
      authMode(false);
    } catch (error) { message(error.message, true); }
  };
  for (const [selector, path] of [['#rosterForm', '/api/admin/roster/import'], ['#fundingForm', '/api/admin/funding/import']]) {
    $(selector).onsubmit = async event => {
      event.preventDefault();
      const button = event.target.querySelector('button');
      button.disabled = true;
      try {
        const result = await api(path, {method: 'POST', body: new FormData(event.target)});
        message(result.message); event.target.reset(); await refresh();
      } catch (error) { message(error.message, true); }
      finally { button.disabled = false; }
    };
  }
  $('#resetForm').onsubmit = async event => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(event.target));
    try {
      const result = await api('/api/admin/users/' + encodeURIComponent(data.employee_no) + '/reset-password', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({new_password: data.new_password})
      });
      message(result.message); event.target.reset();
    } catch (error) { message(error.message, true); }
  };
  refresh();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initializePage, {once: true});
} else {
  initializePage();
}
