const msg = document.getElementById('msg');
const view = document.getElementById('view');
let contracts = [];
let lastCheck = null;

async function api(path, opts={}) {
  const r = await fetch(path, {
    headers: {'Content-Type': 'application/json', ...(opts.headers||{})},
    ...opts
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ('HTTP '+r.status));
  return j;
}

function setMsg(t, isErr=false) {
  msg.textContent = t || '';
  msg.className = isErr ? 'err' : 'muted';
}

async function loadContracts() {
  const data = await api('/api/contracts');
  contracts = data.items || [];
}

function renderContracts() {
  view.innerHTML = `<div class="grid">${contracts.map(c => `
    <article class="card">
      <div class="row">
        <h3>${c.table_name} · v${c.version_no}</h3>
        <span class="chip">${c.status}</span>
        <button type="button" data-fields="${c.id}" class="ghost">字段详情</button>
      </div>
      <p class="muted">${c.note || ''} · 字段 ${c.field_count}</p>
      <pre class="muted" id="fields-${c.id}" hidden></pre>
    </article>`).join('')}</div>`;
  view.querySelectorAll('button[data-fields]').forEach(btn => {
    btn.onclick = async () => {
      const cid = btn.dataset.fields;
      const box = document.getElementById(`fields-${cid}`);
      if (!box.hidden) { box.hidden = true; return; }
      try {
        const j = await api(`/api/contracts/${cid}`);
        box.textContent = j.contract.fields
          .map(f => `${f.field_name}  ${f.field_type}`).join('\n');
        box.hidden = false;
      } catch (e) { setMsg(e.message, true); }
    };
  });
}

async function renderEnums() {
  const j = await api('/api/enums');
  const items = j.items || [];
  const latest = items.find(i => i.is_latest);
  const sameSource = (j.contract_enum_status || [])
    .every(v => (j.page_enum_status || []).includes(v))
    && (j.page_enum_status || []).every(v => (j.contract_enum_status || []).includes(v));
  view.innerHTML = `
    <div class="card grid">
      <div class="row">
        <strong>枚举列表（独立快照表，只追加不改写）</strong>
        <span class="chip ${sameSource ? '' : 'chip-warn'}">
          页面与契约${sameSource ? '已同源' : '未同源'}
        </span>
      </div>
      <p class="muted">页面登记枚举：${(j.page_enum_status || []).join(', ') || '（空）'}</p>
      <p class="muted">契约现行枚举（最新快照）：${(j.contract_enum_status || []).join(', ') || '（空）'}</p>
      <div class="grid">${items.map(s => `
        <article class="card ${s.is_latest ? 'card-live' : ''}">
          <div class="row">
            <strong>快照 #${s.snapshot_id} · ${s.enum_name}</strong>
            ${s.is_latest ? '<span class="chip">现行</span>' : ''}
            <span class="muted">${s.source} · ${s.created_at}</span>
          </div>
          <p>${s.values.join(', ')}</p>
        </article>`).join('') || '<p class="muted">暂无快照</p>'}</div>
    </div>`;
  if (!sameSource) {
    setMsg('页面枚举与契约枚举未同源：请走换版应用收拢扩值', true);
  } else if (latest) {
    setMsg(`已对照最新快照 #${latest.snapshot_id}，页面与契约枚举同源`);
  }
}

function draftOptions() {
  return contracts.filter(c => c.status === '草稿')
    .map(c => `<option value="${c.id}">${c.table_name} v${c.version_no}</option>`).join('');
}

function renderCheck() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-check" type="button">运行破坏性检查</button>
      <pre id="out" class="muted"></pre>
    </div>`;
  document.getElementById('btn-check').onclick = async () => {
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/breaking-check`, {method:'POST', body:'{}'});
      lastCheck = j.check;
      document.getElementById('out').textContent = JSON.stringify(j.check, null, 2);
      setMsg(`检查结论：${j.check.verdict}`);
    } catch (e) { setMsg(e.message, true); }
  };
}

function renderApply() {
  view.innerHTML = `
    <div class="card grid">
      <label>草稿契约
        <select id="cid">${draftOptions()}</select>
      </label>
      <button class="primary" id="btn-apply" type="button">换版应用（一次收拢枚举扩值）</button>
      <pre id="apply-out" class="muted"></pre>
    </div>`;
  document.getElementById('btn-apply').onclick = async () => {
    const out = document.getElementById('apply-out');
    try {
      const cid = document.getElementById('cid').value;
      const j = await api(`/api/contracts/${cid}/apply`, {method:'POST', body:'{}'});
      const exp = j.contract.enum_expansion || [];
      const lines = exp.map(e =>
        `枚举 ${e.enum_name} → 快照 #${e.snapshot_id}`
        + (e.added.length ? `，新收拢：${e.added.join(',')}` : '，无扩值')
      );
      out.textContent = `已应用 ${j.contract.table_name} v${j.contract.version_no}\n`
        + (lines.join('\n') || '无枚举变更');
      setMsg(`已应用，枚举快照号见卡片：${exp.map(e => '#'+e.snapshot_id).join('，') || '无'}`);
      await loadContracts();
    } catch (e) {
      out.textContent = '';
      setMsg(e.message, true);  // 失败吐短句，枚举漏写即停
    }
  };
}

async function renderReconcile() {
  const reports = (await api('/api/reconcile-reports')).items || [];
  const water = (await api('/api/enum-waterline')).waterline;
  const probeText = water.probes
    .map(p => `${p.enum_name}=${p.value} → ${p.accepted ? '可入' : '拒收'}`).join('；');
  view.innerHTML = `
    <div class="card grid">
      <div class="row">
        <button class="primary" id="btn-rec" type="button">跑一轮对账（含水线复验）</button>
        <span class="chip ${water.enum_ok ? '' : 'chip-warn'}">
          枚举水线${water.enum_ok ? '通过' : '失败'} · 快照 #${water.snapshot_id ?? '无'}
        </span>
      </div>
      <p class="muted">页面枚举：${water.page_enum.join(', ') || '（空）'}</p>
      <p class="muted">契约枚举：${water.contract_enum.join(', ') || '（空）'}</p>
      <p class="${water.enum_ok ? 'muted' : 'err'}">复验：${probeText}（${water.detail}）</p>
      <div class="grid" id="reps">${reports.map(r => `
        <article class="card"><strong>${r.as_of}</strong>
        <p>${r.summary}</p></article>`).join('') || '<p class="muted">暂无对账</p>'}</div>
    </div>`;
  document.getElementById('btn-rec').onclick = async () => {
    try {
      const j = await api('/api/reconcile-reports', {
        method:'POST', body: JSON.stringify({as_of: '2026-11-18'})
      });
      setMsg(j.report.summary, !j.report.enum_waterline.enum_ok);
      await renderReconcile();
    } catch (e) { setMsg(e.message, true); }
  };
}

async function show(tab) {
  setMsg('');
  await loadContracts();
  if (tab === 'contracts') renderContracts();
  if (tab === 'enums') await renderEnums();
  if (tab === 'check') renderCheck();
  if (tab === 'apply') renderApply();
  if (tab === 'reconcile') await renderReconcile();
}

document.querySelectorAll('.pills button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('.pills button').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    show(btn.dataset.tab);
  };
});
document.getElementById('btn-logout').onclick = async () => {
  await api('/api/logout', {method:'POST', body:'{}'});
  location.href = '/login';
};
show('contracts');
