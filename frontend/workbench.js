// TMT Atlas research workbench extensions.
// Loaded after app.js; uses the existing classic-script globals and API helper.
(function () {
  const T = window.TMTResearch = {};
  let lastWorkspace = null;
  let selectedEvidence = new Set();
  let lastAgentDashboard = null;

  const escLocal = (value) => String(value ?? '').replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
  const badge = (text, kind = 'demo') => `<span class="pill ${kind}">${escLocal(text)}</span>`;
  const fmt = (value, digits = 2) => value == null ? '—' : Number(value).toLocaleString(undefined, { maximumFractionDigits: digits });
  const jsonBlock = (value) => `<pre class="json-result">${escLocal(JSON.stringify(value, null, 2))}</pre>`;
  const errorBox = (error) => `<div class="error-box">${escLocal(error?.message || error || '请求失败')}</div>`;

  function extensionHead(kicker, title, description) {
    return `<div class="page-head"><div><div class="page-kicker">${escLocal(kicker)}</div><h1 class="page-title">${escLocal(title)}</h1><p class="page-desc">${escLocal(description)}</p></div></div>`;
  }

  function navTo(view) {
    state.view = view;
    renderView();
  }

  async function loadWorkspace() {
    lastWorkspace = await api('/api/workspace');
    return lastWorkspace;
  }

  function agentKindLabel(kind) {
    return ({
      collection_cycle: '授权资讯收集',
      company_snapshot_refresh: '公司快照刷新',
      sector_scan: '赛道扫描',
      event_triage: '事件分诊',
      memo_draft: '备忘录草稿',
    })[kind] || kind || '研究任务';
  }

  function agentPlanPayloadFields(kind) {
    if (kind === 'collection_cycle') {
      return `<div class="field"><label for="agent-source-scope">来源 URL（每行一个）</label><textarea id="agent-source-scope" placeholder="https://example.org/feed.xml"></textarea></div><label class="check-row"><input type="checkbox" id="agent-authorization">我确认这些来源已获得访问和使用授权</label>`;
    }
    if (kind === 'company_snapshot_refresh') {
      return `<div class="field"><label for="agent-company-id">公司</label><select id="agent-company-id">${state.config.companies.map(company => `<option value="${escLocal(company.id)}">${escLocal(company.name_zh)} · ${escLocal(company.name)}</option>`).join('')}</select></div>`;
    }
    if (kind === 'sector_scan') {
      return `<div class="field"><label for="agent-sector-name">赛道</label><select id="agent-sector-name">${state.config.sectors.map(sector => `<option value="${escLocal(sector)}">${escLocal(sector)}</option>`).join('')}</select></div>`;
    }
    if (kind === 'event_triage') {
      return `<div class="field"><label for="agent-event-id">事件</label><select id="agent-event-id">${state.events.map(event => `<option value="${escLocal(event.id)}">${escLocal(event.company_name_zh)} · ${escLocal(event.title)}</option>`).join('')}</select></div>`;
    }
    return `<div class="field"><label for="agent-company-id">公司（可选）</label><select id="agent-company-id"><option value="">不选择</option>${state.config.companies.map(company => `<option value="${escLocal(company.id)}">${escLocal(company.name_zh)} · ${escLocal(company.name)}</option>`).join('')}</select></div><div class="field"><label for="agent-sector-name">赛道（可选）</label><select id="agent-sector-name"><option value="">不选择</option>${state.config.sectors.map(sector => `<option value="${escLocal(sector)}">${escLocal(sector)}</option>`).join('')}</select></div><div class="field"><label for="agent-event-id">事件（可选）</label><select id="agent-event-id"><option value="">不选择</option>${state.events.map(event => `<option value="${escLocal(event.id)}">${escLocal(event.company_name_zh)} · ${escLocal(event.title)}</option>`).join('')}</select></div>`;
  }

  function agentPlanPayload(kind) {
    const payload = {};
    const company = $('#agent-company-id')?.value;
    const sector = $('#agent-sector-name')?.value;
    const event = $('#agent-event-id')?.value;
    if (company) payload.company_id = company;
    if (sector) payload.sector = sector;
    if (event) payload.event_id = event;
    if (kind === 'collection_cycle') {
      payload.source_scope = ($('#agent-source-scope')?.value || '').split(/[\r\n,]+/).map(value => value.trim()).filter(Boolean);
      payload.authorization = Boolean($('#agent-authorization')?.checked);
    }
    return payload;
  }

  function renderAgentPlan(plan) {
    const payload = plan.payload || {};
    const scope = (plan.source_scope || payload.source_scope || []).join(' · ');
    return `<article class="agent-plan-card"><div class="agent-plan-head"><div><div class="agent-plan-title">${escLocal(plan.name || agentKindLabel(plan.kind))}</div><div class="agent-plan-meta">${escLocal(agentKindLabel(plan.kind))} · 每 ${escLocal(plan.schedule?.interval_minutes || '—')} 分钟 · ${escLocal(plan.created_at || '')}</div></div>${badge(agentStatusLabel(plan.status), agentStatusType(plan.status))}</div><div class="agent-plan-detail">${plan.kind === 'collection_cycle' ? `来源范围：${escLocal(scope || '未配置')} · 授权：${plan.authorization === true ? '已确认' : '未确认'}` : `研究对象：${escLocal(payload.company_id || payload.sector || payload.event_id || '按计划配置')}`}</div><div class="agent-plan-actions">${plan.status === 'draft' ? `<button class="action-button agent-plan-action" data-agent-action="approve" data-agent-plan-id="${escLocal(plan.id)}">批准计划</button>` : ''}${['approved', 'active'].includes(plan.status) ? `<button class="outline-button agent-plan-action" data-agent-action="run" data-agent-plan-id="${escLocal(plan.id)}">立即运行</button><button class="ghost-button agent-plan-action" data-agent-action="pause" data-agent-plan-id="${escLocal(plan.id)}">暂停</button>` : ''}${plan.status === 'paused' ? `<button class="outline-button agent-plan-action" data-agent-action="resume" data-agent-plan-id="${escLocal(plan.id)}">恢复</button>` : ''}<span class="agent-plan-next">下次：${escLocal(plan.next_run || '批准后生成')}</span></div></article>`;
  }

  function renderAgentRun(run, tasksByRun) {
    const tasks = tasksByRun[run.id] || [];
    return `<article class="agent-run-row"><div><strong>${escLocal(agentKindLabel(run.kind))}</strong><div class="agent-plan-meta">${escLocal(run.created_at || '')} · ${escLocal(run.trigger || 'manual')}</div></div>${badge(agentStatusLabel(run.status), agentStatusType(run.status))}<div class="agent-run-tasks">${tasks.map(task => `${badge(agentStatusLabel(task.status), agentStatusType(task.status))} ${escLocal(task.label)}`).join(' · ') || '暂无任务详情'}</div></article>`;
  }

  function renderArtifactRow(artifact) {
    const result = artifact.payload?.result || artifact.data?.result || artifact.payload || {};
    const title = result.title || result.company?.name_zh || agentKindLabel(artifact.kind);
    const status = artifact.status || 'draft';
    return `<article class="agent-plan-card"><div class="agent-plan-head"><div><div class="agent-plan-title">${escLocal(title)}</div><div class="agent-plan-meta">${escLocal(agentKindLabel(artifact.kind))} · ${escLocal(artifact.created_at || '')}</div></div>${badge(agentStatusLabel(status), agentStatusType(status))}</div><div class="agent-plan-detail">${escLocal(artifact.payload?.data_mode || 'official_historical')} · facts approved: ${artifact.payload?.facts_approved === false ? '否' : '需审核'}</div><div class="agent-plan-actions">${status === 'draft' ? `<button class="outline-button agent-artifact-review" data-artifact-id="${escLocal(artifact.id)}" data-artifact-decision="accepted">接受草稿</button><button class="ghost-button agent-artifact-review" data-artifact-id="${escLocal(artifact.id)}" data-artifact-decision="rejected">拒绝</button>` : ''}</div></article>`;
  }
  function renderReviewCard(item, gate) {
    const isGate = Boolean(gate);
    const id = isGate ? gate.id : item.id;
    const status = isGate ? gate.status : item.status;
    const sourceItemId = item?.id || gate?.item_id;
    const title = item?.title || item?.name || item?.headline || (isGate ? `待审核线索 ${gate.item_id || gate.id}` : '授权来源结果');
    const sourceTitle = item?.source_title || item?.source || gate?.kind || 'review gate';
    const date = item?.date || item?.published_at || item?.created_at || gate?.created_at || '';
    const url = item?.url || item?.source_url || '仅保留元数据；原始文件不会自动外发';
    const reviewId = isGate ? id : sourceItemId;
    const reviewKind = isGate ? 'gate' : 'item';
    const reviewControls = status === 'pending'
      ? `<div class="agent-plan-actions"><button class="outline-button agent-review-action" data-review-kind="${reviewKind}" data-review-id="${escLocal(reviewId)}" data-review-status="accepted">接受线索</button><button class="ghost-button agent-review-action" data-review-kind="${reviewKind}" data-review-id="${escLocal(reviewId)}" data-review-status="rejected">拒绝</button></div>`
      : `<div class="settings-help">${escLocal(isGate ? gate.reason || '已记录审核结果' : '已记录审核结果')} · 不会自动批准金融事实</div>`;
    return `<article class="review-card"><div class="agent-plan-head"><div><div class="agent-plan-title">${escLocal(title)}</div><div class="agent-plan-meta">${escLocal(sourceTitle)} · ${escLocal(date)}</div></div>${badge(agentStatusLabel(status), agentStatusType(status))}</div><div class="agent-review-url">${escLocal(url)}</div>${item?.facts_approved === false || isGate ? '<div class="settings-help">资讯线索状态不会改变金融事实审核状态。</div>' : ''}${reviewControls}</article>`;
  }



  function mergeReviewQueue(items, gates) {
    const itemById = new Map((items || []).map(item => [item.id, item]));
    const cards = [];
    const seenItems = new Set();
    (gates || []).forEach(gate => {
      const item = itemById.get(gate.item_id);
      cards.push(renderReviewCard(item || null, gate));
      if (item) seenItems.add(item.id);
    });
    (items || []).forEach(item => {
      if (!seenItems.has(item.id)) cards.push(renderReviewCard(item, null));
    });
    return cards.join('');
  }

  async function loadAgentDashboard() {
    const [plansResult, runsResult, gatesResult, itemsResult, artifactsResult] = await Promise.all([
      api('/api/agent/plans'), api('/api/agent/runs'), api('/api/agent/review-gates?status=pending'), api('/api/agent/retrieval-items?status=pending'), api('/api/agent/artifacts'),
    ]);
    const taskResults = await Promise.all((runsResult.runs || []).slice(0, 8).map(run => api(`/api/agent/runs/${encodeURIComponent(run.id)}/tasks`).catch(() => ({ tasks: [] }))));
    const tasksByRun = {};
    (runsResult.runs || []).slice(0, 8).forEach((run, index) => { tasksByRun[run.id] = taskResults[index].tasks || []; });
    return { plans: plansResult.plans || [], runs: runsResult.runs || [], gates: gatesResult.review_gates || [], items: itemsResult.retrieval_items || [], artifacts: artifactsResult.artifacts || [], tasksByRun };
  }

  T.renderAgent = async function renderAgent() {
    setLoading('正在加载 Agent 计划、运行记录和审核队列…');
    try {
      lastAgentDashboard = await loadAgentDashboard();
      const dashboard = lastAgentDashboard;
      const pendingReviewIds = new Set([
        ...dashboard.gates.map(gate => gate.item_id || gate.id),
        ...dashboard.items.map(item => item.id),
      ]);
      const pendingReviews = pendingReviewIds.size;
      const reviewQueue = mergeReviewQueue(dashboard.items, dashboard.gates);
      $('#content').innerHTML = `${extensionHead('10 · AGENT AUTOMATION', 'Agent 自动化', '把研究任务变成可批准、可暂停、可重跑的本地计划；每次运行都保留草稿状态，资讯和事实必须经过人工审核。')}<div class="agent-guardrail"><div>${officialDataBadge()} <strong>官方历史事实是唯一数字来源。</strong> Agent 只能刷新本地快照、生成可审阅草稿或收集已授权来源的元数据，不会自动批准金融事实。</div><button class="outline-button" id="refresh-agent">刷新状态</button></div><div class="agent-grid"><section class="card"><div class="card-title-row"><div><h3 class="card-title">新建研究计划</h3><p class="card-caption">计划先保存为“待确认”，批准后才可运行。</p></div></div><form id="agent-plan-form" class="form-stack"><div class="field"><label for="agent-kind">计划类型</label><select id="agent-kind"><option value="company_snapshot_refresh">公司快照刷新</option><option value="sector_scan">赛道扫描</option><option value="event_triage">事件分诊</option><option value="memo_draft">备忘录草稿</option><option value="collection_cycle">授权资讯收集</option></select></div><div class="field"><label for="agent-name">计划名称</label><input id="agent-name" placeholder="例如：每周微软官方快照"></div><div class="field"><label for="agent-interval">间隔（分钟，至少 15）</label><input id="agent-interval" type="number" value="60" min="15" required></div><div id="agent-kind-fields">${agentPlanPayloadFields('company_snapshot_refresh')}</div><button class="action-button" type="submit">保存为待确认计划</button><div class="settings-help">授权收集必须同时提供来源范围和明确授权；所有研究结果都保持 draft。</div></form></section><section class="card"><div class="card-title-row"><div><h3 class="card-title">计划队列</h3><p class="card-caption">批准、运行和暂停都写入本地审计记录。</p></div><span class="pill demo">${dashboard.plans.length} 个计划</span></div><div class="agent-list">${dashboard.plans.length ? dashboard.plans.map(renderAgentPlan).join('') : '<div class="empty">还没有 Agent 计划。</div>'}</div></section></div><div class="agent-grid agent-grid-bottom"><section class="card"><div class="card-title-row"><div><h3 class="card-title">最近运行</h3><p class="card-caption">运行结果只会生成草稿或待审核线索。</p></div><button class="link-button" id="refresh-agent-runs">刷新</button></div><div class="agent-list">${dashboard.runs.length ? dashboard.runs.slice(0, 8).map(run => renderAgentRun(run, dashboard.tasksByRun)).join('') : '<div class="empty">还没有运行记录。</div>'}</div></section><section class="card"><div class="card-title-row"><div><h3 class="card-title">审核队列</h3><p class="card-caption">接受线索不等于批准事实；同一条线索的 review gate 与来源记录会合并显示。</p></div><span class="pill ${pendingReviews ? 'medium' : 'source'}">${pendingReviews} 条待审核</span></div><div class="review-list">${reviewQueue || '<div class="empty">暂无待审核线索。</div>'}</div></section></div><section class="card agent-artifacts-card"><div class="card-title-row"><div><h3 class="card-title">研究草稿产物</h3><p class="card-caption">快照、赛道、事件和备忘录的 Agent 结果会显示在这里；接受草稿不等于批准金融事实。</p></div><span class="pill ${dashboard.artifacts.length ? 'medium' : 'source'}">${dashboard.artifacts.length} 个草稿</span></div><div class="agent-list">${dashboard.artifacts.length ? dashboard.artifacts.slice(0, 8).map(renderArtifactRow).join('') : '<div class="empty">暂无草稿产物。</div>'}</div></section>`;
      bindAgentPage();
    } catch (error) { setError(error); }
  };

  function bindAgentPage() {
    $('#refresh-agent')?.addEventListener('click', () => T.renderAgent());
    $('#refresh-agent-runs')?.addEventListener('click', () => T.renderAgent());
    $('#agent-kind')?.addEventListener('change', event => { $('#agent-kind-fields').innerHTML = agentPlanPayloadFields(event.target.value); });
    $('#agent-plan-form')?.addEventListener('submit', createAgentPlan);
    $$('.agent-plan-action').forEach(button => button.addEventListener('click', () => handleAgentPlanAction(button.dataset.agentAction, button.dataset.agentPlanId)));
    $$('.agent-review-action').forEach(button => button.addEventListener('click', () => reviewAgentItem(button.dataset.reviewKind, button.dataset.reviewId, button.dataset.reviewStatus)));
    $$('.agent-artifact-review').forEach(button => button.addEventListener('click', () => reviewAgentArtifact(button.dataset.artifactId, button.dataset.artifactDecision)));
  }

  async function createAgentPlan(event) {
    event.preventDefault();
    const kind = $('#agent-kind').value;
    const payload = { kind, name: $('#agent-name').value.trim() || agentKindLabel(kind), schedule: { interval_minutes: Number($('#agent-interval').value) }, ...agentPlanPayload(kind) };
    try { await api('/api/agent/plans', { method: 'POST', body: JSON.stringify(payload) }); showToast('计划已保存为待确认'); await T.renderAgent(); } catch (error) { showToast(`计划未保存：${error.message}`); }
  }

  async function handleAgentPlanAction(action, planId) {
    try {
      const response = await api(`/api/agent/plans/${encodeURIComponent(planId)}/${action}`, { method: 'POST', body: JSON.stringify({ trigger: 'manual' }) });
      const runStatus = response.run?.status;
      showToast(action === 'run' ? `计划已运行：${agentStatusLabel(runStatus)}${runStatus === 'waiting_review' ? ' · 请处理审核队列' : ''}` : '计划状态已更新');
      await T.renderAgent();
    } catch (error) { showToast(`操作失败：${error.message}`); }
  }

  async function reviewAgentArtifact(id, decision) {
    const reason = window.prompt(decision === 'accepted' ? '接受草稿理由（必填）' : '拒绝草稿理由（必填）', '已完成人工复核');
    if (!reason) return;
    try {
      await api(`/api/agent/artifacts/${encodeURIComponent(id)}/review`, { method: 'POST', body: JSON.stringify({ decision, reason }) });
      showToast(`草稿已${decision === 'accepted' ? '接受' : '拒绝'}；事实仍需单独审核`);
      await T.renderAgent();
    } catch (error) { showToast(`草稿审核失败：${error.message}`); }
  }


  async function reviewAgentItem(kind, id, status) {
    let reason = '已核验来源范围与元数据';
    if (kind === 'gate') reason = window.prompt(status === 'accepted' ? '接受理由（必填）' : '拒绝理由（必填）', reason);
    if (kind === 'gate' && !reason) return;
    try {
      const path = kind === 'gate' ? `/api/agent/review-gates/${encodeURIComponent(id)}/decision` : `/api/agent/retrieval-items/${encodeURIComponent(id)}/review`;
      const body = kind === 'gate' ? { decision: status, reason } : { status };
      await api(path, { method: 'POST', body: JSON.stringify(body) });
      showToast(`审核结果：${agentStatusLabel(status)}`);
      await T.renderAgent();
    } catch (error) { showToast(`审核失败：${error.message}`); }
  }


  T.renderEvidence = async function renderEvidence() {
    setLoading('正在加载本地来源、证据和事实…');
    try {
      const workspace = await loadWorkspace();
      const sources = workspace.sources || [];
      const facts = workspace.facts || [];
      const evidence = workspace.evidence || [];
      $('#content').innerHTML = `
        ${extensionHead('07 · EVIDENCE REVIEW', '证据与事实审核', '把本地文件变成带页码、行号或 CSV 行号的证据片段；事实默认 pending，只有人工审核后才进入 accepted。')}
        <div class="evidence-grid">
          <section class="card">
            <div class="card-title-row"><div><h3 class="card-title">导入本地材料</h3><p class="card-caption">文件不会离开本机；单文件上限 12 MiB。支持 PDF、CSV、JSON、Markdown、TXT。</p></div>${badge('无 LLM', 'source')}</div>
            <form id="evidence-upload-form" class="form-stack">
              <div class="field"><label for="evidence-file">选择文件</label><input id="evidence-file" type="file" accept=".pdf,.csv,.json,.md,.markdown,.txt" required></div>
              <div class="field"><label for="evidence-company">关联公司（可选）</label><select id="evidence-company"><option value="">不指定</option>${state.config.companies.map(c => `<option value="${escLocal(c.id)}">${escLocal(c.name_zh)} · ${escLocal(c.name)}</option>`).join('')}</select></div>
              <label class="check-row"><input type="checkbox" id="evidence-ocr">扫描 PDF 时尝试本地 OCR（结果必须人工核验）</label>
              <button class="action-button" type="submit">导入并建立证据</button>
              <a class="sample-link" href="/data/example-facts.csv" download>下载事实 CSV 模板</a>
              <div id="evidence-upload-result" class="settings-help">CSV 若匹配事实字段，只会提出 pending 事实，不会自动批准。</div>
            </form>
          </section>
          <section class="card">
            <div class="card-title-row"><div><h3 class="card-title">工作区统计</h3><p class="card-caption">当前本地 SQLite 工作区。</p></div><button class="link-button" id="refresh-workspace">刷新</button></div>
            <div class="stat-strip"><div><strong>${workspace.stats.sources}</strong><span>来源</span></div><div><strong>${workspace.stats.evidence}</strong><span>证据</span></div><div><strong>${workspace.stats.facts}</strong><span>事实</span></div><div><strong>${workspace.stats.accepted_facts}</strong><span>已接受</span></div></div>
            <div class="info-note">所有导入材料会按 SHA-256 去重，原文件放在应用自己的状态目录；请求体不能指定服务器路径。</div>
          </section>
        </div>
        <div class="section-head"><div><h2 class="section-title">证据检索</h2><p class="section-subtitle">搜索结果会保留来源、定位和原文摘录。</p></div></div>
        <section class="card"><div class="evidence-search"><input id="evidence-query" placeholder="搜索 revenue、云计算或中文关键词"><button class="outline-button" id="evidence-search-button">搜索证据</button></div><div id="evidence-results" class="evidence-list">${renderEvidenceRows(evidence)}</div></section>
        <div class="section-head"><div><h2 class="section-title">事实审核台</h2><p class="section-subtitle">接受或拒绝前，请回到证据原文核验期间、单位、主体和会计口径。</p></div></div>
        <section class="card"><div class="table-wrap"><table><thead><tr><th>事实</th><th>值 / 单位</th><th>期间</th><th>状态</th><th>冲突</th><th>操作</th></tr></thead><tbody>${facts.length ? facts.map(renderFactRow).join('') : '<tr><td colspan="6"><div class="empty">还没有事实。导入匹配字段的 CSV 或手动添加事实。</div></td></tr>'}</tbody></table></div></section>
        <div class="two-col" style="margin-top:16px"><section class="card"><h3 class="card-title">手动提出事实</h3><p class="card-caption">必须关联一个证据片段；新事实始终为 pending。</p>${factForm()}</section><section class="card"><h3 class="card-title">审核边界</h3><div class="analysis-block"><ul><li>“来源入口”不等于“页码证据”。</li><li>相同公司、指标、期间和 scope 出现不同值时，系统只发出冲突提醒，不自动覆盖。</li><li>拒绝事实不会删除原始证据，审计记录会保留前后状态。</li></ul></div></section></div>
      `;
      bindEvidencePage();
    } catch (error) { setError(error); }
  };

  function renderEvidenceRows(items) {
    if (!items?.length) return '<div class="empty">没有证据片段。先导入本地文件，或换一个关键词。</div>';
    return items.map(item => `<article class="evidence-row-card"><label class="evidence-check"><input type="checkbox" data-evidence-check="${escLocal(item.id)}" ${selectedEvidence.has(item.id) ? 'checked' : ''}><span></span></label><div class="evidence-body"><div class="evidence-meta">${escLocal(item.locator_value)} · ${escLocal(item.source_id)}</div><div class="evidence-quote">${escLocal(item.quote)}</div></div><button class="link-button evidence-send-one" data-evidence-id="${escLocal(item.id)}">选中用于 LLM</button></article>`).join('');
  }

  function factForm() {
    const fields = [
      ['company_id', '公司 ID', 'nvidia'], ['metric_key', '指标键', 'revenue'], ['value', '数值', '130.5'], ['unit', '单位', 'USD million'], ['currency', '币种', 'USD'],
      ['period_start', '起始日', '2025-01-01'], ['period_end', '结束日', '2025-12-31'], ['period_type', '期间类型', 'FY'], ['accounting_basis', '会计口径', 'GAAP'], ['scope', '合并范围', 'consolidated'], ['evidence_id', '证据 ID', '']
    ];
    return `<form id="manual-fact-form" class="fact-form">${fields.map(([name, label, placeholder]) => `<div class="field"><label for="fact-${name}">${label}</label><input id="fact-${name}" name="${name}" placeholder="${placeholder}" ${name === 'evidence_id' ? 'list="evidence-id-list"' : ''} required></div>`).join('')}<datalist id="evidence-id-list">${(lastWorkspace?.evidence || []).map(item => `<option value="${escLocal(item.id)}">${escLocal(item.locator_value)}</option>`).join('')}</datalist><button class="action-button" type="submit">提出 pending 事实</button><div id="fact-form-result" class="settings-help"></div></form>`;
  }

  function renderFactRow(fact) {
    const statusType = fact.status === 'accepted' ? 'source' : fact.status === 'rejected' ? 'high' : 'demo';
    return `<tr><td class="company-cell">${escLocal(fact.company_id)}<small>${escLocal(fact.metric_key)}</small></td><td><div class="table-number">${escLocal(fmt(fact.value))}</div><div class="table-sub">${escLocal(fact.unit)} · ${escLocal(fact.currency)}</div></td><td>${escLocal(fact.period_start)} — ${escLocal(fact.period_end)}<div class="table-sub">${escLocal(fact.accounting_basis)}</div></td><td>${badge(fact.status, statusType)}</td><td>${fact.conflict ? badge('需核验', 'medium') : badge('无冲突', 'source')}</td><td><button class="outline-button fact-review" data-fact-id="${escLocal(fact.id)}" data-review-status="accepted">接受</button> <button class="ghost-button fact-review" data-fact-id="${escLocal(fact.id)}" data-review-status="rejected">拒绝</button></td></tr>`;
  }

  function bindEvidencePage() {
    $('#refresh-workspace')?.addEventListener('click', () => T.renderEvidence());
    $('#evidence-upload-form')?.addEventListener('submit', uploadEvidence);
    $('#evidence-search-button')?.addEventListener('click', searchEvidence);
    $('#evidence-query')?.addEventListener('keydown', event => { if (event.key === 'Enter') searchEvidence(); });
    $$('[data-evidence-check]').forEach(input => input.addEventListener('change', event => event.target.checked ? selectedEvidence.add(event.target.dataset.evidenceCheck) : selectedEvidence.delete(event.target.dataset.evidenceCheck)));
    $$('.evidence-send-one').forEach(button => button.addEventListener('click', () => { selectedEvidence.add(button.dataset.evidenceId); openConsent(); }));
    $$('.fact-review').forEach(button => button.addEventListener('click', () => reviewFact(button.dataset.factId, button.dataset.reviewStatus)));
    $('#manual-fact-form')?.addEventListener('submit', addFact);
  }

  async function uploadEvidence(event) {
    event.preventDefault();
    const result = $('#evidence-upload-result');
    const file = $('#evidence-file').files[0];
    if (!file) return;
    if (file.size > 12 * 1024 * 1024) { result.textContent = '文件超过 12 MiB 上限。'; return; }
    result.textContent = '正在读取并建立本地证据…';
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = ''; const block = 0x8000;
      for (let i = 0; i < bytes.length; i += block) binary += String.fromCharCode(...bytes.subarray(i, i + block));
      const payload = { filename: file.name, content_base64: btoa(binary), ocr: $('#evidence-ocr').checked, company_id: $('#evidence-company').value };
      const imported = await api('/api/ingest', { method: 'POST', body: JSON.stringify(payload) });
      result.innerHTML = `已导入 ${escLocal(imported.source.filename)}：${imported.evidence.length} 条证据，${imported.pending} 条 pending 事实。${imported.source.warnings?.length ? '<br>' + imported.source.warnings.map(escLocal).join('<br>') : ''}`;
      await T.renderEvidence();
    } catch (error) { result.innerHTML = errorBox(error); }
  }

  async function searchEvidence() {
    const query = $('#evidence-query').value;
    const result = await api(`/api/evidence?q=${encodeURIComponent(query)}`);
    $('#evidence-results').innerHTML = renderEvidenceRows(result.evidence);
    bindEvidencePage();
  }

  async function addFact(event) {
    event.preventDefault();
    const form = event.target; const data = Object.fromEntries(new FormData(form).entries()); data.value = Number(data.value);
    const result = $('#fact-form-result');
    try { await api('/api/facts', { method: 'POST', body: JSON.stringify(data) }); result.textContent = '事实已提出为 pending；请在审核表中复核。'; await T.renderEvidence(); } catch (error) { result.innerHTML = errorBox(error); }
  }

  async function reviewFact(id, status) {
    const reason = window.prompt(status === 'accepted' ? '接受理由（必填）' : '拒绝理由（必填）', '已回到来源原文核对');
    if (!reason) return;
    try { await api('/api/facts/review', { method: 'POST', body: JSON.stringify({ id, status, reason }) }); showToast(`事实已标记为 ${status}`); await T.renderEvidence(); } catch (error) { showToast(error.message); }
  }

  function openConsent() {
    const evidence = (lastWorkspace?.evidence || []).filter(item => selectedEvidence.has(item.id));
    const overlay = document.createElement('div'); overlay.className = 'consent-overlay'; overlay.id = 'consent-overlay';
    overlay.innerHTML = `<div class="consent-modal" role="dialog" aria-modal="true" aria-labelledby="consent-title"><button class="modal-close" id="consent-close" aria-label="关闭">×</button><div class="page-kicker">EXPLICIT CONSENT</div><h2 id="consent-title">发送选定证据给外部模型？</h2><p>仅会发送下面勾选的证据片段；不会发送整个文件。模型、Base URL 和 API key 由你在设置页配置。返回内容会标记为生成草稿，不会自动写入事实层。</p><div class="consent-destination">当前选择：<strong>${evidence.length}</strong> 条证据 · LLM 默认关闭</div><div class="consent-list">${evidence.length ? evidence.map(item => `<label><input type="checkbox" checked data-consent-id="${escLocal(item.id)}"> <span>${escLocal(item.locator_value)}：${escLocal(item.quote.slice(0, 180))}</span></label>`).join('') : '<div class="empty">尚未选择证据片段。</div>'}</div><div class="consent-actions"><button class="outline-button" id="consent-cancel">取消</button><button class="action-button" id="consent-confirm">确认发送</button></div><div id="consent-result"></div></div>`;
    document.body.appendChild(overlay);
    $('#consent-close').addEventListener('click', closeConsent); $('#consent-cancel').addEventListener('click', closeConsent); $('#consent-confirm').addEventListener('click', confirmConsent);
  }
  function closeConsent() { $('#consent-overlay')?.remove(); }
  async function confirmConsent() {
    const ids = $$('[data-consent-id]').filter(input => input.checked).map(input => input.dataset.consentId);
    const evidence = (lastWorkspace?.evidence || []).filter(item => ids.includes(item.id));
    const result = $('#consent-result'); result.innerHTML = '<div class="loading">正在请求模型…</div>';
    try { const response = await api('/api/llm/draft', { method: 'POST', body: JSON.stringify({ instruction: '请仅根据这些证据生成带事实/计算/推断标签的研究摘要。', evidence: evidence.map(item => `[${item.locator_value}] ${item.quote}`) }) }); result.innerHTML = `<div class="info-note"><strong>生成草稿：</strong><br>${escLocal(response.content)}</div>`; } catch (error) { result.innerHTML = errorBox(error); }
  }

  T.renderFinance = async function renderFinance() {
    $('#content').innerHTML = `${extensionHead('08 · FINANCE ENGINE', '金融计算工具', '所有计算都在本地规则引擎完成。结果展示公式、输入、警告和状态；估值模块是教育性机械计算，不是价格目标。')}<div class="finance-grid"><section class="card"><h3 class="card-title">选择计算</h3><div class="form-stack"><div class="field"><label for="calc-operation">计算类型</label><select id="calc-operation"><option value="growth">同比增长率</option><option value="margin">利润率</option><option value="fcf">自由现金流</option><option value="enterprise_value">企业价值</option><option value="valuation_multiple">估值倍数</option><option value="dcf">简化 DCF（教育用途）</option></select></div><div id="calc-fields"></div><button class="action-button" id="run-calculation">运行本地计算</button><div class="settings-help">输入必须明确单位、币种和假设；不存在的历史数据不能由工具自动补全。</div></div></section><section class="card" id="calc-output"><div class="memo-empty">选择一个计算并运行。<br>金融引擎不会调用网络或 LLM。</div></section></div>`;
    $('#calc-operation').addEventListener('change', renderCalcFields); $('#run-calculation').addEventListener('click', runCalculation); renderCalcFields();
  };
  function inputField(name, label, placeholder) { return `<div class="field"><label for="calc-${name}">${label}</label><input id="calc-${name}" name="${name}" type="text" placeholder="${placeholder}" required></div>`; }
  function renderCalcFields() {
    const op = $('#calc-operation').value; const fields = {
      growth: inputField('current', '本期值', '120') + inputField('previous', '上期值（必须 > 0）', '100'),
      margin: inputField('numerator', '分子（利润/现金流）', '25') + inputField('denominator', '分母（收入）', '100'),
      fcf: inputField('cfo', '经营现金流 CFO', '100') + inputField('capex', '资本开支（填正的现金流出）', '30'),
      enterprise_value: inputField('market_cap', '市值', '1000') + inputField('debt', '债务', '200') + inputField('cash', '现金', '100') + inputField('preferred_equity', '优先股', '0') + inputField('minority_interest', '少数股东权益', '0'),
      valuation_multiple: inputField('numerator', '分子（EV/市值）', '1000') + inputField('denominator', '分母（收入/EPS）', '100') + `<div class="field"><label for="calc-multiple_type">类型</label><select id="calc-multiple_type"><option value="ev_revenue">EV / Revenue</option><option value="pe">P / E</option></select></div>`,
      dcf: inputField('base_fcf', '基准自由现金流', '100') + inputField('forecast_growth', '预测增长率（逗号分隔）', '0.10,0.08,0.06') + inputField('wacc', 'WACC（小数）', '0.10') + inputField('terminal_growth', '终值增长率（小数）', '0.03') + inputField('years', '预测年数（1-10）', '3') + inputField('net_debt', '净债务', '50') + inputField('shares', '股数', '10'),
    };
    $('#calc-fields').innerHTML = `<div class="calc-form">${fields[op]}</div><div class="field"><label for="calc-unit">统一单位（可选）</label><input id="calc-unit" placeholder="USD million"></div>`;
  }
  async function runCalculation() {
    const op = $('#calc-operation').value; const payload = { operation: op }; $$('#calc-fields input, #calc-fields select').forEach(input => { if (input.value !== '') payload[input.name || input.id.replace('calc-', '')] = input.value; });
    ['current','previous','numerator','denominator','cfo','capex','market_cap','debt','cash','preferred_equity','minority_interest','base_fcf','wacc','terminal_growth','years','net_debt','shares'].forEach(key => { if (payload[key] !== undefined) payload[key] = Number(payload[key]); });
    if (op === 'dcf') payload.forecast_growth = String(payload.forecast_growth).split(',').map(value => Number(value.trim()));
    try { const result = await api('/api/calculate', { method: 'POST', body: JSON.stringify(payload) }); $('#calc-output').innerHTML = `<div class="card-title-row"><div><h3 class="card-title">计算结果</h3><p class="card-caption">status: ${escLocal(result.status)}</p></div>${badge(result.status, result.status === 'ok' ? 'source' : 'medium')}</div><div class="calc-result-value">${result.value == null ? '无法计算' : escLocal(fmt(result.value, 6))} <span>${escLocal(result.unit || '')}</span></div><div class="analysis-block"><h4>公式</h4><code>${escLocal(result.formula)}</code></div><div class="analysis-block"><h4>输入</h4>${jsonBlock(result.inputs)}</div><div class="analysis-block"><h4>警告与限制</h4><ul>${(result.warnings || []).map(w => `<li>${escLocal(w)}</li>`).join('') || '<li>无额外警告</li>'}</ul></div>${result.sensitivity ? `<div class="analysis-block"><h4>敏感性矩阵</h4>${jsonBlock(result.sensitivity)}</div>` : ''}</section>`; } catch (error) { $('#calc-output').innerHTML = errorBox(error); }
  }

  T.renderAutomation = async function renderAutomation() {
    setLoading('正在加载资讯收集配置…');
    try { const config = await api('/api/collection/status'); const inbox = await api('/api/collection/inbox'); $('#content').innerHTML = `${extensionHead('09 · AUTHORIZED COLLECTION', '授权资讯收集', '只连接用户明确授权的 RSS/Atom/JSON Feed；只保存标题、日期和链接元数据；新资讯先进入审核箱，不自动写入事实。')}<div class="automation-grid"><section class="card"><h3 class="card-title">连接设置</h3><p class="card-caption">应用必须保持运行；最小间隔 15 分钟。没有显式授权时不会联网。</p><form id="collector-form" class="form-stack"><label class="check-row"><input type="checkbox" id="collector-enabled" ${config.enabled ? 'checked' : ''}>启用定期收集</label><label class="check-row"><input type="checkbox" id="collector-authorized">我确认下面的来源已获得访问和使用授权</label><div class="field"><label for="collector-interval">间隔（分钟，至少 15）</label><input id="collector-interval" value="${escLocal(config.interval_minutes || 60)}" type="number" min="15"></div><div class="field"><label for="collector-url">RSS/Atom/JSON Feed URL</label><input id="collector-url" placeholder="https://example.org/feed.xml" type="url"></div><div class="field"><label for="collector-title">来源名称</label><input id="collector-title" placeholder="公司官方 IR 更新"></div><button class="action-button" type="submit">保存授权配置</button><button class="outline-button" id="collector-run" type="button">现在运行一次</button><div class="settings-help">${escLocal(config.last_error || '当前没有错误')}</div></form></section><section class="card"><div class="card-title-row"><div><h3 class="card-title">待审核资讯箱</h3><p class="card-caption">接受资讯不会自动批准任何金融事实。</p></div><span class="pill demo">${inbox.items.length} 条</span></div><div class="inbox-list">${inbox.items.length ? inbox.items.map(renderInboxItem).join('') : '<div class="empty">还没有收集到资讯。可先保存授权配置，再运行一次。</div>'}</div></section></div>`; bindAutomation(); } catch (error) { setError(error); }
  };
  function renderInboxItem(item) { return `<article class="inbox-item"><div><div class="event-title">${escLocal(item.title)}</div><div class="event-summary">${escLocal(item.date || '未标注')} · ${escLocal(item.source_title)} · ${escLocal(item.url || '无链接')}</div></div>${badge(item.status, item.status === 'pending' ? 'medium' : item.status === 'accepted' ? 'source' : 'high')}<div class="inbox-actions"><button class="outline-button inbox-review" data-inbox-id="${escLocal(item.id)}" data-inbox-status="accepted">进入研究线索</button><button class="ghost-button inbox-review" data-inbox-id="${escLocal(item.id)}" data-inbox-status="rejected">忽略</button></div></article>`; }
  function bindAutomation() { $('#collector-form').addEventListener('submit', saveCollector); $('#collector-run').addEventListener('click', runCollector); $$('.inbox-review').forEach(button => button.addEventListener('click', async () => { try { await api('/api/collection/review', { method: 'POST', body: JSON.stringify({ id: button.dataset.inboxId, status: button.dataset.inboxStatus }) }); T.renderAutomation(); } catch (error) { showToast(error.message); } })); }
  async function saveCollector(event) { event.preventDefault(); const url = $('#collector-url').value.trim(); const feeds = url ? [{ url, title: $('#collector-title').value.trim(), format: 'auto', authorized: $('#collector-authorized').checked }] : []; try { await api('/api/collection/config', { method: 'POST', body: JSON.stringify({ enabled: $('#collector-enabled').checked, authorized: $('#collector-authorized').checked, interval_minutes: Number($('#collector-interval').value), feeds }) }); showToast('收集配置已保存'); T.renderAutomation(); } catch (error) { showToast('配置未保存：' + error.message); } }
  async function runCollector() { try { const result = await api('/api/collection/run', { method: 'POST', body: JSON.stringify({}) }); showToast(`收集完成：${result.items.length} 条新线索`); T.renderAutomation(); } catch (error) { showToast(error.message); } }

  document.addEventListener('click', event => {
    const nav = event.target.closest('[data-research-view]');
    if (nav) { navTo(nav.dataset.researchView); }
  });
})();
