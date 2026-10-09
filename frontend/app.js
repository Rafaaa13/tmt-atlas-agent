const state = {
  view: 'overview',
  config: null,
  companyId: 'nvidia',
  sector: 'AI基础设施/半导体',
  events: [],
  selectedEventId: null,
  selectedEventAnalysis: null,
  searchTimer: null,
  memo: null,
  settings: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.message || payload.error || `请求失败：${response.status}`);
  return payload;
}

function esc(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

function initial(text) {
  return String(text || '').slice(0, 2).toUpperCase();
}

function showToast(message) {
  const toast = $('#toast');
  toast.textContent = message;
  toast.classList.add('show');
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove('show'), 2600);
}

function setLoading(message = '正在加载研究工作区…') {
  $('#content').innerHTML = `<div class="loading">${esc(message)}</div>`;
}

function setError(error) {
  $('#content').innerHTML = `<div class="error-box"><strong>页面加载失败</strong><br>${esc(error.message || error)}</div>`;
}

function pageHead(kicker, title, description, action = '') {
  return `<div class="page-head"><div><div class="page-kicker">${esc(kicker)}</div><h1 class="page-title">${esc(title)}</h1><p class="page-desc">${esc(description)}</p></div>${action}</div>`;
}

function pill(text, type = 'demo') { return `<span class="pill ${type}">${esc(text)}</span>`; }

const METRIC_STATUS_LABELS = {
  direct: '官方直接披露',
  derived: '官方事实派生',
  missing: '未披露',
};

const METRIC_MISSING_REASONS = {
  official_fact_unavailable: '官方语料中暂无该字段',
  requires_revenue_prior_value: '官方来源未提供可核验的上期收入',
  requires_research_and_development_and_revenue: '缺少同口径的研发费用或收入',
  requires_gross_profit_and_revenue: '缺少同口径的毛利或收入',
  requires_operating_income_and_revenue: '缺少同口径的营业利润或收入',
  requires_direct_free_cash_flow_or_explicit_ocf_capex_derivation: '仅有现金流与资本开支，但未获准派生自由现金流',
  inputs_not_comparable: '派生输入的币种、期间、口径或合并范围不一致',
  denominator_zero: '派生计算的分母为 0',
};

function metricStatusLabel(metric) {
  if (metric?.status === 'derived') return '官方事实派生';
  if (metric?.status === 'direct' && metric?.fact_status === 'verified_non_gaap') return '官方直接披露 · 非 GAAP/IFRS';
  return METRIC_STATUS_LABELS[metric?.status] || '待审核';
}

function metricStatusType(metric) {
  if (metric?.status === 'direct') return 'source';
  if (metric?.status === 'derived') return 'derived';
  if (metric?.status === 'missing') return 'missing';
  return 'medium';
}

function metricMissingReason(metric) {
  return METRIC_MISSING_REASONS[metric?.missing_reason] || metric?.missing_reason || '暂无可用官方事实';
}

function comparabilityLabel(value) {
  return ({ eligible: '可比较并排名', not_comparable: '不可比较', missing: '未披露', pending: '待审核' })[value] || '待审核';
}

function agentStatusLabel(value) {
  return ({
    draft: '待确认', approved: '已批准', active: '运行中', paused: '已暂停',
    running: '执行中', succeeded: '已完成', succeeded_noop: '无新结果',
    waiting_review: '等待审核', blocked: '已阻止', failed: '失败',
    pending: '待审核', accepted: '已接受', rejected: '已拒绝',
  })[value] || value || '未知';
}

function agentStatusType(value) {
  return ({
    draft: 'medium', approved: 'source', active: 'source', running: 'medium',
    succeeded: 'source', succeeded_noop: 'demo', waiting_review: 'medium',
    blocked: 'high', failed: 'high', pending: 'medium', accepted: 'source', rejected: 'high',
  })[value] || 'demo';
}

function officialDataBadge() {
  return pill('官方历史事实', 'official');
}


function updateHeader(label) {
  $('#breadcrumb-current').textContent = label;
  $$('.nav-item').forEach(item => item.classList.toggle('active', item.dataset.view === state.view));
  const enabled = Boolean(state.settings?.allow_external_llm);
  $('.llm-status').classList.toggle('enabled', enabled);
  $('#llm-status-label').textContent = enabled ? 'LLM 已开启' : 'LLM 未启用';
}

async function boot() {
  try {
    state.config = await api('/api/config');
    state.events = (await api('/api/events')).events;
    state.settings = state.config.llm || {};
    state.sector = state.config.sectors[0] || state.sector;
    renderView();
  } catch (error) { setError(error); }
}

