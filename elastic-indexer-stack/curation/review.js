'use strict';
const $ = id => document.getElementById(id);
let offset = 0, data = null, busy = false;
let actions = Promise.resolve();
let selectedSite = null;
let renderedView = null;
const scrollWidgets = new Map();
const capturedStates = ['captured_awaiting_review','approved_waiting_publication','indexing_declined','published','indexed'];
const viewNames = {recommended:'Suggestions',approved:'Awaiting capture',capturing:'Capturing',captured:'Review & indexing',pending:'All awaiting review',all:'All candidates'};
const viewDescriptions = {
  recommended:'Choose sites to capture. Review the source, set the scope, then approve the download.',
  approved:'Approved sites wait here until capture starts. You can undo an approval while it is still queued.',
  capturing:'Follow the current download. Completed sites move to Review & indexing automatically.',
  captured:'Choose a captured site, browse its pages, then decide whether to publish and index the whole site.',
  pending:'Review candidates that still need a capture decision.',
  all:'Browse all candidates, including deferred, rejected, queued, and captured sites.'
};
const restored = new URLSearchParams(location.search);
if (Object.hasOwn(viewNames,restored.get('view'))) $('filter').value=restored.get('view');
if ($('filter').value==='captured' && /^[a-f0-9]{32}$/.test(restored.get('site')) && /^[a-f0-9]{24}$/.test(restored.get('candidate'))) {
  selectedSite={id:restored.get('site'),candidate:restored.get('candidate')};
}
function saveView() {
  const url=new URL(location.href);
  url.searchParams.set('view',$('filter').value);
  if ($('filter').value==='captured' && selectedSite) {
    url.searchParams.set('site',selectedSite.id);url.searchParams.set('candidate',selectedSite.candidate);
  } else { url.searchParams.delete('site');url.searchParams.delete('candidate'); }
  history.replaceState(null,'',url);
}
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
function updateScrollControls() {
  for (const [target, update] of scrollWidgets) {
    if (!target.isConnected) scrollWidgets.delete(target);
    else update();
  }
}
function revealIn(container,item) {
  const bounds=container.getBoundingClientRect(),target=item.getBoundingClientRect();
  if (target.top<bounds.top) container.scrollTop+=target.top-bounds.top;
  else if (target.bottom>bounds.bottom) container.scrollTop+=target.bottom-bounds.bottom;
}
function scrollControls(target, name) {
  const controls=element('div',undefined,'scroll-controls'), hint=element('p',undefined,'scroll-hint');
  target.classList.add('scrollable');target.tabIndex=0;target.setAttribute('aria-label',name);
  const up=element('button','↑'),down=element('button','↓');
  up.setAttribute('aria-label',`Scroll ${name.toLowerCase()} up`);
  down.setAttribute('aria-label',`Scroll ${name.toLowerCase()} down`);
  up.addEventListener('click',()=>target.scrollBy({top:-target.clientHeight*.8,behavior:'instant'}));
  down.addEventListener('click',()=>target.scrollBy({top:target.clientHeight*.8,behavior:'instant'}));
  function update() {
    const overflow=target.scrollHeight>target.clientHeight+2;
    up.disabled=!overflow || target.scrollTop<=1;
    down.disabled=!overflow || target.scrollTop+target.clientHeight>=target.scrollHeight-2;
    const noun=name==='Document text' ? 'text' : name.toLowerCase().replace('captured ','');
    hint.textContent=!overflow ? `All ${noun} shown` : down.disabled ? `End of ${noun} · scroll up for more` : `More ${noun} below · scroll to explore`;
  }
  target.addEventListener('scroll',update,{passive:true});scrollWidgets.set(target,update);
  controls.append(hint,up,down);requestAnimationFrame(updateScrollControls);return controls;
}
function stateTone(state) {
  return state==='indexed' ? 'complete' : ['index_failed','interrupted'].includes(state) ? 'attention' :
    ['publication_requested','published_waiting_index','indexing'].includes(state) ? 'active' : state==='indexing_declined' ? 'muted' : 'review';
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
  const active = activeOperation(), ready = data.awaiting_site_review ?? data.batches.filter(row => row.state === 'awaiting_review').length;
  const reviewing=$('filter').value==='captured';
  const messages = [];
  const paused = data.operations.find(op=>op.kind==='publish' && op.state==='interrupted');
  if (paused) messages.push('Publication paused for an approved site. Open Review & indexing to retry; its sources are retained.');
  if (active?.kind === 'capture') messages.push(active.state === 'queued' ? 'Capture queued; waiting for the worker.' : reviewing ? `Capture running in the background: ${active.result?.progress?.files ?? 0} HTML files staged.` : 'Capture is running. Live progress appears below.');
  else if (active?.kind === 'publish') messages.push(active.state === 'queued' ? 'Publication queued; it will start after the current operation.' : 'Publishing the approved files. AI enrichment and indexing will queue automatically afterward.');
  else if (active) messages.push('Discovery is running. The capture queue will continue when it finishes.');
  if (data.approved) messages.push(`${data.approved} site${data.approved === 1 ? '' : 's'} awaiting automatic capture. New approvals have a ${data.undo_seconds ?? 60}-second grace period. Undo is available in Awaiting capture until the worker starts. There is no queue count limit.`);
  if (data.capture_queue_error) messages.push(`Capture queue paused: ${data.capture_queue_error}`);
  if (data.operations.some(op=>op.kind==='capture' && op.state==='interrupted')) messages.push('The capture queue is paused until the interrupted capture is resumed.');
  if (ready) messages.push(`${ready} captured site${ready === 1 ? '' : 's'} awaiting your indexing decision. Browse each site in Review & indexing.`);
  if (!messages.length) messages.push('Review a suggestion’s source and scope, then Approve for capture. It moves to Awaiting capture and downloads automatically after the grace period.');
  const text = messages.join(' ');
  if ($('next-step').textContent !== text) $('next-step').textContent = text;
  const visible = data.operations.filter(op => !reviewing && ['capture','publish'].includes(op.kind) &&
    ['queued','running','interrupted'].includes(op.state));
  $('capture-activity').hidden = !visible.length;
  $('activity').replaceChildren(...visible.map(operation));
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
    if (op.state === 'completed') card.append(button('Review captured sites',()=>{$('filter').value='captured';offset=0;}));
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
  const details = element('details',undefined,'source-review'); details.append(element('summary', `Read source evidence · ${captures.length} captures`));
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
  if (capturedStates.includes(row.state)) {
    const capture = row.coverage?.capture || {}, reviewId = capture.review_id || capture.batch_id;
    card.classList.add('site-summary');
    const title = element('h3'),siteLink=link(row.scope,row.scope.replace(/^https?:\/\//,''));siteLink.setAttribute('aria-label',row.scope);title.append(siteLink);card.append(title);
    const states = {captured_awaiting_review:'Awaiting indexing decision',approved_waiting_publication:'Approved for indexing',indexing_declined:'Indexing declined',published:'Published — indexing queued',indexed:'Indexed'};
    const reviewStates={awaiting_review:'Awaiting indexing decision',publication_requested:'Approved for indexing',published_waiting_index:'Waiting to index',indexing:'Indexing in progress',index_failed:'Indexing failed',indexed:'Indexed',indexing_declined:'Indexing declined'};
    const status=element('p',reviewStates[row.review_state] || states[row.state],'status-badge');status.dataset.tone=stateTone(row.review_state);card.append(status);
    if (capture.pages!==undefined) card.append(element('p',`${capture.pages} pages · ${capture.files} captures`,'meta'));
    card.append(button('Review site',()=>{selectedSite={id:reviewId,candidate:row.id};$('filter').value='captured';offset=0;},!reviewId));
    return card;
  }
  card.classList.add('candidate-card');
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
  if (row.state==='capturing') {
    card.append(jump('View capture progress','#capture-activity'));
    return card;
  }
  const editable = ['approval_pending','deferred','rejected'].includes(row.state);
  const controls = element('div', undefined, 'actions');
  const scope = element('select'); scope.setAttribute('aria-label', 'Capture scope'); scope.disabled = !editable;
  [['directory','Linked directory and below'],['page','Linked page only'],['site','Whole site / shared account'],['custom','Custom folder and below']].forEach(([value,label]) => {
    const option = element('option',label); option.value = value; scope.append(option);
  }); scope.value = row.scope_mode;
  controls.append(element('p','Capture settings','capture-settings-label'));
  const scopeLabel=element('label','Download scope');scopeLabel.append(scope);controls.append(scopeLabel);
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
    if (decision === 'approve') { approveButton=action;action.className='primary'; }
    controls.append(action);
  }
  if (row.state === 'coverage_unverified') controls.append(button('Recheck archive coverage', () => request('/api/coverage',{id:row.id,manifest_sha256:row.manifest_sha256})));
  card.append(controls); return card;
}
function siteStatus(row) {
  const status = element('div', undefined, 'site-status');
  const labels = {awaiting_review:'Awaiting indexing decision',indexing_declined:'Indexing declined — files retained in staging',publication_requested:'Approved — publication queued or in progress',published_waiting_index:'Published — waiting to index',indexing:'AI enrichment and indexing in progress',indexed:'Indexed — complete',index_failed:'Indexing failed — attention needed'};
  const publication=row.state==='publication_requested' ? row.operation : null;
  status.dataset.tone=stateTone(publication?.state || row.state);
  const publicationLabels={interrupted:'Publication paused',queued:'Approved — publication queued',running:'Publishing approved site'};
  status.append(element('p',publicationLabels[publication?.state] || labels[row.state] || row.state.replaceAll('_',' '),'capture-phase'));
  const next = {awaiting_review:'Browse the pages below, then approve or decline indexing for this entire captured site.',indexing_declined:'This site will not be published or indexed. Reconsider to return it to review.',publication_requested:'All captured pages from this site are approved. AI enrichment and indexing queue automatically after publication.',published_waiting_index:'AI enrichment and indexing will start when the worker is available and other indexing Jobs finish.',indexing:'The import Job includes Luna enrichment and indexing. Its completion is checked automatically.',indexed:'This site is published and indexed.',index_failed:'Published files are retained. Retry indexing to continue with saved AI results and the same $2 site budget. Existing entries are skipped.'};
  status.append(element('p',publication?.state==='interrupted' ? 'Your approval is saved. Retry publication to continue; no recapture or new approval is needed.' : next[row.state] || 'Awaiting the next workflow step.'));
  if (publication?.error) status.append(element('p',publication.error,'failure'));
  if (row.error) status.append(element('p',row.error));
  if (row.job?.waiting_for?.length || row.job?.name) {
    const details=element('details',undefined,'technical-details');details.append(element('summary','Indexing details'));
    if (row.job?.waiting_for?.length) details.append(element('p',`Waiting for existing Jobs: ${row.job.waiting_for.join(', ')}`));
    if (row.job?.name) details.append(element('p',`Indexing Job: ${row.job.name}`,'meta'));
    status.append(details);
  }
  if (row.publication?.commit) status.append(link(`https://github.com/dbsanfte/eq-archives/commit/${row.publication.commit}`,'View archive commit'));
  return status;
}
function captureDate(timestamp) { return `${timestamp.slice(0,4)}-${timestamp.slice(4,6)}-${timestamp.slice(6,8)} ${timestamp.slice(8,10)}:${timestamp.slice(10,12)}:${timestamp.slice(12,14)} UTC`; }
function sitePages(container, row) {
  const captures=row.manifest.captures,pages=new Map();
  captures.forEach((capture,slot)=>{
    const identity=row.page_identities?.[slot] || capture.url;
    if (!pages.has(identity)) pages.set(identity,[]);
    pages.get(identity).push(slot);
  });
  const entries=[...pages],layout=element('div',undefined,'site-browser');
  const navigation=element('section',undefined,'page-navigation'),list=element('ul',undefined,'site-pages');
  navigation.setAttribute('aria-label','Page browser');
  const heading=element('div',undefined,'panel-heading'),headingText=element('div');
  headingText.append(element('p','Browse this capture','panel-label'),element('h4','Captured pages'),element('p','Scroll to browse pages ↓','browse-hint'));
  heading.append(headingText,element('span',`${pages.size} pages`,'count'));
  navigation.append(heading,list,scrollControls(list,'Captured pages'));
  const reader=element('section',undefined,'site-source'),readerHeading=element('div',undefined,'reader-heading'),title=element('h4','Page source');
  reader.setAttribute('aria-label','Document preview');
  readerHeading.append(element('p','Document preview','panel-label'),title);
  const pageControls=element('div',undefined,'page-controls'),previous=element('button','← Previous page'),next=element('button','Next page →'),position=element('span',undefined,'page-position');
  previous.setAttribute('aria-label','Previous page');next.setAttribute('aria-label','Next page');
  pageControls.append(previous,position,next);
  const controls=element('div',undefined,'reader-controls'),versionLabel=element('label','Capture date'),version=element('select');
  version.setAttribute('aria-label','Capture version');versionLabel.append(version);
  const citation=element('div',undefined,'site-citation'),text=element('pre','Choose a captured page to read its complete extracted source.');
  controls.append(versionLabel,citation);
  reader.append(readerHeading,pageControls,controls,text,scrollControls(text,'Document text'));
  let generation=0,selected=0;
  async function read(slot) {
    const current=++generation,capture=captures[slot];
    title.textContent=capture.title || new URL(capture.url).pathname;
    citation.replaceChildren(link(`https://web.archive.org/web/${capture.timestamp}/${capture.url}`,'Open this capture in Wayback'),element('p',capture.url,'meta'));
    text.textContent='Loading verified source…';text.scrollTop=0;
    try {
      const source=await request(`/api/source?batch=${encodeURIComponent(row.id)}&slot=${row.source_slots[slot]}`);
      if (current===generation) text.textContent=source.complete_extracted_text;
    } catch (error) { if (current===generation) text.textContent=error.message; }
    requestAnimationFrame(updateScrollControls);
  }
  function choosePage(index,reveal=false) {
    if (index<0 || index>=entries.length) return;
    selected=index;
    const slots=entries[index][1];
    for (const active of list.querySelectorAll('[aria-current]')) active.removeAttribute('aria-current');
    const chosen=list.children[index].querySelector('button');chosen.setAttribute('aria-current','page');
    version.replaceChildren(...slots.map(slot=>{const option=element('option',captureDate(captures[slot].timestamp));option.value=slot;return option;}));
    position.textContent=`Page ${index+1} of ${entries.length}`;
    previous.disabled=index===0;next.disabled=index===entries.length-1;
    if (reveal) revealIn(list,list.children[index]);
    read(slots[0]);
  }
  previous.addEventListener('click',()=>choosePage(selected-1,true));next.addEventListener('click',()=>choosePage(selected+1,true));
  version.addEventListener('change',()=>read(Number(version.value)));
  entries.forEach(([identity,slots],index)=>{
    const item=element('li',undefined,'site-page'),capture=captures[slots[0]],choose=element('button',capture.title || new URL(identity).pathname);
    choose.addEventListener('click',()=>choosePage(index));
    item.append(choose,element('p',new URL(identity).pathname+new URL(identity).search,'meta'));
    for (const slot of slots) item.append(link(`https://web.archive.org/web/${captures[slot].timestamp}/${captures[slot].url}`,`${captureDate(captures[slot].timestamp)} · Wayback`));
    list.append(item);
  });
  layout.append(navigation,reader);container.append(layout);
  if (captures.length) choosePage(0);
  else { text.textContent='No pages were captured within this scope. This site cannot be indexed.';previous.disabled=true;next.disabled=true;position.textContent='No pages'; }
}
function siteCard(row) {
  const card = element('article',undefined,'card'); card.dataset.siteSignature = `${row.id}:${row.manifest.sites[0].id}:${row.state}:${row.manifest_sha256}`;
  card.dataset.siteIdentity=`${row.id}:${row.manifest.sites[0].id}:${row.manifest_sha256}`;
  const site = row.manifest.sites[0], captures = row.manifest.captures, title = element('h3'),heading=element('div',undefined,'site-heading');
  title.append(link(site.scope,site.scope.replace(/^https?:\/\//,'')));heading.append(element('p','Selected site','panel-label'),title);
  const pages = new Set(row.page_identities || captures.map(capture=>capture.url)).size;
  heading.append(element('p',`${pages} pages · ${captures.length} dated captures · ${(captures.reduce((total,c)=>total+c.bytes,0)/1048576).toFixed(2)} MiB`,'site-metrics'),siteStatus(row));
  const details=element('details',undefined,'capture-details');details.append(element('summary',`Capture details${row.manifest.notes?.length ? ` · ${row.manifest.notes.length} coverage notes` : ''}`),element('p',`Capture scope: ${site.scope}${site.scope_mode==='page' ? ' (linked page only)' : ' and descendants'}`,'scope'));
  for (const note of row.manifest.notes || []) details.append(element('p',`${note.url}: ${note.note}`,'meta'));
  heading.append(details);card.append(heading);
  sitePages(card,row); card.append(siteDecisions(row)); return card;
}
function siteDecisions(row) {
  const container=element('div',undefined,'site-decisions'),controls=element('div',undefined,'actions');
  container.append(element('h4','Site decision'));
  const decision=value=>request('/api/site-decision',{id:row.id,manifest_sha256:row.manifest_sha256,decision:value});
  if (row.state==='awaiting_review') {
    container.append(element('p',`Approval publishes every captured page above and queues AI-enriched indexing. Maximum enrichment spend: $${row.manifest.indexing?.max_enrichment_usd ?? 2} for this site.`));
    const approve=button('Approve site & queue indexing',()=>decision('approve'),!row.manifest.captures.length);approve.className='primary';
    controls.append(approve,button('Decline indexing',()=>decision('decline')));
  } else if (row.state==='indexing_declined') controls.append(button('Reconsider indexing',()=>decision('reconsider')));
  else if (row.state==='publication_requested' && row.operation?.state==='interrupted') {
    const retry=button('Retry publication',()=>request('/api/resume',{id:row.operation.id}),Boolean(activeOperation()));retry.className='primary';
    controls.append(retry);
  }
  else if (row.state==='index_failed' && row.job?.name) {
    const retry=button('Retry indexing',()=>request('/api/index-retry',{id:row.id,manifest_sha256:row.manifest_sha256,job_name:row.job.name}));retry.className='primary';
    controls.append(retry);
  }
  container.append(controls);container.hidden=!controls.childElementCount;return container;
}
async function loadSite(viewChanged=false) {
  $('site-review').hidden = $('filter').value !== 'captured';
  if ($('site-review').hidden) return;
  if (!selectedSite) {
    const paused=data.operations.find(op=>op.kind==='publish' && op.state==='interrupted');
    const row = data.candidates.find(row=>paused && [row.coverage?.capture?.review_id,row.coverage?.capture?.batch_id].includes(paused.payload.batch_id)) ||
      data.candidates.find(row=>row.state==='captured_awaiting_review') || data.candidates[0];
    const capture = row?.coverage?.capture;
    selectedSite = capture ? {id:capture.review_id || capture.batch_id,candidate:row.id} : null;
  }
  if (!selectedSite) { $('site-review').hidden=true;$('reviewed-site').replaceChildren();return; }
  const selected = selectedSite;
  const displayed = $('reviewed-site').querySelector('[data-site-signature]');
  if (!displayed?.dataset.siteSignature.startsWith(`${selected.id}:${selected.candidate}:`)) {
    $('reviewed-site').replaceChildren(element('p','Loading the selected site…'));
  }
  const row = await request(`/api/site?id=${encodeURIComponent(selected.id)}&candidate=${encodeURIComponent(selected.candidate)}`);
  if (selected !== selectedSite || $('filter').value !== 'captured') return;
  const signature = `${row.id}:${row.manifest.sites[0].id}:${row.state}:${row.manifest_sha256}`;
  const previous = $('reviewed-site').querySelector('[data-site-signature]');
  if (previous?.dataset.siteIdentity===`${row.id}:${row.manifest.sites[0].id}:${row.manifest_sha256}`) {
    previous.dataset.siteSignature=signature;
    const status=siteStatus(row),details=status.querySelector('.technical-details');
    if (details && previous.querySelector('.technical-details[open]')) details.open=true;
    previous.querySelector('.site-status').replaceWith(status);
    previous.querySelector('.site-decisions').replaceWith(siteDecisions(row));
  } else $('reviewed-site').replaceChildren(siteCard(row));
  for (const card of $('candidates').querySelectorAll('[data-candidate]')) {
    card.dataset.selected=String(card.dataset.candidate===selected.candidate);
    card.querySelector('button')?.setAttribute('aria-pressed',card.dataset.selected);
    if (card.dataset.selected==='true' && (viewChanged || displayed?.dataset.siteIdentity!==`${row.id}:${row.manifest.sites[0].id}:${row.manifest_sha256}`)) revealIn($('candidates'),card);
  }
}
function emptyView(view) {
  const messages={recommended:['No suggestions to review','Captured sites may still need your indexing decision. New discovery runs are available in Tools & help.'],
    approved:['No sites awaiting capture','Approve a suggestion to add it to this queue.'],
    capturing:['No capture running','Downloads begin automatically when an approved site is ready.'],
    captured:['No captured sites yet','Completed downloads will appear here for a site-by-site indexing decision.'],
    pending:['All capture decisions are up to date','Use All candidates in Tools & help to revisit earlier decisions.'],
    all:['No candidates yet','Start a bounded discovery run from Tools & help.']};
  const [title,message]=messages[view],empty=element('div',undefined,'empty-state');empty.append(element('h3',title),element('p',message));
  const target=view==='recommended' && data.captured ? 'captured' : ['approved','capturing','captured'].includes(view) ? 'recommended' : null;
  if (target) empty.append(button(`Go to ${viewNames[target]}`,()=>{$('filter').value=target;offset=0;selectedSite=null;}));
  return empty;
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
  const view=$('filter').value,reviewing=view==='captured' && data.candidates.length>0,listScroll=renderedView===view ? $('candidates').scrollTop : 0;
  $('workspace').classList.toggle('review-mode',reviewing);
  $('site-picker-heading').hidden=!reviewing;$('site-picker-scroll').hidden=!reviewing;
  $('site-picker-count').textContent=data.total;
  $('candidates').tabIndex=reviewing ? 0 : -1;
  if (reviewing) $('candidates').setAttribute('aria-label','Captured sites');else $('candidates').removeAttribute('aria-label');
  const previousCandidates = new Map([...$('candidates').querySelectorAll('[data-candidate]')].map(card=>[card.dataset.candidate,card]));
  $('candidates').replaceChildren(...data.candidates.map(row=>{
    const card = previousCandidates.get(row.id);
    return preserveEdits && card?.dataset.state===row.state && card.dataset.manifest===row.manifest_sha256 &&
      card.querySelector('details[open], [data-scope-draft]') ? card : candidate(row);
  }));
  if (!data.candidates.length) $('candidates').append(emptyView(view));
  $('candidates').scrollTop=listScroll;
  const viewChanged=renderedView!==view;renderedView=view;
  await loadSite(viewChanged);
  $('view-title').textContent=viewNames[$('filter').value];
  $('view-description').textContent=viewDescriptions[$('filter').value];
  $('view-total').textContent=`${data.total} site${data.total===1 ? '' : 's'}`;
  for (const node of document.querySelectorAll('[data-view]')) {
    if (node.dataset.view===$('filter').value) node.setAttribute('aria-current','page');else node.removeAttribute('aria-current');
  }
  const counts={recommended:data.recommended ?? ($('filter').value==='recommended' ? data.total : ''),approved:data.approved,capturing:data.capturing,captured:data.captured};
  for (const node of document.querySelectorAll('[data-count]')) node.textContent=counts[node.dataset.count] ?? '';
  saveView();
  $('operations').replaceChildren(...data.operations.map(operation));
  $('version').textContent=`Build ${data.version}`;
  $('page').textContent=data.total ? `${offset+1}–${Math.min(offset+50,data.total)} of ${data.total}` : '0 candidates';
  $('previous').disabled=offset===0; $('next').disabled=offset+50>=data.total;
  document.querySelector('.pagination').hidden=data.total<=50 && offset===0;
  $('discover').disabled=Boolean(activeOperation()); workflow();
  requestAnimationFrame(updateScrollControls);
}
$('filter').addEventListener('change',()=>{$('tools').open=false;perform(async()=>{offset=0;selectedSite=null;});});
$('previous').addEventListener('click',()=>perform(async()=>{offset=Math.max(0,offset-50);selectedSite=null;}));
$('next').addEventListener('click',()=>perform(async()=>{offset+=50;selectedSite=null;}));
$('refresh').addEventListener('click',()=>perform(async()=>{},true));
$('discover').addEventListener('click',()=>{$('tools').open=false;perform(()=>request('/api/discover',{max_candidates:50,max_usd:2}));});
for (const node of document.querySelectorAll('[data-view]')) node.addEventListener('click',()=>{
  $('filter').value=node.dataset.view;$('tools').open=false;
  perform(async()=>{offset=0;selectedSite=null;});
});
$('site-picker-scroll').append(scrollControls($('candidates'),'Captured sites'));
window.addEventListener('resize',updateScrollControls);
perform(async()=>{});
setInterval(()=>{if (!busy && data &&
  (data.approved || activeOperation() || $('filter').value==='captured' || data.batches.some(batch=>['published_waiting_index','indexing'].includes(batch.state)))) perform(async()=>{},true);},5000);
