'use strict';
const $ = id => document.getElementById(id);
let offset = 0, data = null, busy = false;
const selected = new Set();
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function link(url, text) {
  const node = element('a', text || url);
  if (/^https?:\/\//.test(url)) { node.href = url; node.target = '_blank'; node.rel = 'noopener noreferrer'; }
  return node;
}
function button(text, action, disabled = false) {
  const node = element('button', text);
  node.disabled = disabled;
  node.addEventListener('click', () => perform(action));
  return node;
}
async function request(path, value) {
  const response = await fetch(path, value === undefined ? {} : {
    method: 'POST', headers: {'Content-Type':'application/json', 'X-Curation-Request':'1'}, body: JSON.stringify(value)
  });
  let result;
  try { result = await response.json(); } catch (_) { throw new Error('The review service is unavailable. Refresh to retry.'); }
  if (!response.ok) throw new Error(result.error || 'The action could not be completed.');
  return result;
}
async function perform(action) {
  if (busy) return;
  busy = true; $('error').hidden = true;
  try { await action(); await load(); }
  catch (error) { $('error').textContent = error.message; $('error').hidden = false; }
  finally { busy = false; }
}
function selection() {
  $('selection').textContent = `${selected.size} selected (maximum 5)`;
  $('capture').disabled = !selected.size || selected.size > 5 || data?.operations.some(op => ['queued','running'].includes(op.state));
}
function sources(container, captures, candidate, batch) {
  const details = element('details'); details.append(element('summary', `Review complete extracted source · ${captures.length} capture(s)`));
  const controls = element('div', undefined, 'source-controls'), select = element('select');
  select.setAttribute('aria-label', 'Source capture');
  captures.forEach((capture, slot) => {
    const option = element('option', `${capture.timestamp} · ${capture.url}`); option.value = slot; select.append(option);
  });
  const text = element('pre', 'Select a capture to read its complete extracted text.');
  let generation = 0;
  async function read() {
    const current = ++generation, slot = select.value;
    text.textContent = 'Loading verified source…';
    try {
      const source = await request(`/api/source?${batch ? 'batch' : 'candidate'}=${encodeURIComponent(batch || candidate)}&slot=${slot}`);
      if (current === generation) text.textContent = source.complete_extracted_text;
    } catch (error) { if (current === generation) text.textContent = error.message; }
  }
  const reread = element('button','Read source'); reread.addEventListener('click',read);
  controls.append(select, reread); details.append(controls, text);
  details.addEventListener('toggle', () => { if (details.open && generation === 0) read(); });
  select.addEventListener('change', read); container.append(details);
}
function candidate(row) {
  const card = element('article', undefined, 'card'); card.dataset.candidate = row.id;
  const rating = row.rating;
  const title = element('h3'); title.append(element('span', rating ? `Grade ${rating.grade}/3` : 'Ungraded', 'badge'), link(row.url)); card.append(title);
  card.append(element('div', `${rating?.category?.replaceAll('_',' ') || 'Unclassified'} · ${rating?.confidence || 'No'} confidence · ${row.state.replaceAll('_',' ')}`, 'meta'));
  card.append(element('p', rating?.reason || row.error || 'Awaiting source acquisition.'));
  const coverage = row.coverage;
  if (coverage?.status) card.append(element('p', `Archive coverage: ${coverage.status.replaceAll('_',' ')}${coverage.complete === false ? ' · incomplete inventory' : ''}${coverage.archive_sha ? ' · checked at '+coverage.archive_sha.slice(0,12) : ''}`, 'meta'));
  if (rating) for (const evidence of rating.evidence) card.append(element('blockquote', evidence.excerpt, 'quote'));
  card.append(element('p', `Capture scope: ${row.scope}${row.scope_mode === 'page' ? ' (exact page only)' : ''}`, 'scope'));
  if (row.captures?.length) sources(card, row.captures, row.id);
  const editable = ['approval_pending','approved_waiting_batch','deferred','rejected'].includes(row.state);
  const controls = element('div', undefined, 'actions');
  const scope = element('select'); scope.setAttribute('aria-label', 'Capture scope'); scope.disabled = !editable;
  [['directory','Linked directory and below'],['page','Linked page only'],['site','Whole site / shared account']].forEach(([value,label]) => {
    const option = element('option',label); option.value = value; scope.append(option);
  }); scope.value = row.scope_mode;
  scope.addEventListener('change', () => perform(() => request('/api/scope',{id:row.id, manifest_sha256:row.manifest_sha256, mode:scope.value})));
  controls.append(scope);
  for (const [decision, label] of [['approve','Approve capture'],['reject','Reject'],['defer','Defer']]) {
    controls.append(button(label, () => request('/api/decisions',[{id:row.id,manifest_sha256:row.manifest_sha256,decision}]), !editable || decision === 'approve' && !rating));
  }
  if (row.state === 'approved_waiting_batch') {
    const label = element('label', 'Select for batch'), check = element('input'); check.type='checkbox'; check.checked=selected.has(row.id);
    check.addEventListener('change', () => { if (check.checked) selected.add(row.id); else selected.delete(row.id); selection(); }); label.prepend(check); controls.append(label);
  }
  card.append(controls); return card;
}
function batch(row) {
  const card = element('article', undefined, 'card'); card.dataset.batch = row.id;
  card.append(element('h3', `Batch ${row.id.slice(0,8)} · ${row.state.replaceAll('_',' ')}`));
  if (row.error) card.append(element('p',row.error));
  if (row.job?.waiting_for?.length) card.append(element('p',`Indexing waits for: ${row.job.waiting_for.join(', ')}`));
  if (row.job?.name) card.append(element('p',`Indexing Job: ${row.job.name}`,'meta'));
  if (row.publication?.commit) card.append(link(`https://github.com/dbsanfte/eq-archives/commit/${row.publication.commit}`,'View archive commit'));
  if (row.manifest) {
    const captures = row.manifest.captures;
    const slots = new Set(captures.map((_,index)=>index));
    let publish;
    card.append(element('p', `${captures.length} HTML files · ${(captures.reduce((total,c)=>total+c.bytes,0)/1048576).toFixed(2)} MiB`, 'meta'));
    const details = element('details'); details.append(element('summary','Review archive file set'));
    captures.forEach((capture,slot) => {
      const file = element('div',undefined,'file');
      if (row.state==='awaiting_review') {
        const label=element('label','Include this capture'),checkbox=element('input'); checkbox.type='checkbox'; checkbox.checked=true;
        checkbox.addEventListener('change',()=>{if(checkbox.checked)slots.add(slot);else slots.delete(slot);if(publish)publish.disabled=!slots.size;});
        label.prepend(checkbox);file.append(label);
      }
      file.append(element('div',capture.archive_path), link(`https://web.archive.org/web/${capture.timestamp}/${capture.url}`),element('div',`Tier ${capture.tier} · SHA256 ${capture.sha256}`,'meta')); details.append(file);
    }); card.append(details); sources(card,captures,null,row.id);
    if (row.manifest.notes?.length) for (const note of row.manifest.notes) card.append(element('p',`${note.url}: ${note.note}`,'meta'));
    if (row.state === 'awaiting_review') {
      publish = button('Approve publication & queue indexing', () => request('/api/publish',{id:row.id,manifest_sha256:row.manifest_sha256,slots:[...slots]}), data.operations.some(op=>['queued','running'].includes(op.state)));
      publish.className='primary'; card.append(publish);
    }
  } return card;
}
async function load() {
  data = await request(`/api/queue?filter=${encodeURIComponent($('filter').value)}&offset=${offset}`);
  $('status').textContent = `${data.all_count} candidates · ${data.total} in this view · ${data.approved} approved for capture`;
  $('candidates').replaceChildren(...data.candidates.map(candidate));
  if (!data.candidates.length) $('candidates').append(element('p','No candidates in this view.'));
  for (const id of [...selected]) if (data.candidates.some(row=>row.id===id && row.state!=='approved_waiting_batch')) selected.delete(id);
  $('batches').replaceChildren(...data.batches.map(batch));
  if (!data.batches.length) $('batches').append(element('p','No capture batches yet.'));
  $('operations').replaceChildren(...data.operations.map(op => {
    const card = element('article',undefined,'card'); card.append(element('h3',`${op.kind} · ${op.state}`));
    if (op.kind==='discover') card.append(element('p',`${op.payload.max_candidates} candidates · $${op.payload.max_usd} maximum`,'meta'));
    if (op.error) card.append(element('p',op.error));
    if (op.result?.grading) card.append(element('p',`Estimated spend $${op.result.grading.estimated_usd.toFixed(4)} · conservative reservations $${op.result.grading.reserved_usd.toFixed(4)}`,'meta'));
    if (op.state==='interrupted') card.append(button('Resume within original budget',()=>request('/api/resume',{id:op.id})));
    return card;
  }));
  $('version').textContent=`Build ${data.version}`;
  $('page').textContent=data.total ? `${offset+1}–${Math.min(offset+50,data.total)} of ${data.total}` : '0 candidates';
  $('previous').disabled=offset===0; $('next').disabled=offset+50>=data.total;
  $('discover').disabled=data.operations.some(op=>['queued','running'].includes(op.state)); selection();
}
$('filter').addEventListener('change',()=>perform(async()=>{offset=0;selected.clear();}));
$('previous').addEventListener('click',()=>perform(async()=>{offset=Math.max(0,offset-50);}));
$('next').addEventListener('click',()=>perform(async()=>{offset+=50;}));
$('refresh').addEventListener('click',()=>perform(async()=>{}));
$('discover').addEventListener('click',()=>perform(()=>request('/api/discover',{max_candidates:50,max_usd:2})));
$('capture').addEventListener('click',()=>perform(async()=>{await request('/api/capture',{ids:[...selected]});selected.clear();}));
perform(async()=>{});
setInterval(()=>{if (!busy && data && !document.querySelector('details[open]') &&
  (data.operations.some(op=>['queued','running'].includes(op.state)) || data.batches.some(batch=>['published_waiting_index','indexing'].includes(batch.state)))) perform(async()=>{});},5000);