function renderView() {
  const label = ({ overview: '研究总览', company: '公司快照', sector: '赛道扫描', events: '事件雷达', memo: '研究备忘录', sources: '来源与设置', evidence: '证据审核', finance: '金融计算', automation: '授权收集', agent: 'Agent 自动化' })[state.view];
  updateHeader(label);
  if (!state.config) return;
  if (state.view === 'overview') renderOverview();
  if (state.view === 'company') renderCompany();
  if (state.view === 'sector') renderSector();
  if (state.view === 'events') renderEvents();
  if (state.view === 'memo') renderMemo();
  if (state.view === 'sources') renderSources();
  if (state.view === 'evidence') window.TMTResearch?.renderEvidence();
  if (state.view === 'finance') window.TMTResearch?.renderFinance();
  if (state.view === 'automation') window.TMTResearch?.renderAutomation();
  if (state.view === 'agent') window.TMTResearch?.renderAgent();
}

function renderOverview() {
  const companies = state.config.companies;
  const events = state.events.slice(0, 5);
  const prompts = state.config.prompts || [];
  $('#content').innerHTML = `
    ${pageHead('TMT ATLAS · RESEARCH WORKBENCH', '把研究问题，变成可追溯的判断。', '一个专注于 TMT 研究的工作台：在同一个本地工作区里组织官方材料、经营指标、事件线索和带来源的研究备忘录。')}
    <section class="hero">
      <div class="hero-eyebrow">官方事实 · 证据优先 · LLM 可选</div>
      <h2>先看证据，<br>再形成观点。</h2>
      <p>无论你在做公司快照、赛道扫描还是事件分析，TMT Atlas 都把“来源 → 事实 → 计算 → 推断 → 备忘录”串成一条可以复核的研究链。</p>
      <div class="hero-search search-wrap">
        <input id="global-search" aria-label="搜索公司、事件或来源" placeholder="搜索公司、事件或来源，例如：Azure、资本开支…">
        <button id="search-button">开始搜索</button>
        <div id="search-results" class="search-results" hidden></div>
      </div>
      <div class="hero-meta"><span>◉ ${companies.length} 家研究对象</span><span>◌ ${events.length} 条事件线索</span><span>⌁ 官方历史事实 · 不做币种转换</span></div>
    </section>
    <div class="kpi-grid">
      <div class="kpi"><div class="kpi-label">研究对象</div><div class="kpi-value">${companies.length}</div><div class="kpi-note">中美 TMT 公司 · <strong>官方历史语料</strong></div></div>
      <div class="kpi"><div class="kpi-label">官方事实</div><div class="kpi-value">${state.config.dataset?.official_fact_count ?? '—'}</div><div class="kpi-note">带来源 URL 与定位说明</div></div>
      <div class="kpi"><div class="kpi-label">事件线索</div><div class="kpi-value">${state.events.length}</div><div class="kpi-note">按来源和影响等级组织</div></div>
      <div class="kpi"><div class="kpi-label">外部模型</div><div class="kpi-value">可选</div><div class="kpi-note">默认关闭 · <strong>证据不外传</strong></div></div>
    </div>
    <div class="official-banner"><div>${officialDataBadge()} <strong>当前页面已切换为官方历史事实模式。</strong> 数字来自本地来源语料；派生值会展示公式，缺失字段保持“未披露”。</div><button class="link-button" data-view-action="automation">查看 Agent 工作流 →</button></div>
    <div class="section-head"><div><h2 class="section-title">从一个研究问题开始</h2><p class="section-subtitle">常用入口已经预置，点击后直接进入相应工作流。</p></div><button class="link-button" data-view-action="sources">查看数据边界 →</button></div>
    <div class="prompt-grid">${prompts.slice(0, 6).map(prompt => `<button class="prompt-card" data-prompt="${esc(prompt.id)}"><div class="prompt-label">${esc(prompt.label)}</div><h4>${esc(prompt.prompt.replaceAll('{company}', '目标公司').replaceAll('{sector}', '目标赛道'))}</h4><p>进入专门工作流 →</p></button>`).join('')}</div>
    <div class="section-head"><div><h2 class="section-title">最近研究线索</h2><p class="section-subtitle">事件先进入审阅区，不会自动改写已确认事实。</p></div><button class="link-button" data-view-action="events">打开事件雷达 →</button></div>
    <div class="card"><div class="event-list">${events.map(eventRow).join('')}</div></div>
    <div class="section-head"><div><h2 class="section-title">公司地图</h2><p class="section-subtitle">从公司快照进入，再进入赛道横向比较。</p></div><button class="link-button" data-view-action="company">打开公司快照 →</button></div>
    <div class="card"><div class="company-strip">${companies.slice(0, 6).map(companyMini).join('')}</div></div>
  `;
  bindOverviewEvents();
}

