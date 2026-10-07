"""The ops page, served inline so there is no build step and no static host.

Meera's three executives need two things: to read what happened to a rider, and
to clear what is waiting. So the page is a queue on the left and a trace on the
right, and nothing else.
"""

OPS_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rider Payout Dispute Desk - Ops</title>
<style>
  :root {
    --bg: #f6f7f9; --panel: #fff; --ink: #14171a; --muted: #5b6672;
    --line: #e3e6ea; --accent: #1b6ef3; --pay: #0b8457; --warn: #b4530a;
    --mono: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: var(--bg); color: var(--ink); }
  header { background: var(--panel); border-bottom: 1px solid var(--line);
           padding: 14px 20px; display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; }
  header h1 { font-size: 16px; margin: 0; }
  header .meta { color: var(--muted); font-size: 13px; }
  .wrap { display: grid; grid-template-columns: 420px 1fr; gap: 16px; padding: 16px; align-items: start; }
  @media (max-width: 900px) { .wrap { grid-template-columns: 1fr; } }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
  .panel h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em;
              color: var(--muted); margin: 0; padding: 12px 14px; border-bottom: 1px solid var(--line); }
  .item { padding: 12px 14px; border-bottom: 1px solid var(--line); }
  .item:last-child { border-bottom: 0; }
  .row { display: flex; justify-content: space-between; gap: 10px; align-items: baseline; }
  .rider { font-weight: 600; font-family: var(--mono); }
  .tag { font-size: 11px; padding: 2px 7px; border-radius: 10px; border: 1px solid var(--line);
         text-transform: uppercase; letter-spacing: .03em; color: var(--muted); }
  .tag.approval { color: var(--pay); border-color: #bfe4d4; background: #f0faf5; }
  .tag.escalation { color: var(--warn); border-color: #f0ddc4; background: #fdf6ed; }
  .amount { font-family: var(--mono); font-weight: 600; }
  .reason { color: var(--muted); margin: 6px 0 10px; }
  button { font: inherit; padding: 5px 11px; border-radius: 6px; border: 1px solid var(--line);
           background: #fff; cursor: pointer; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button:disabled { opacity: .5; cursor: default; }
  .empty { padding: 22px 14px; color: var(--muted); }
  .conv { padding: 10px 14px; border-bottom: 1px solid var(--line); cursor: pointer; }
  .conv:hover { background: #fafbfc; }
  .conv.active { background: #eef4fe; }
  .turn { margin: 8px 0; }
  .turn .who { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  .turn .body { padding: 7px 10px; border-radius: 7px; background: #f4f6f8; }
  .turn.agent .body { background: #eef4fe; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
  td.t { font-family: var(--mono); white-space: nowrap; color: var(--muted); }
  .steptype { font-family: var(--mono); font-size: 12px; }
  .steptype.decision { color: var(--accent); }
  .steptype.tool_call { color: var(--pay); }
  .steptype.error { color: #c0392b; }
  details > summary { cursor: pointer; color: var(--muted); font-size: 12px; }
  pre { margin: 6px 0 0; padding: 8px; background: #f4f6f8; border-radius: 6px;
        overflow-x: auto; font-family: var(--mono); font-size: 12px; }
  .rule { font-family: var(--mono); font-size: 12px; }
  .ledger { margin: 0 16px 16px; }
  .totals { display: flex; gap: 20px; flex-wrap: wrap; padding: 12px 14px;
            border-bottom: 1px solid var(--line); }
  .totals div { font-size: 13px; color: var(--muted); }
  .totals b { display: block; font-size: 18px; color: var(--ink);
              font-family: var(--mono); font-weight: 600; }
  .alarm { padding: 10px 14px; border-bottom: 1px solid var(--line);
           background: #fdf6ed; color: var(--warn); font-size: 13px; }
  .alarm.ok { background: #f0faf5; color: var(--pay); }
  .alarm code { font-family: var(--mono); font-size: 12px; }
  .src { font-size: 11px; padding: 1px 6px; border-radius: 9px; border: 1px solid var(--line);
         font-family: var(--mono); color: var(--muted); }
  .src.agent { color: var(--accent); border-color: #c5d9fb; background: #f2f6fe; }
  .src.ops { color: var(--pay); border-color: #bfe4d4; background: #f0faf5; }
  .src.external { color: var(--warn); border-color: #f0ddc4; background: #fdf6ed; }
  .num { font-family: var(--mono); text-align: right; white-space: nowrap; }
</style>
</head>
<body>
<header>
  <h1>Rider Payout Dispute Desk</h1>
  <span class="meta" id="meta">loading...</span>
  <span style="flex:1"></span>
  <a href="/chat" style="color:var(--accent);text-decoration:none;font-size:13px;margin-right:6px">Message simulator &rarr;</a>
  <button onclick="refresh()">Refresh</button>
</header>

<div class="wrap">
  <div>
    <div class="panel" style="margin-bottom:16px">
      <h2>Waiting for ops <span id="count"></span></h2>
      <div id="pending"><div class="empty">Loading...</div></div>
    </div>
    <div class="panel">
      <h2>Conversations</h2>
      <div id="convs"><div class="empty">Loading...</div></div>
    </div>
  </div>
  <div class="panel">
    <h2 id="traceHead">Select a rider</h2>
    <div id="detail"><div class="empty">Pick a conversation to see what the agent did and why.</div></div>
  </div>
</div>

<div class="panel ledger">
  <h2>PaySwift ledger <span id="ledgerCount"></span></h2>
  <div id="ledger"><div class="empty">Loading...</div></div>
</div>

<script>
let selected = null;
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function refresh() {
  const [health, pending, convs, ledger] = await Promise.all([
    fetch('/health').then(r => r.json()).catch(() => ({})),
    fetch('/ops/pending').then(r => r.json()).catch(() => []),
    fetch('/ops/conversations').then(r => r.json()).catch(() => []),
    fetch('/ops/ledger').then(r => r.json()).catch(() => null),
  ]);

  document.getElementById('meta').textContent =
    `${health.trips_loaded ?? 0} trips - ${health.riders_loaded ?? 0} riders - ` +
    `PaySwift ${health.payswift_reachable ? 'reachable' : 'unreachable'} - ` +
    `model ${health.llm_configured ? 'configured' : 'off (rules only)'}`;

  document.getElementById('count').textContent = pending.length ? `(${pending.length})` : '';
  document.getElementById('pending').innerHTML = pending.length
    ? pending.map(renderPending).join('')
    : '<div class="empty">Nothing waiting. </div>';

  document.getElementById('convs').innerHTML = convs.length
    ? convs.map(c => `<div class="conv ${c.rider_id === selected ? 'active' : ''}" onclick="select('${c.rider_id}')">
         <div class="row"><span class="rider">${esc(c.rider_id)}</span>
         <span class="tag">${c.steps} steps</span></div>
         <div class="reason">${esc(c.name || '')}${c.pending.length ? ` - ${c.pending.length} waiting` : ''}</div>
       </div>`).join('')
    : '<div class="empty">No conversations yet. POST a message to /messages.</div>';

  renderLedger(ledger);

  if (selected) showRider(selected, convs);
}

function renderLedger(l) {
  const host = document.getElementById('ledger');
  const countEl = document.getElementById('ledgerCount');
  if (!l) {
    countEl.textContent = '';
    host.innerHTML = '<div class="empty">Could not read the ledger.</div>';
    return;
  }

  countEl.textContent = `(${l.payout_count})`;

  const totals = `<div class="totals">
    <div>Paid<b>Rs ${l.total_paid}</b></div>
    <div>Payouts<b>${l.payout_count}</b></div>
    <div>Riders<b>${l.rider_count}</b></div>
    <div>PaySwift<b>${l.payswift_reachable ? 'reachable' : 'DOWN'}</b></div>
    <div>Reconciling<b>${l.payments_reconciling}</b></div>
  </div>`;

  // The three questions ops has before trusting a number.
  const alarms = [];
  if (l.unconfirmed.length) alarms.push(`<div class="alarm">
    <b>${l.unconfirmed.length} payout(s) sent but not yet confirmed by PaySwift.</b>
    Being retried in the background with the same idempotency key, so they cannot pay twice.
    ${l.unconfirmed.map(u => `<div><code>${esc(u.rider_id)} Rs${u.amount} ${esc(u.dispute_id)}</code></div>`).join('')}
  </div>`);
  if (l.missing_from_ledger.length) alarms.push(`<div class="alarm">
    <b>${l.missing_from_ledger.length} payout(s) we recorded as paid are not in the ledger.</b>
    In production this needs a human. Here it usually means the sandbox restarted - its ledger is in memory.
    ${l.missing_from_ledger.map(u => `<div><code>${esc(u.rider_id)} Rs${u.amount} ${esc(u.payout_id || u.dispute_id)}</code></div>`).join('')}
  </div>`);
  if (l.unattributed.length) alarms.push(`<div class="alarm">
    <b>${l.unattributed.length} payout(s) in the ledger that no dispute of ours explains.</b>
    Paid outside this agent.
  </div>`);
  if (!alarms.length && l.payout_count) alarms.push(
    `<div class="alarm ok">Ledger reconciles: every payout traces to a dispute, and nothing we sent is unaccounted for.</div>`);

  const rows = l.payouts.map(p => `<tr>
      <td class="rider">${esc(p.rider_id)}</td>
      <td class="num">Rs ${p.amount}</td>
      <td><span class="src ${esc(p.source)}">${esc(p.source)}</span></td>
      <td class="rule">${esc(p.rule || '-')}</td>
      <td class="rule">${esc(p.dispute_id || '-')}</td>
      <td class="t">${esc((p.created_at || '').replace('T', ' ').replace('Z', ''))}</td>
      <td class="rule">${esc(p.payout_id)}</td>
    </tr>`).join('');

  host.innerHTML = totals + alarms.join('') + (l.payout_count
    ? `<div style="overflow-x:auto"><table><thead><tr>
         <th>Rider</th><th class="num">Amount</th><th>Source</th><th>Authorised by</th>
         <th>Dispute</th><th>When (UTC)</th><th>Payout id</th></tr></thead>
       <tbody>${rows}</tbody></table></div>`
    : '<div class="empty">No payouts yet.</div>');
}

function renderPending(i) {
  return `<div class="item">
    <div class="row">
      <span><span class="rider">${esc(i.rider_id)}</span>
        <span class="tag ${esc(i.type)}">${esc(i.type)}</span></span>
      ${i.amount ? `<span class="amount">Rs ${i.amount}</span>` : ''}
    </div>
    <div class="reason">${esc(i.reason)}</div>
    <div>
      ${i.type === 'approval'
        ? `<button class="primary" onclick="act('${i.id}','approve',this)">Approve &amp; pay</button>`
        : ''}
      <button onclick="act('${i.id}','reject',this)">${i.type === 'approval' ? 'Reject' : 'Close'}</button>
      <button onclick="select('${i.rider_id}')">Open trace</button>
    </div>
  </div>`;
}

async function act(id, what, btn) {
  btn.disabled = true; btn.textContent = what === 'approve' ? 'Paying...' : 'Closing...';
  const r = await fetch(`/ops/${id}/${what}`, { method: 'POST' }).then(r => r.json()).catch(() => null);
  if (r && what === 'approve' && r.paid === false) alert('PaySwift did not confirm. Item stays pending.');
  refresh();
}

function select(rider) { selected = rider; refresh(); }

async function showRider(rider, convs) {
  const conv = convs.find(c => c.rider_id === rider);
  const trace = await fetch(`/trace/${rider}`).then(r => r.json()).catch(() => []);
  document.getElementById('traceHead').textContent =
    `${rider}${conv && conv.name ? ' - ' + conv.name : ''} - ${trace.length} steps`;

  const chat = (conv ? conv.turns : []).map(t => `
    <div class="turn"><div class="who">rider</div><div class="body">${esc(t.text)}</div></div>
    <div class="turn agent"><div class="who">agent</div><div class="body">${esc(t.reply)}</div></div>`).join('');

  const rows = trace.map(s => `<tr>
      <td class="t">${esc((s.at || '').slice(11, 19))}</td>
      <td><span class="steptype ${esc(s.type)}">${esc(s.type)}</span></td>
      <td><span class="rule">${esc(s.name)}</span>
        ${s.type === 'decision' && s.output ? `<div class="reason">${esc(s.output.reason || '')}</div>` : ''}
        ${s.type === 'reply' ? `<div class="reason">${esc(s.output)}</div>` : ''}
        <details><summary>detail</summary><pre>${esc(JSON.stringify({input: s.input, output: s.output}, null, 1))}</pre></details>
      </td></tr>`).join('');

  document.getElementById('detail').innerHTML = `
    <div style="padding:12px 14px;border-bottom:1px solid var(--line)">${chat || '<span class="empty">No turns.</span>'}</div>
    <table><thead><tr><th>Time</th><th>Type</th><th>Step</th></tr></thead><tbody>${rows}</tbody></table>`;
}

refresh();
setInterval(refresh, 8000);
</script>
</body>
</html>
"""
