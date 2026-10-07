'use strict';
const $ = id => document.getElementById(id);
let offset = 0, data = null, busy = false;
let actions = Promise.resolve();
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
function perform(action, preserveEdits = false) {
  actions = actions.then(async () => {
    busy = true; $('error').hidden = true;
    try { await action(); await load(preserveEdits); }
    catch (error) { $('error').textContent = error.message; $('error').hidden = false; }
    finally { busy = false; }
  });
  return actions;
}
function activeOperation() { return data.operations.find(op=>op.state==='running') || data.operations.find(op=>op.state==='queued'); }
function jump(text, target) {
  const node = element('a', text); node.href = target; return node;
}
function workflow() {
  const active = activeOperation(), ready = data.batches.filter(row => row.state === 'awaiting_review');
  const messages = [];
  if (active?.kind === 'capture') messages.push(active.state === 'queued' ? 'Capture queued; waiting for the worker.' : 'Capture is running. Live progress appears below.');
  else if (active?.kind === 'publish') messages.push(active.state === 'queued' ? 'Publication queued; it will start after the current operation.' : 'Publishing the approved files. AI enrichment and indexing will queue automatically afterward.');
  else if (active) messages.push('Discovery is running. The capture queue will continue when it finishes.');
  if (data.approved) messages.push(`${data.approved} site${data.approved === 1 ? '' : 's'} awaiting automatic capture. New approvals have a ${data.undo_seconds ?? 60}-second grace period. Undo is available in Awaiting capture until the worker starts. There is no queue count limit.`);
  if (data.capture_queue_error) messages.push(`Capture queue paused: ${data.capture_queue_error}`);
  if (data.operations.some(op=>op.kind==='capture' && op.state==='interrupted')) messages.push('The capture queue is paused until the interrupted capture is resumed.');
  if (ready.length) messages.push(`${ready.length} downloaded batch${ready.length === 1 ? '' : 'es'} awaiting your file review and publication approval.`);
  if (!messages.length) messages.push('Review a suggestion’s source and scope, then Approve for capture. It moves to Awaiting capture and downloads automatically after the grace period.');
  const text = messages.join(' ');
  if ($('next-step').textContent !== text) $('next-step').textContent = text;
  $('view-awaiting').disabled = !data.approved;
  $('view-captured').disabled = !data.captured && !ready.length;
  const visible = data.operations.filter(op => ['capture','publish'].includes(op.kind) &&
    (['queued','running','interrupted'].includes(op.state) || op.kind === 'capture' && ready.some(row => row.id === op.payload.batch_id)));
  $('capture-activity').hidden = !visible.length;
  $('activity').replaceChildren(...visible.map(operation));
  if (ready.length) $('activity').append(jump('Review downloaded batches', '#capture-batches'));
}
function age(value) {
  const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 1000));
  if (!Number.isFinite(seconds)) return '';
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}
function operation(op) {
  const card = element('article', undefined, 'card'); card.dataset.operation = op.id || '';
  const labels = {capture:{queued:'Capture queued',running:'Capturing sites',completed:'Capture complete',interrupted:'Capture paused'},
                  publish:{queued:'Publication queued',running:'Publishing approved files',completed:'Publication complete',interrupted:'Publication paused'}};
  card.append(element('h3', labels[op.kind]?.[op.state] || `${op.kind} · ${op.state}`));
  if (op.kind === 'discover') card.append(element('p', `${op.payload.max_candidates} candidates · $${op.payload.max_usd} maximum`, 'meta'));
  const progress = op.result?.progress;
  if (op.kind === 'capture') {
    const phases = {preparing:'Preparing approved sources',checking_wayback:'Checking Wayback for this URL',downloading:'Downloading a capture',ready_for_review:'Downloaded files are ready for review'};
    if (progress) {
      card.append(element('p', phases[progress.phase] || 'Capturing approved scopes', 'capture-phase'));
      card.append(element('p', `${progress.files} HTML files staged · ${(progress.bytes / 1048576).toFixed(2)} MiB · ${progress.urls_checked} new URLs checked`, 'capture-counts'));
      if (progress.site_url && op.state === 'running') card.append(element('p', `Site ${Math.min(progress.sites_done + 1, progress.sites_total)} of ${progress.sites_total} · ${progress.site_url}`, 'meta'));
      if (progress.current_url) card.append(link(progress.current_url));
    } else if (op.state === 'completed') card.append(element('p', `${op.result?.captures ?? 'Downloaded'} HTML files ready for review.`));
    else card.append(element('p', 'The worker will report files as it checks and downloads URLs.'));
    if (['queued','running'].includes(op.state)) {
      const indicator = element('progress'); indicator.setAttribute('aria-label','Capture in progress'); card.append(indicator);
      card.append(element('p','Progress refreshes every 5 seconds. The number of pages is discovered during capture, so a completion percentage is unavailable.','meta'));
    }
    if (op.state === 'completed') card.append(jump('Next: review files and approve publication', '#capture-batches'));
  }
  if (op.kind === 'publish' && ['queued','running'].includes(op.state)) card.append(element('p','Publishing the reviewed files in one archive commit. Git publication can take several minutes.'));
  if (op.error) card.append(element('p',op.error));
  if (age(op.created)) card.append(element('p', `Elapsed ${age(op.created)}${age(op.updated) ? ' · last update '+age(op.updated)+' ago' : ''}`, 'meta'));
  if (op.result?.grading) card.append(element('p',`Estimated spend $${op.result.grading.estimated_usd.toFixed(4)} · conservative reservations $${op.result.grading.reserved_usd.toFixed(4)}`,'meta'));
  if (op.state === 'interrupted') {
    card.append(element('p','Staged files are retained. Resume explicitly to continue within the original budget.'));
    card.append(button('Resume within original budget',()=>request('/api/resume',{id:op.id})));
  }
  return card;
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
  card.dataset.state = row.state; card.dataset.manifest = row.manifest_sha256;
  const rating = row.rating;
  const title = element('h3'); title.append(element('span', rating ? `Grade ${rating.grade}/3` : 'Ungraded', 'badge'), link(row.url)); card.append(title);
  const states = {approved_waiting_batch:'Awaiting capture',capturing:'Capture in progress',captured_awaiting_review:'Downloaded — awaiting publication approval',published:'Published — indexing queued',indexed:'Indexed'};
  card.append(element('div', `${rating?.category?.replaceAll('_',' ') || 'Unclassified'} · ${rating?.confidence || 'No'} confidence · ${states[row.state] || row.state.replaceAll('_',' ')}`, 'meta'));
  card.append(element('p', rating?.reason || row.error || 'Awaiting source acquisition.'));
  const coverage = row.coverage;
  if (coverage?.site_check) {
    const check = coverage.site_check;
    card.append(element('p', `Website/account check: ${check.status.replaceAll('_',' ')}${check.archive_path ? ' · '+check.archive_path : ''}${check.complete === false ? ' · approval blocked until verified' : ''}`, 'meta'));
  }
  if (coverage?.status) card.append(element('p', `Archive coverage: ${coverage.status.replaceAll('_',' ')}${coverage.complete === false ? ' · incomplete inventory' : ''}${coverage.archive_sha ? ' · checked at '+coverage.archive_sha.slice(0,12) : ''}`, 'meta'));
  if (rating) for (const evidence of rating.evidence) card.append(element('blockquote', evidence.excerpt, 'quote'));
  card.append(element('p', `Capture scope: ${row.scope}${row.scope_mode === 'page' ? ' (exact page only)' : ''}`, 'scope'));
  if (row.captures?.length) sources(card, row.captures, row.id);
  if (row.state === 'approved_waiting_batch') {
    const remaining = Math.ceil((Date.parse(row.decision?.capture_after) - Date.now()) / 1000);
    card.append(element('p',remaining > 0 ? `Queued; eligible in ${remaining}s. Undo before capture starts.` : 'Queued; waiting for the worker. You can undo until capture starts.','candidate-next'));
    card.append(button('Undo approval',()=>request('/api/undo',{id:row.id,manifest_sha256:row.manifest_sha256})));
    return card;
  }
  if (['capturing','captured_awaiting_review','published','indexed'].includes(row.state)) {
    const batchId = row.coverage?.capture?.batch_id;
    card.append(jump(row.state==='capturing' ? 'View capture progress' : 'Review captured files and publication status', row.state==='capturing' ? '#capture-activity' : batchId ? '#batch-'+batchId : '#capture-batches'));
    return card;
  }
  const editable = ['approval_pending','deferred','rejected'].includes(row.state);
  const controls = element('div', undefined, 'actions');
  const scope = element('select'); scope.setAttribute('aria-label', 'Capture scope'); scope.disabled = !editable;
  [['directory','Linked directory and below'],['page','Linked page only'],['site','Whole site / shared account'],['custom','Custom folder and below']].forEach(([value,label]) => {
    const option = element('option',label); option.value = value; scope.append(option);
  }); scope.value = row.scope_mode;
  controls.append(scope);
  const custom = element('div', undefined, 'custom-scope');
  const pathLabel = element('label', 'Custom capture folder path'), path = element('input');
  path.type = 'text'; path.placeholder = '/eq/research/'; path.setAttribute('aria-label','Custom capture folder path');
  path.value = new URL(row.scope).pathname; path.disabled = !editable;
  pathLabel.append(path); custom.append(pathLabel);
  custom.append(button('Save custom scope', () => request('/api/scope',{id:row.id,manifest_sha256:row.manifest_sha256,mode:'custom',path:path.value}), !editable),
                element('p','Uses this folder and its descendants. Save this scope before approving capture.','meta'));
  custom.hidden = row.scope_mode !== 'custom'; controls.append(custom);
  let approveButton;
  function draftScope() {
    custom.dataset.scopeDraft='1';
    if (approveButton) approveButton.disabled = true;
  }
  path.addEventListener('input',draftScope);
  scope.addEventListener('change', () => {
    custom.hidden = scope.value !== 'custom';
    if (scope.value === 'custom') draftScope();
    else perform(() => request('/api/scope',{id:row.id, manifest_sha256:row.manifest_sha256, mode:scope.value}));
  });
  for (const [decision, label] of [['approve','Approve for capture'],['reject','Reject'],['defer','Defer']]) {
    const action = button(label, async () => {
      await request('/api/decisions',[{id:row.id,manifest_sha256:row.manifest_sha256,decision}]);
    }, !editable || decision === 'approve' && !rating);
    if (decision === 'approve') approveButton=action;
    controls.append(action);
  }
  if (row.state === 'coverage_unverified') controls.append(button('Recheck archive coverage', () => request('/api/coverage',{id:row.id,manifest_sha256:row.manifest_sha256})));
  card.append(controls); return card;
}
function batchStatus(row) {
  const status = element('div', undefined, 'batch-status');
  const labels = {capturing:'Capturing approved scopes',awaiting_review:'Downloaded — review required',publication_requested:'Publishing approved files',published_waiting_index:'Published — waiting to index',indexing:'AI enrichment and indexing in progress',indexed:'Indexed — complete',index_failed:'Indexing failed — attention needed'};
  status.append(element('h3', `Batch ${row.id.slice(0,8)} · ${labels[row.state] || row.state.replaceAll('_',' ')}`));
  const step = {capturing:0,awaiting_review:1,publication_requested:2,published_waiting_index:3,indexing:3,index_failed:3,indexed:4}[row.state] ?? 0;
  const stages = element('ol', undefined, 'flow'); stages.setAttribute('aria-label','Batch progress');
  ['Capture','Review files','Publish','Enrich & index'].forEach((label,index) => {
    const stage = element('li', `${index < step ? '✓ ' : ''}${label}`, index < step ? 'done' : index === step ? 'current' : '');
    if (index === step) stage.setAttribute('aria-current','step'); stages.append(stage);
  }); status.append(stages);
  const next = {capturing:'Downloads are staged here. Live capture progress appears above.',awaiting_review:'Next: review the downloaded files below, then approve publication. No archive commit has been made yet.',publication_requested:'Publishing the reviewed file set. Indexing queues automatically after publication.',published_waiting_index:'AI enrichment and indexing will start automatically when other indexing Jobs finish.',indexing:'The import Job includes Luna enrichment and indexing. Its completion is checked automatically.',indexed:'This batch is published and indexed.',index_failed:'Inspect the failed import Job before retrying. The published files are retained.'};
  status.append(element('p',next[row.state] || 'Awaiting the next workflow step.'));
  if (row.error) status.append(element('p',row.error));
  if (row.job?.waiting_for?.length) status.append(element('p',`Waiting for existing Jobs: ${row.job.waiting_for.join(', ')}`));
  if (row.job?.name) status.append(element('p',`Indexing Job: ${row.job.name}`,'meta'));
  if (row.publication?.commit) status.append(link(`https://github.com/dbsanfte/eq-archives/commit/${row.publication.commit}`,'View archive commit'));
  return status;
}
function batch(row) {
  const card = element('article', undefined, 'card'); card.dataset.batch = row.id;
  card.id = 'batch-'+row.id;
  card.dataset.signature = `${row.state}:${row.manifest_sha256}`;
  card.append(batchStatus(row));
  if (row.manifest) {
    const captures = row.manifest.captures;
    const slots = new Set(captures.map((_,index)=>index));
    let publish;
    card.append(element('p', `${captures.length} HTML files · ${(captures.reduce((total,c)=>total+c.bytes,0)/1048576).toFixed(2)} MiB`, 'meta'));
    card.append(element('p', `AI enrichment on import: Luna summaries, categories, tags and supported date estimates · $${row.manifest.indexing?.max_enrichment_usd ?? 2} maximum for this batch.`, 'meta'));
    const details = element('details'); details.append(element('summary','Review archive file set'));
    captures.forEach((capture,slot) => {
      const file = element('div',undefined,'file');
      if (row.state==='awaiting_review') {
        const label=element('label','Include this capture'),checkbox=element('input'); checkbox.type='checkbox'; checkbox.checked=true;
        checkbox.addEventListener('change',()=>{if(checkbox.checked)slots.add(slot);else slots.delete(slot);if(publish){publish.dataset.selectedCount=slots.size;publish.disabled=!slots.size;}});
        label.prepend(checkbox);file.append(label);
      }
      file.append(element('div',capture.archive_path), link(`https://web.archive.org/web/${capture.timestamp}/${capture.url}`),element('div',`Tier ${capture.tier} · SHA256 ${capture.sha256}`,'meta')); details.append(file);
    }); card.append(details); sources(card,captures,null,row.id);
    if (row.manifest.notes?.length) for (const note of row.manifest.notes) card.append(element('p',`${note.url}: ${note.note}`,'meta'));
    if (row.state === 'awaiting_review') {
      publish = button('Approve publication & queue indexing', () => request('/api/publish',{id:row.id,manifest_sha256:row.manifest_sha256,slots:[...slots]}), !slots.size);
      publish.className='primary'; publish.dataset.selectedCount=slots.size; card.append(publish);
    }
  } return card;
}
async function load(preserveEdits = false) {
  for (;;) {
    const filter = $('filter').value, requestedOffset = offset;
    const result = await request(`/api/queue?filter=${encodeURIComponent(filter)}&offset=${requestedOffset}`);
    if (filter !== $('filter').value || requestedOffset !== offset) continue;
    if (offset > 0 && offset >= result.total) {
      offset = Math.max(0, Math.floor((result.total - 1) / 50) * 50);
      continue;
    }
    data = result;
    break;
  }
  $('status').textContent = `${data.all_count} candidates · ${data.total} in this view · ${data.approved} awaiting capture · ${data.capturing ?? 0} capturing · ${data.captured ?? 0} captured`;
  const previousCandidates = new Map([...$('candidates').querySelectorAll('[data-candidate]')].map(card=>[card.dataset.candidate,card]));
  $('candidates').replaceChildren(...data.candidates.map(row=>{
    const card = previousCandidates.get(row.id);
    return preserveEdits && card?.dataset.state===row.state && card.dataset.manifest===row.manifest_sha256 &&
      card.querySelector('details[open], [data-scope-draft]') ? card : candidate(row);
  }));
  if (!data.candidates.length) $('candidates').append(element('p','No candidates in this view.'));
  const previousBatches = new Map([...$('batches').querySelectorAll('[data-batch]')].map(card=>[card.dataset.batch,card]));
  $('batches').replaceChildren(...data.batches.map(row => {
    const card = previousBatches.get(row.id);
    if (!card || card.dataset.signature !== `${row.state}:${row.manifest_sha256}`) return batch(row);
    card.querySelector('.batch-status').replaceWith(batchStatus(row));
    const publish = card.querySelector('[data-selected-count]');
    if (publish) publish.disabled = !Number(publish.dataset.selectedCount);
    return card;
  }));
  if (!data.batches.length) $('batches').append(element('p','No capture batches yet.'));
  $('operations').replaceChildren(...data.operations.map(operation));
  $('version').textContent=`Build ${data.version}`;
  $('page').textContent=data.total ? `${offset+1}–${Math.min(offset+50,data.total)} of ${data.total}` : '0 candidates';
  $('previous').disabled=offset===0; $('next').disabled=offset+50>=data.total;
  $('discover').disabled=Boolean(activeOperation()); workflow();
}
$('filter').addEventListener('change',()=>perform(async()=>{offset=0;}));
$('previous').addEventListener('click',()=>perform(async()=>{offset=Math.max(0,offset-50);}));
$('next').addEventListener('click',()=>perform(async()=>{offset+=50;}));
$('refresh').addEventListener('click',()=>perform(async()=>{}));
$('discover').addEventListener('click',()=>perform(()=>request('/api/discover',{max_candidates:50,max_usd:2})));
$('view-awaiting').addEventListener('click',()=>perform(async()=>{$('filter').value='approved';offset=0;}));
$('view-captured').addEventListener('click',()=>perform(async()=>{$('filter').value='captured';offset=0;}).then(()=>$('capture-batches').scrollIntoView()));
perform(async()=>{});
setInterval(()=>{if (!busy && data &&
  (data.approved || activeOperation() || data.batches.some(batch=>['published_waiting_index','indexing'].includes(batch.state)))) perform(async()=>{},true);},5000);