function companyMini(company) {
  return `<button class="company-mini" data-company="${esc(company.id)}"><div><div class="company-mini-name">${esc(company.name_zh)} <span style="color:#9aa8a4;font-weight:500">${esc(company.name)}</span></div><div class="company-mini-meta">${esc(company.sector)} · ${esc(company.ticker)}</div></div><div class="company-mini-score">官方事实 →</div></button>`;
}

function eventRow(event) {
  const impactClass = event.impact === '高' ? 'high' : 'medium';
  return `<button class="event-row" data-event="${esc(event.id)}"><div class="event-date">${esc(event.date)}</div><div><div class="event-title">${esc(event.title)}</div><div class="event-summary">${esc(event.company_name_zh)} · ${esc(event.summary)}</div></div><div>${pill(event.impact === '高' ? '高影响' : '中影响', impactClass)}</div></button>`;
}

function bindOverviewEvents() {
  $('#global-search').addEventListener('input', e => runSearch(e.target.value));
  $('#search-button').addEventListener('click', () => runSearch($('#global-search').value, true));
  $$('[data-company]').forEach(button => button.addEventListener('click', () => { state.companyId = button.dataset.company; state.view = 'company'; renderView(); }));
  $$('[data-event]').forEach(button => button.addEventListener('click', () => { state.selectedEventId = button.dataset.event; state.view = 'events'; renderView(); }));
  $$('[data-view-action]').forEach(button => button.addEventListener('click', () => { state.view = button.dataset.viewAction; renderView(); }));
  $$('[data-prompt]').forEach(button => button.addEventListener('click', () => handlePrompt(button.dataset.prompt)));
}

function runSearch(value, focus = false) {
  const panel = $('#search-results');
  if (!panel) return;
  window.clearTimeout(state.searchTimer);
  if (!value.trim()) { panel.hidden = true; return; }
  state.searchTimer = window.setTimeout(async () => {
    try {
      const result = await api(`/api/search?q=${encodeURIComponent(value)}`);
      const items = [...result.companies, ...result.events, ...result.sources];
      panel.innerHTML = items.length ? items.map(item => `<button class="search-result" data-search-type="${esc(item.type)}" data-search-id="${esc(item.id)}"><span><span class="search-result-title">${esc(item.name_zh || item.title || item.company || item.name)}</span><br><span class="search-result-meta">${esc(item.sector || item.type || '')}</span></span><span class="pill source">${esc(item.type === 'company' ? '公司' : item.type === 'event' ? '事件' : '来源')}</span></button>`).join('') : '<div class="empty">没有找到匹配内容。试试公司名、赛道或“资本开支”。</div>';
      panel.hidden = false;
      $$('.search-result', panel).forEach(button => button.addEventListener('click', () => {
        const type = button.dataset.searchType;
        if (type === 'company') { state.companyId = button.dataset.searchId; state.view = 'company'; }
        if (type === 'event') { state.selectedEventId = button.dataset.searchId; state.view = 'events'; }
        if (type === 'source') { state.view = 'sources'; }
        renderView();
      }));
      if (focus) $('#global-search').focus();
    } catch (error) { showToast(error.message); }
  }, 180);
}

function handlePrompt(promptId) {
  const prompt = state.config.prompts.find(item => item.id === promptId);
  if (!prompt) return;
  if (promptId === 'company-snapshot' || promptId === 'cloud-compare') { state.view = 'company'; }
  else if (promptId === 'sector-scan') { state.view = 'sector'; }
  else if (promptId === 'event-analysis' || promptId === 'latest-news') { state.view = 'events'; }
  else { state.view = 'memo'; }
  renderView();
  showToast(`已打开「${prompt.label}」工作流`);
}

async function renderCompany() {
  setLoading('正在加载公司事实与来源…');
  try {
    const snapshot = await api(`/api/companies/${encodeURIComponent(state.companyId)}/snapshot`);
    const company = snapshot.company;
    $('#content').innerHTML = `
      ${pageHead('02 · COMPANY SNAPSHOT', '公司快照', '把一个公司的经营变化、口径说明和事件证据放在同一个可复核画面里。', '<button class="action-button" id="company-to-memo">加入研究备忘录 →</button>')}
      <div class="control-bar"><span class="control-label">当前研究对象</span><select id="company-select">${state.config.companies.map(c => `<option value="${esc(c.id)}" ${c.id === state.companyId ? 'selected' : ''}>${esc(c.name_zh)} · ${esc(c.name)}</option>`).join('')}</select><span class="info-note" style="padding:8px 10px">核心计算由本地规则引擎完成，LLM 不参与数字计算。</span></div>
      <section class="company-hero"><div><div class="company-title"><div class="company-avatar">${initial(company.name)}</div><div><div class="company-name">${esc(company.name_zh)}</div><div class="company-en">${esc(company.name)} · ${esc(company.ticker)}</div></div></div><p class="company-description">${esc(company.description)}</p></div><div class="company-tags"><span class="tag">${esc(company.sector)}</span><span class="tag">${esc(company.listing)}</span><span class="tag">${esc(company.country === 'US' ? '美国公司' : '中国公司')}</span></div></section>
      <div class="data-note official-note"><strong>数据边界：</strong>${esc(snapshot.data_notice || '当前页面只展示官方历史事实及可审计派生值。')} 缺失字段保持“未披露”，不同币种、期间和会计口径不会自动转换或混排。</div>
      <div class="section-head"><div><h2 class="section-title">关键指标</h2><p class="section-subtitle">数据截至 ${esc(snapshot.as_of || '未标注')} · ${esc(snapshot.official_fact_count || 0)} 条官方事实 · ${esc(snapshot.source_count || 0)} 个来源</p></div><div class="status-legend">${pill('官方直接披露', 'source')}${pill('官方事实派生', 'derived')}${pill('未披露', 'missing')}</div></div>
      <div class="metric-grid">${snapshot.metrics.map(metricCard).join('')}</div>
      <div class="two-col" style="margin-top:16px"><section class="card"><div class="card-title-row"><div><h3 class="card-title">证据链说明</h3><p class="card-caption">从数字回到来源，再回到原文定位。</p></div>${officialDataBadge()}</div><div class="analysis-block"><h4>当前可确认的产品事实</h4><ul><li>直接披露值保留来源 URL、发布日期和定位说明。</li><li>派生值只使用同币种、同期间、同会计口径和同合并范围的官方事实，并展示公式。</li><li>缺失值不会被自动填充为 0；正式结论仍需回到来源原文核验。</li></ul></div><div class="analysis-block"><h4>下一步研究问题</h4><ul><li>这个指标的官方定义和统计范围是什么？</li><li>报告期、财年截止日和同业公司是否一致？</li><li>是否存在管理层非 GAAP/IFRS 指标与报表指标的差异？</li></ul></div></section><section class="card"><div class="card-title-row"><div><h3 class="card-title">近期事件</h3><p class="card-caption">当前事件条目仍是演示线索，不自动成为结论。</p></div><button class="link-button" id="company-events">查看全部 →</button></div><div class="event-list">${snapshot.events.slice(0, 4).map(eventRow).join('') || '<div class="empty">暂无事件</div>'}</div></section></div>
    `;
    $('#company-select').addEventListener('change', e => { state.companyId = e.target.value; renderCompany(); });
    $('#company-to-memo').addEventListener('click', () => { state.view = 'memo'; renderView(); });
    $('#company-events').addEventListener('click', () => { state.view = 'events'; renderView(); });
    $$('[data-event]').forEach(button => button.addEventListener('click', () => { state.selectedEventId = button.dataset.event; state.view = 'events'; renderView(); }));
    $$('[data-source-link]').forEach(button => button.addEventListener('click', () => { state.view = 'sources'; renderView(); }));
  } catch (error) { setError(error); }
}

function metricCard(metric) {
  const status = metric?.status || 'missing';
  const statusLabel = metricStatusLabel(metric);
  const statusType = metricStatusType(metric);
  const value = metric?.display_value || '未披露';
  const detail = status === 'missing'
    ? metricMissingReason(metric)
    : metric?.formula
      ? `公式：${metric.formula}`
      : metric?.fact_status === 'verified_non_gaap'
        ? '公司定义口径 · 请注意与 GAAP/IFRS 区分'
        : `${metric?.period || '期间未标注'} · ${metric?.accounting_basis || '会计口径未标注'}`;
  const source = metric?.source_url
    ? `<a href="${esc(metric.source_url)}" target="_blank" rel="noreferrer">${esc(metric.source_title || '打开官方来源')}</a>`
    : '<span>暂无官方来源定位</span>';
  return `<article class="metric-card metric-${esc(status)}"><div class="metric-card-head"><div class="metric-label">${esc(metric?.label || metric?.key || '指标')}</div>${pill(statusLabel, statusType)}</div><div class="metric-value">${esc(value)}</div><div class="metric-delta ${status === 'missing' ? 'flat' : metric?.formula ? 'derived' : 'flat'}">${esc(detail)}</div><div class="metric-source">${source}</div>${metric?.source_locator ? `<div class="metric-locator">定位：${esc(metric.source_locator)}</div>` : ''}</article>`;
}

async function renderSector() {
  setLoading('正在生成可比公司矩阵…');
  try {
    const result = await api(`/api/sectors/${encodeURIComponent(state.sector)}`);
    const labels = { revenue: '收入', revenue_growth: '收入增速', gross_margin: '毛利率', operating_margin: '营业利润率', rd_ratio: '研发费用率', capex: '资本开支' };
    $('#content').innerHTML = `
      ${pageHead('03 · SECTOR SCAN', '赛道扫描', '横向比较不是把所有数字排成一张表，而是先说明哪些指标真正可比。', '<button class="action-button secondary" id="sector-to-memo">生成比较备忘录 →</button>')}
      <div class="control-bar"><span class="control-label">研究赛道</span><select id="sector-select">${state.config.sectors.map(s => `<option ${s === state.sector ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select><span class="info-note" style="padding:8px 10px">只对币种、报告期、会计口径、合并范围和单位完全一致的官方事实进行排名；不做币种转换。</span></div>
      <div class="data-note official-note"><strong>比较原则：</strong>${esc(result.method)}</div>
      <div class="card"><div class="card-title-row"><div><h3 class="card-title">${esc(result.sector)} · 公司×指标矩阵</h3><p class="card-caption">数据截至 ${esc(result.as_of || '未标注')} · “未披露”不等于 0，未达到比较条件的公司不会获得虚假排名。</p></div><div class="status-legend">${officialDataBadge()}${pill(`${result.rows.length} 家公司`, 'demo')}</div></div><div class="table-wrap"><table><thead><tr><th>排名 / 公司</th>${result.columns.map(key => `<th>${labels[key] || key}</th>`).join('')}<th>可比性</th></tr></thead><tbody>${result.rows.map(row => `<tr><td class="company-cell"><button class="link-button" data-company="${esc(row.company_id)}">${esc(row.company_zh)}</button><small>${esc(row.company)} · ${esc(row.ticker)}${row.growth_rank ? ` · #${esc(row.growth_rank)}` : ''}</small></td>${result.columns.map(key => { const metric = row[key] || {}; return `<td><div class="table-number">${esc(metric.display_value || '未披露')}</div><div class="table-sub">${metricStatusLabel(metric)}${metric.quality ? ` · ${esc(metric.quality)}` : ''}</div></td>`; }).join('')}<td>${pill(comparabilityLabel(row.comparability), row.comparability === 'eligible' ? 'source' : row.comparability === 'missing' ? 'missing' : 'medium')}<div class="table-sub comparison-detail">${esc(row.comparability_reason || '等待口径核验')}</div></td></tr>`).join('')}</tbody></table></div></div>
      <div class="two-col" style="margin-top:16px"><section class="card"><div class="card-title-row"><div><h3 class="card-title">最值得追问的差异</h3><p class="card-caption">这是产品帮助研究员发现问题的地方，不是自动生成投资结论。</p></div></div><div class="analysis-block"><h4>增长与规模</h4><ul><li>收入增速高不等于业务质量更高，需要结合利润率和现金投入。</li><li>规模差异较大的公司不应直接按绝对收入排名。</li></ul></div><div class="analysis-block"><h4>口径与时点</h4><ul><li>中美公司财年截止日和会计准则可能不同。</li><li>云业务、广告业务和用户指标的定义并不天然一致。</li></ul></div></section><section class="card"><div class="card-title-row"><div><h3 class="card-title">研究检查清单</h3><p class="card-caption">导出前逐项确认。</p></div></div><div class="analysis-block"><ul><li>是否记录每个数字的原始来源和定位？</li><li>是否区分 GAAP/IFRS 与管理层非 GAAP 指标？</li><li>是否把缺失项与不可比项保留为“未披露/不可比较”？</li><li>是否把事实、计算和推断分开？</li></ul></div></section></div>
    `;
    $('#sector-select').addEventListener('change', e => { state.sector = e.target.value; renderSector(); });
    $('#sector-to-memo').addEventListener('click', () => { state.view = 'memo'; renderView(); });
    $$('[data-company]').forEach(button => button.addEventListener('click', () => { state.companyId = button.dataset.company; state.view = 'company'; renderView(); }));
  } catch (error) { setError(error); }
}

function renderEventAnalysis(analysis) {
  const coverageText = analysis.evidence_coverage == null ? '未核验' : `${analysis.evidence_coverage}%`;
  const coverageBar = analysis.evidence_coverage == null ? '' : `<div class="coverage-bar"><span style="width:${analysis.evidence_coverage}%"></span></div>`;
  return `<div class="card-title-row"><div><h3 class="card-title">${esc(analysis.event.title)}</h3><p class="card-caption">${esc(analysis.event.company_name_zh)} · ${esc(analysis.event.date)} · ${esc(analysis.event.source_title)}</p></div>${pill(analysis.event.status_label, analysis.event.demo_only ? 'demo' : 'source')}</div><div class="coverage"><span class="coverage-value">证据覆盖 ${coverageText}</span>${coverageBar}</div><div class="settings-help">${esc(analysis.evidence_coverage_status || '段落级证据覆盖率需人工核验。')}</div><div class="analysis-block"><h4>直接事实</h4><ul>${analysis.direct_facts.map(item => `<li>${esc(item)}</li>`).join('')}</ul></div><div class="analysis-block"><h4>可能的传导路径</h4><ul>${analysis.transmission_paths.map(item => `<li>${esc(item)}</li>`).join('')}</ul></div><div class="analysis-block"><h4>分析性推断（必须人工确认）</h4><ul>${analysis.inferences.map(item => `<li>${esc(item)}</li>`).join('')}</ul></div><div class="analysis-block"><h4>待验证问题</h4><ul>${analysis.questions.map(item => `<li>${esc(item)}</li>`).join('')}</ul></div><button class="action-button secondary" id="event-to-memo">将事件加入备忘录 →</button>`;
}

async function renderEvents() {
  setLoading('正在加载事件雷达…');
  try {
    if (!state.events.length) state.events = (await api('/api/events')).events;
    const selectedId = state.selectedEventId || state.events[0]?.id;
    state.selectedEventId = selectedId;
    const selected = state.events.find(event => event.id === selectedId) || state.events[0];
    state.selectedEventAnalysis = selected ? await api(`/api/events/${encodeURIComponent(selected.id)}`) : null;
    const analysis = state.selectedEventAnalysis;
    $('#content').innerHTML = `
      ${pageHead('04 · EVENT RADAR', '事件雷达', '把新闻和公告拆成直接事实、传导路径、反向证据和待验证问题。', '<button class="action-button" id="collection-plan-button">配置定期收集 →</button>')}
      <div class="data-note"><strong>审阅优先：</strong>事件不会自动改写已确认事实。连接 API 后，Agent 只会把新资讯放入待审核资讯箱，用户确认后才进入时间线和事实抽取。</div>
      <div class="event-layout"><div class="event-select-list">${state.events.map(event => `<button class="event-select ${event.id === selected?.id ? 'active' : ''}" data-select-event="${esc(event.id)}"><div class="event-select-date">${esc(event.date)} · ${esc(event.company_name_zh)}</div><div class="event-select-title">${esc(event.title)}</div><div class="event-select-meta">${pill(event.impact === '高' ? '高影响' : '中影响', event.impact === '高' ? 'high' : 'medium')} ${event.demo_only ? pill('演示事件', 'demo') : pill('官方来源', 'source')}</div></button>`).join('')}</div><section class="card analysis-card">${analysis ? renderEventAnalysis(analysis) : '<div class="empty">暂无事件，请先导入来源。</div>'}</section></div>
    `;
    $$('[data-select-event]').forEach(button => button.addEventListener('click', () => { state.selectedEventId = button.dataset.selectEvent; renderEvents(); }));
    $('#event-to-memo')?.addEventListener('click', () => { state.view = 'memo'; renderView(); });
    $('#collection-plan-button').addEventListener('click', () => { state.view = 'sources'; renderView(); setTimeout(() => $('#collection-section')?.scrollIntoView({ behavior: 'smooth' }), 50); });
  } catch (error) { setError(error); }
}

async function renderMemo() {
  $('#content').innerHTML = `
    ${pageHead('05 · RESEARCH MEMO', '研究备忘录', '选择一个公司、赛道或事件，生成一份可以继续审阅、编辑和导出的研究草稿。')}
    <div class="memo-grid"><section class="card"><div class="card-title-row"><div><h3 class="card-title">备忘录构建器</h3><p class="card-caption">本地模板使用官方历史事实；接入后可在明确同意后生成语言草稿。</p></div>${officialDataBadge()}</div><div class="form-stack"><div class="field"><label for="memo-company">公司快照（可选）</label><select id="memo-company"><option value="">不选择</option>${state.config.companies.map(c => `<option value="${esc(c.id)}" ${c.id === state.companyId ? 'selected' : ''}>${esc(c.name_zh)} · ${esc(c.name)}</option>`).join('')}</select></div><div class="field"><label for="memo-sector">赛道扫描（可选）</label><select id="memo-sector"><option value="">不选择</option>${state.config.sectors.map(s => `<option value="${esc(s)}" ${s === state.sector ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select></div><div class="field"><label for="memo-event">事件分析（可选）</label><select id="memo-event"><option value="">不选择</option>${state.events.map(e => `<option value="${esc(e.id)}" ${e.id === state.selectedEventId ? 'selected' : ''}>${esc(e.company_name_zh)} · ${esc(e.title)}</option>`).join('')}</select></div><div class="field"><label for="memo-notes">研究员备注</label><textarea id="memo-notes" placeholder="写下你希望报告突出的问题、限制或下一步验证方向…"></textarea></div><button class="action-button" id="generate-memo">生成本地备忘录</button><div class="info-note">生成内容会明确标注官方直接披露、可审计派生值、缺失项和待审核事件。导出前请确认每个事实都回到来源定位。</div></div></section><section class="memo-preview" id="memo-preview"><div class="memo-empty">选择研究对象后点击“生成本地备忘录”。<br>你会看到事实、计算、推断与来源如何被分层组织。</div></section></div>
  `;
  $('#generate-memo').addEventListener('click', generateMemo);
}

function markdownToHtml(markdown) {
  return markdown.split('\n').map(line => {
    const safe = esc(line);
    if (line.startsWith('# ')) return `<h1>${safe.slice(2)}</h1>`;
    if (line.startsWith('## ')) return `<h2>${safe.slice(3)}</h2>`;
    if (line.startsWith('### ')) return `<h3>${safe.slice(4)}</h3>`;
    if (line.startsWith('- ')) return `<li>${safe.slice(2)}</li>`;
    if (line.startsWith('> ')) return `<blockquote>${safe.slice(2)}</blockquote>`;
    return line.trim() ? `<p>${safe}</p>` : '';
  }).join('');
}

async function generateMemo() {
  const preview = $('#memo-preview');
  preview.innerHTML = '<div class="loading">正在生成可追溯备忘录…</div>';
  try {
    state.memo = await api('/api/memos', { method: 'POST', body: JSON.stringify({ company_id: $('#memo-company').value || null, sector: $('#memo-sector').value || null, event_id: $('#memo-event').value || null, notes: $('#memo-notes').value }) });
    preview.innerHTML = `<div class="memo-content">${markdownToHtml(state.memo.markdown)}<div style="border-top:1px solid var(--line);margin-top:25px;padding-top:16px;display:flex;gap:9px;flex-wrap:wrap"><button class="action-button" id="download-md">导出 Markdown</button><button class="outline-button" id="download-html">导出 HTML</button><button class="action-button secondary" id="llm-draft">用 LLM 润色（可选）</button></div></div>`;
    $('#download-md').addEventListener('click', () => downloadFile(`${state.memo.title}.md`, state.memo.markdown, 'text/markdown;charset=utf-8'));
    $('#download-html').addEventListener('click', () => downloadFile(`${state.memo.title}.html`, state.memo.html, 'text/html;charset=utf-8'));
    $('#llm-draft').addEventListener('click', draftWithLLM);
    showToast('备忘录已生成；当前不依赖 LLM');
  } catch (error) { preview.innerHTML = `<div class="error-box">${esc(error.message)}</div>`; }
}

async function draftWithLLM() {
  if (!state.memo) return;
  try {
    const evidence = state.memo.markdown.split('\n').filter(line => line.startsWith('- ')).slice(0, 12);
    const result = await api('/api/llm/draft', { method: 'POST', body: JSON.stringify({ instruction: '请把这些已选证据压缩成一段中文研究摘要，保留事实/计算/推断边界和来源提示。', evidence }) });
    const block = document.createElement('div');
    block.className = 'info-note';
    block.style.marginTop = '15px';
    block.innerHTML = `<strong>LLM 草稿（已标记为生成内容）：</strong><br>${esc(result.content)}`;
    $('#memo-preview').appendChild(block);
  } catch (error) { showToast(error.message); }
}

function downloadFile(filename, content, type) {
  const blob = new Blob([content], { type });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  link.click();
  URL.revokeObjectURL(link.href);
}

async function renderSources() {
  setLoading('正在加载来源与连接设置…');
  try {
    const sourceResult = await api('/api/sources');
    const collection = await api('/api/collection/status');
    const settings = state.settings || {};
    $('#content').innerHTML = `
      ${pageHead('06 · SOURCES & SETTINGS', '来源与设置', '连接真实材料，但不让外部 API 成为产品的单点故障。每个来源都需要明确授权与审阅状态。')}
      <div class="source-grid"><section class="card"><div class="card-title-row"><div><h3 class="card-title">官方来源注册表</h3><p class="card-caption">存储来源入口和许可提醒，不镜像受限制的完整报告。</p></div><span class="pill source">${sourceResult.sources.length} 个来源</span></div><div class="source-list">${sourceResult.sources.map(sourceItem).join('')}</div></section><section class="form-stack"><section class="card settings-card"><div class="card-title-row"><div><h3 class="card-title">LLM 可选增强</h3><p class="card-caption">默认关闭；API key 只从环境变量读取。</p></div><span class="pill ${settings.allow_external_llm ? 'medium' : 'demo'}">${settings.allow_external_llm ? '已开启' : '未开启'}</span></div><div class="field"><label for="llm-provider">服务商标签</label><input id="llm-provider" value="${esc(settings.provider || 'openai-compatible')}" placeholder="openai-compatible"></div><div class="field"><label for="llm-base-url">Base URL</label><input id="llm-base-url" value="${esc(settings.base_url || '')}" placeholder="https://api.example.com/v1"></div><div class="field"><label for="llm-model">模型名</label><input id="llm-model" value="${esc(settings.model || '')}" placeholder="model-name"></div><div class="toggle-row"><div><strong style="font-size:12px;color:var(--teal)">允许发送选定证据片段</strong><div class="settings-help">默认不发送整个文件；必须显式开启。</div></div><input class="toggle" type="checkbox" id="llm-toggle" ${settings.allow_external_llm ? 'checked' : ''}></div><button class="action-button" id="save-llm">保存设置</button><div class="settings-help">环境变量：<code>TMT_LLM_API_KEY</code>。系统不会把密钥写入配置、数据库或日志。</div></section><section class="card settings-card" id="collection-section"><div class="card-title-row"><div><h3 class="card-title">定期资讯收集 Agent</h3><p class="card-caption">先生成计划，再进入待审核资讯箱。</p></div>${pill(collection.enabled ? '已允许计划' : '默认关闭', collection.enabled ? 'source' : 'demo')}</div><div class="toggle-row"><div><strong style="font-size:12px;color:var(--teal)">允许创建收集计划</strong><div class="settings-help">不会默认抓取全网，只使用你选择的来源。</div></div><input class="toggle" type="checkbox" id="collection-toggle" ${settings.allow_scheduled_collection ? 'checked' : ''}></div><div class="field"><label for="collection-frequency">频率</label><select id="collection-frequency"><option value="daily">每日</option><option value="weekly" selected>每周</option></select></div><button class="action-button secondary" id="preview-plan">生成审阅计划</button><button class="outline-button" id="open-agent-console" type="button">打开 Agent 调度台 →</button><div id="plan-result" class="settings-help">${esc(collection.next_step)}</div></section><section class="card"><h3 class="card-title">添加授权来源</h3><p class="card-caption">只保存来源元数据；请确认你有权访问和使用目标材料。</p><form id="source-form" class="form-stack" style="margin-top:15px"><div class="field"><label>来源标题</label><input name="title" required placeholder="公司 2026 Q2 业绩公告"></div><div class="field"><label>URL</label><input name="url" type="url" required placeholder="https://..."></div><div class="field"><label>备注</label><input name="notes" placeholder="授权范围、核查日期或数据限制"></div><button class="action-button" type="submit">保存来源入口</button></form></section></section></div>
    `;
    $('#save-llm').addEventListener('click', saveLLMSettings);
    $('#collection-toggle').addEventListener('change', e => saveSettings({ allow_scheduled_collection: e.target.checked }));
    $('#preview-plan').addEventListener('click', previewCollectionPlan);
    $('#open-agent-console').addEventListener('click', () => { state.view = 'automation'; renderView(); });
    $('#source-form').addEventListener('submit', addSource);
  } catch (error) { setError(error); }
}

function sourceItem(source) {
  return `<article class="source-item"><div class="source-item-title">${esc(source.title)}</div><div class="source-item-meta">${esc(source.company || '多公司')} · ${esc(source.type)} · ${esc(source.access)}</div><a href="${esc(source.url)}" target="_blank" rel="noreferrer">${esc(source.url)}</a><div class="source-item-meta">${esc(source.notes || '')}</div></article>`;
}

async function saveSettings(patch) {
  try {
    const result = await api('/api/settings/llm', { method: 'POST', body: JSON.stringify(patch) });
    state.settings = result.llm;
    updateHeader($('#breadcrumb-current').textContent);
    showToast('设置已保存');
  } catch (error) { showToast(error.message); }
}

async function saveLLMSettings() {
  await saveSettings({ provider: $('#llm-provider').value, base_url: $('#llm-base-url').value, model: $('#llm-model').value, allow_external_llm: $('#llm-toggle').checked });
  renderSources();
}

async function previewCollectionPlan() {
  const output = $('#plan-result');
  try {
    const sources = await api('/api/sources');
    const result = await api('/api/collection/plan', { method: 'POST', body: JSON.stringify({ source_ids: sources.sources.slice(0, 6).map(s => s.id), frequency: $('#collection-frequency').value, lookback_days: 7 }) });
    output.innerHTML = `<strong>计划已生成：</strong>${result.plan.steps.map(step => `<br>· ${esc(step)}`).join('')}<br><br>${esc(result.plan.note)}`;
  } catch (error) { output.textContent = error.message; }
}

async function addSource(event) {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target).entries());
  try { await api('/api/sources', { method: 'POST', body: JSON.stringify(data) }); showToast('来源入口已保存'); renderSources(); } catch (error) { showToast(error.message); }
}

document.addEventListener('click', event => {
  const nav = event.target.closest('[data-view]');
  if (nav) { state.view = nav.dataset.view; renderView(); }
  const sourceButton = event.target.closest('[data-action="show-sources"]');
  if (sourceButton) { state.view = 'sources'; renderView(); }
});

boot();
