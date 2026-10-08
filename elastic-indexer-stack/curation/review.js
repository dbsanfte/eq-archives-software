'use strict';
const $ = id => document.getElementById(id);
const stages = ['candidates','queued','capturing','review','indexing'];
const names = {candidates:'Candidates',queued:'Capture queue',capturing:'Capturing',review:'Review capture',indexing:'Indexing',saved:'Saved for later',history:'History'};
const descriptions = {
  candidates:'Find the next piece of EverQuest history. Review a site’s evidence and choose what to capture.',
  queued:'Approved sites download automatically. Undo is available until the worker starts.',
  capturing:'Follow downloads here. Completed captures move to Review automatically.',
  review:'Browse each captured site, then decide whether to publish and index it.',
  indexing:'Approved sites are published, enriched with AI, and indexed automatically.',
  saved:'Sites you set aside. Restore one when you’re ready to review it.',
  history:'Completed and declined sites stay here, away from your active work.'
};
const aliases = {recommended:'candidates',approved:'queued',captured:'review',pending:'candidates',all:'history'};
const labels = {approval_pending:'Needs a capture decision',coverage_unverified:'Coverage needs checking',deferred:'Saved for later',rejected:'Dismissed',already_archived:'Already archived',duplicate_candidate:'Duplicate candidate',approved_waiting_batch:'Queued for capture',capturing:'Capture in progress',captured_awaiting_review:'Ready to review',approved_waiting_publication:'Publication approved',awaiting_review:'Ready to review',publication_requested:'Publishing',published_waiting_index:'Waiting to index',indexing:'Enriching & indexing',index_failed:'Indexing needs attention',indexed:'Indexed',indexing_declined:'Indexing declined'};
let route = readRoute(), data = null, detail = null, busy = false, generation = 0, controller = null;
let listSignature = '', workspaceSignature = '', dockSignature = '', liveSignature = '', sourceGeneration = 0;
let searchTimer;
let manualResult=null,manualSignature='';
const drafts = new Map(), positions = new Map(), pageQueries = new Map(), sourceCache = new Map();
history.scrollRestoration = 'manual';
function node(tag, text, className) {
  const item=document.createElement(tag);
  if (text!==undefined) item.textContent=text;
  if (className) item.className=className;
  return item;
}
function control(text, action, className='') {
  const item=node('button',text,className);item.type='button';item.addEventListener('click',action);return item;
}
function external(url, text=url) {
  const item=node('a',text);
  if (/^https?:\/\//.test(url)) { item.href=url;item.target='_blank';item.rel='noopener noreferrer'; }
  return item;
}
function badge(text,tone='') { const item=node('span',text,'badge');item.dataset.tone=tone;return item; }
function tone(row) { return row.review_state==='index_failed' || row.state==='coverage_unverified' ? 'attention' : (row.review_state || row.state)==='indexed' ? 'complete' : ['queued','capturing','indexing'].includes(row.stage) ? 'active' : ''; }
function siteName(row) { const url=new URL(row.scope);return url.host+(url.pathname==='/' ? '' : url.pathname); }
function statusLabel(row) { return labels[row.review_state] || labels[row.state] || row.state.replaceAll('_',' '); }
function captureDate(stamp) { return `${stamp.slice(0,4)}-${stamp.slice(4,6)}-${stamp.slice(6,8)} ${stamp.slice(8,10)}:${stamp.slice(10,12)} UTC`; }
function readRoute() {
  const query=new URLSearchParams(location.search),view=aliases[query.get('view')] || query.get('view');
  return {view:Object.hasOwn(names,view) ? view : 'candidates',candidate:query.get('candidate') || null,
    panel:['pages','reader'].includes(query.get('screen')) ? query.get('screen') : 'site',
    page:Math.max(0,Number(query.get('page')) || 0),slot:Math.max(0,Number(query.get('slot')) || 0),
    offset:Math.max(0,Number(query.get('offset')) || 0),query:(query.get('search') || '').slice(0,200)};
}
function routeKey(value=route) { return JSON.stringify(value); }
function writeRoute(replace=false, parent=null) {
  const url=new URL(location.href);url.search='';url.searchParams.set('view',route.view);
  if (route.candidate) { url.searchParams.set('candidate',route.candidate);
    if (route.panel!=='site') { url.searchParams.set('screen',route.panel);url.searchParams.set('page',route.page);url.searchParams.set('slot',route.slot); }
  } else { if (route.offset) url.searchParams.set('offset',route.offset);if (route.query) url.searchParams.set('search',route.query); }
  history[replace ? 'replaceState' : 'pushState'](replace ? history.state : {parent},'',url);
}
function remember() {
  positions.set(routeKey(),{window:scrollY,pages:document.querySelector('.site-pages')?.scrollTop || 0,text:document.querySelector('.document-text')?.scrollTop || 0});
  if (positions.size>80) positions.delete(positions.keys().next().value);
}
function restorePosition() {
  const saved=positions.get(routeKey());
  requestAnimationFrame(()=>{window.scrollTo(0,saved?.window || 0);
    const list=document.querySelector('.site-pages'),text=document.querySelector('.document-text');
    if (list) list.scrollTop=saved?.pages || 0;if (text) text.scrollTop=saved?.text || 0;
  });
}
function go(change,{replace=false}={}) {
  remember();const parent={...route};route={...route,...change};writeRoute(replace,parent);$('tools').close();$('error').hidden=true;$('notice').hidden=true;
  if (!route.candidate && parent.view!==route.view) { $('candidates').replaceChildren(node('p','Loading sites…','description'));listSignature=''; }
  const cached=detail?.candidate.id===route.candidate;
  renderShell();if (cached) {renderWorkspace();renderDock();restorePosition();} refresh(!cached);
}
function openStage(view) { go({view,candidate:null,panel:'site',page:0,slot:0,offset:0,query:''}); }
function openSite(id) { go({candidate:id,panel:'site',page:0,slot:0}); }
async function request(path,value,signal) {
  const response=await fetch(path,value===undefined ? {signal} : {method:'POST',headers:{'Content-Type':'application/json','X-Curation-Request':'1'},body:JSON.stringify(value),signal});
  let result;try { result=await response.json(); } catch (_) { throw new Error('The service is unavailable. Refresh to retry.'); }
  if (!response.ok) throw new Error(result.error || 'The action could not be completed.');return result;
}
function message(text,undo,tone='') {
  $('notice').replaceChildren(node('span',text));$('notice').hidden=false;
  $('notice').dataset.tone=tone;
  if (undo) $('notice').append(control('Undo approval',()=>act('Undoing approval',()=>request('/api/undo',undo),'Returned to Candidates.')));
}
async function act(label,action,confirmation,{returnToCandidates=false}={}) {
  if (busy) return;
  const actedId=route.candidate;busy=true;++generation;controller?.abort();$('error').hidden=true;
  for (const item of document.querySelectorAll('[data-mutation]')) item.disabled=true;
  $('status').textContent=label;renderDock(true);renderDiscovery();
  try { const outcome=await action();let approved=null;
    // Only confirmed actions move the site. Read its durable state before following it.
    if (route.candidate && route.candidate===actedId) {
      const result=await request(`/api/candidate?id=${encodeURIComponent(route.candidate)}`);
      if (route.candidate===actedId) {
        detail=result;
        if (returnToCandidates) {
          approved=result.candidate;
          route={...route,view:'candidates',candidate:null,panel:'site',page:0,slot:0};
        } else route={...route,view:result.candidate.stage,panel:'site',page:0,slot:0};
        writeRoute(true);
      }
    }
    message((typeof confirmation==='function' ? confirmation(outcome) : confirmation) || 'Saved.',null,outcome?.coverage?.complete===false ? 'attention' : '');workspaceSignature='';dockSignature='';
    await refresh(true);
    if (approved) {
      message(approved.stage==='queued' ? `Queued ${siteName(approved)} for capture. Undo before it starts.` : `${siteName(approved)} is now in ${names[approved.stage]}.`,
        approved.stage==='queued' ? {id:approved.id,manifest_sha256:approved.manifest_sha256} : null);
    } else if (detail?.candidate.id===actedId && detail.candidate.stage==='queued') {
      message('Approved. This site is now in the capture queue. Undo is available below until it starts.');
    }
  } catch (error) { $('error').textContent=error.message;$('error').hidden=false; }
  finally { busy=false;for (const button of document.querySelectorAll('[data-mutation]')) button.disabled=button.dataset.blocked==='true';renderDock(true);renderDiscovery(); }
}
function mutation(text,action,confirmation,primary=false,disabled=false,options={}) {
  const button=control(text,()=>act(text,action,confirmation,options),primary ? 'primary' : '');
  button.dataset.mutation='';button.dataset.blocked=String(disabled);button.disabled=busy || disabled;return button;
}
async function refresh(navigated=false) {
  const token=++generation,requested={...route};controller?.abort();controller=new AbortController();
  const signal=controller.signal;
  try {
    const [listing,context]=await Promise.all([
      request(`/api/queue?filter=${requested.view}&offset=${requested.offset}&search=${encodeURIComponent(requested.query)}`,undefined,signal),
      requested.candidate ? request(`/api/candidate?id=${encodeURIComponent(requested.candidate)}`,undefined,signal) : Promise.resolve(null)
    ]);
    if (token!==generation || routeKey(requested)!==routeKey()) return;
    if (!requested.candidate && route.offset>0 && route.offset>=listing.total) {
      route.offset=Math.max(0,Math.floor((listing.total-1)/50)*50);writeRoute(true);return refresh(navigated);
    }
    if (context && context.candidate.stage!==route.view) {
      // During Back/deep-link navigation, the visible DOM still belongs to the
      // previous route. Preserve the destination's saved reading/list position.
      if (!navigated) remember();const position=positions.get(routeKey());route.view=context.candidate.stage;
      if (route.panel!=='site') positions.set(routeKey(),position);
      writeRoute(true);detail=context;
      if (route.panel==='site') message(route.view==='history' && context.candidate.review_state==='indexed' ? 'Indexing complete. This site has retired to History.' : `This site moved to ${names[route.view]}.`);
      return refresh(true);
    }
    data=listing;detail=context;renderShell();renderList();renderWorkspace();renderDock();renderTools();
    $('version').textContent=`Build ${data.version}`;
    $('status').textContent=`${names[route.view]}: ${data.total} sites. ${detail ? statusLabel(detail.candidate) : ''}`;
    if (navigated) restorePosition();
  } catch (error) {
    if (error.name==='AbortError' || token!==generation) return;
    $('error').textContent=error.message;$('error').hidden=false;
  }
}
function renderShell() {
  renderDiscovery();
  document.body.dataset.panel=route.panel;
  $('stage-list').hidden=Boolean(route.candidate);$('site-workspace').hidden=!route.candidate;
  if (!route.candidate) { $('action-dock').hidden=true;measureDock(); }
  for (const item of $('stages').querySelectorAll('button')) {
    if (item.dataset.view===route.view) item.setAttribute('aria-current','step');else item.removeAttribute('aria-current');
  }
  for (const item of document.querySelectorAll('[data-count]')) item.textContent=data?.stage_counts?.[item.dataset.count] ?? '0';
  $('view-title').textContent=names[route.view];$('view-step').textContent=stages.includes(route.view) ? `Step ${stages.indexOf(route.view)+1} of 5` : 'Your records';
  $('view-description').textContent=descriptions[route.view];
  if ($('site-search').value!==route.query) $('site-search').value=route.query;
  if (route.candidate && detail?.candidate.id!==route.candidate) {
    $('site-workspace').replaceChildren(node('p','Loading site…','description'));workspaceSignature='';
    $('action-dock').hidden=true;measureDock();
  }
}
function renderList() {
  $('view-total').textContent=`${data.total} ${data.total===1 ? 'site' : 'sites'}`;
  if (route.candidate) return;
  const signature=JSON.stringify([route.view,route.offset,route.query,data.candidates]);
  if (signature!==listSignature) {
    const items=data.candidates.map(row=>{
      const item=control('',()=>openSite(row.id),'site-tile');item.dataset.candidate=row.id;
      item.setAttribute('aria-label',`Open ${siteName(row)}`);
      const top=node('div',undefined,'tile-top');top.append(badge(statusLabel(row),tone(row)));
      if (row.stage==='candidates' && row.rating) top.append(node('span',`Grade ${row.rating.grade}/3`,'meta'));
      item.append(top,node('h2',siteName(row)),node('span',row.url,'address'));
      const capture=row.coverage?.capture;
      const description=row.stage==='candidates' ? row.rating?.reason || row.error || 'Source review is needed.' :
        row.stage==='queued' ? 'Approved scope saved. Undo before capture starts.' : row.stage==='capturing' ? 'Download progress is available inside.' :
        row.stage==='indexing' ? (row.review_state==='index_failed' ? 'Sources are retained. Open this site to retry.' : 'Publication and AI-enriched indexing are automatic.') :
        row.stage==='saved' ? 'Set aside for a later decision.' : capture ? `${capture.pages} pages · ${capture.files} dated captures` : 'Saved decision and source evidence.';
      item.append(node('p',description));
      const bottom=node('div',undefined,'tile-bottom');bottom.append(node('span',row.rating?.category?.replaceAll('_',' ') || 'Website'),node('span','Open site →','open-label'));item.append(bottom);return item;
    });
    if (!items.length) {
      const empty=node('div',undefined,'empty');empty.append(node('h2',route.query ? 'No matching sites' : `Nothing in ${names[route.view].toLowerCase()}`),node('p',route.query ? 'Try another name or address.' : ({candidates:'Use Discover & grade above to find candidates, or review captures already waiting for a decision.',queued:'Approve a candidate and it will wait here until capture starts.',capturing:'Downloads appear here as soon as the worker starts.',review:'Completed downloads arrive here for your indexing decision.',indexing:'Approved captures appear here until indexing is complete.',saved:'Sites you save for later will appear here.',history:'Completed, declined, and dismissed sites will appear here.'})[route.view]));
      if (route.query) empty.append(control('Clear search',()=>go({query:'',offset:0},{replace:true})));
      else if (route.view==='candidates' && data.stage_counts.review) empty.append(control('Review captured sites',()=>openStage('review')));
      items.push(empty);
    }
    $('candidates').replaceChildren(...items);listSignature=signature;
  }
  $('pagination').hidden=data.total<=50 && route.offset===0;
  $('page').textContent=data.total ? `${route.offset+1}–${Math.min(route.offset+50,data.total)} of ${data.total}` : '0 sites';
  $('previous').disabled=route.offset===0;$('next').disabled=route.offset+50>=data.total;
  const paused=data.operations.find(op=>op.kind==='capture' && op.state==='interrupted');
  $('stage-activity').hidden=!(['queued','capturing'].includes(route.view) && (paused || data.capture_queue_error));
  if (!$('stage-activity').hidden) {
    const box=panel('Queue waiting','A paused capture needs attention. Other approved sites stay queued.','attention');
    if (data.capture_queue_error) box.append(node('p',data.capture_queue_error));
    if (paused) box.append(control('Open paused captures',()=>openStage('capturing')));$('stage-activity').replaceChildren(box);
  }
}
function panel(title,text,className='') {
  const box=node('section',undefined,`panel ${className}`);box.append(node('h2',title));if (text) box.append(node('p',text));return box;
}
function scopeDescription(row) { return row.scope_mode==='page' ? 'Exact linked page only' : row.scope_mode==='site' ? 'Whole site / shared account' : row.scope_mode==='custom' ? 'Custom folder and descendants' : 'Linked directory and descendants'; }
function getDraft(row) {
  let draft=drafts.get(row.id);
  if (!draft || draft.hash!==row.manifest_sha256) { draft={hash:row.manifest_sha256,mode:row.scope_mode,path:new URL(row.scope).pathname,dirty:false};drafts.set(row.id,draft); }
  return draft;
}
function renderWorkspace() {
  if (!detail || !route.candidate) { workspaceSignature='';return; }
  const row=detail.candidate,review=detail.review;
  if (route.panel!=='site') { const {groups}=pageGroups(row,review);if (groups.length) { route.page=Math.min(route.page,groups.length-1);if (!groups[route.page].slots.includes(route.slot)) route.slot=groups[route.page].slots[0];writeRoute(true); } }
  const signature=JSON.stringify([row.id,row.stage,row.state,row.manifest_sha256,review?.manifest_sha256,route.panel,route.page,route.slot,route.panel==='site' ? row.coverage?.site_check : null]);
  if (signature!==workspaceSignature) {
    const root=$('site-workspace');root.replaceChildren();
    const navigation=node('div',undefined,'workspace-nav');
    navigation.append(control(route.panel==='reader' ? '← Back to pages' : route.panel==='pages' ? '← Back to site review' : `← ${names[route.view]}`,()=>backFromSite(),'quiet back'));
    if (route.panel==='site' && stages.includes(row.stage)) {
      const next=control('Next site →',()=>{
        const rows=data.candidates,index=rows.findIndex(item=>item.id===route.candidate),target=rows[(index+1)%rows.length];
        if (target && target.id!==route.candidate) openSite(target.id);
      },'quiet back');next.id='next-site';navigation.append(next);
    }
    root.append(navigation);
    const header=node('header',undefined,'site-header');
    header.append(node('p',stages.includes(row.stage) ? `Step ${stages.indexOf(row.stage)+1} of 5 · ${names[row.stage]}` : names[row.stage],'eyebrow'),node('h1',siteName(row)),node('p',`${scopeDescription(row)} · ${row.scope}`,'scope'));
    root.append(header);
    if (route.panel!=='site') renderPages(root,row,review);
    else {
      const live=node('div');live.id='stage-live';root.append(live);
      if (row.stage==='candidates') renderCandidate(root,row);
      else if (row.stage==='review') renderCaptureReview(root,row,review);
      else if (row.stage==='saved') {
        root.append(panel('Saved for later','Your source evidence and capture scope are retained. Restore this site to Candidates when you’re ready.'));
        appendEvidenceLink(root,row);
      } else if (row.stage==='history') renderHistory(root,row,review);
      else {
        const scope=panel('Approved capture scope',row.scope);scope.append(node('p',scopeDescription(row),'meta'));root.append(scope);
        if (review) root.append(control('Browse captured pages',()=>go({panel:'pages',page:0,slot:0}),'wide'));
      }
    }
    workspaceSignature=signature;liveSignature='';dockSignature='';
  }
  if ($('next-site')) $('next-site').disabled=!data.candidates.some(item=>item.id!==route.candidate);
  renderLive();
}
function appendEvidenceLink(root,row) {
  if (row.captures?.length) root.append(control(`Read source evidence · ${row.captures.length} ${row.captures.length===1 ? 'capture' : 'captures'}`,()=>go({panel:'pages',page:0,slot:0}),'wide'));
}
function coverageMessage(result) {
  const check=result.coverage || {};
  if (check.status==='already_archived') return 'Coverage verified: this site/account is already archived. Moved to History.';
  if (check.status==='new_site' && check.complete) return 'Coverage verified: this site/account is not yet archived.';
  return `Coverage is still unverified. ${check.message || 'The check is incomplete; approval remains blocked.'}`;
}
function coveragePanel(row) {
  const coverage=row.coverage || {},check=coverage.site_check || coverage;
  if (!check.status) return null;
  const descriptions={new_site:'No archived copy of this site/account was found.',already_archived:'This site/account is already represented in the archive.',inventory_partial:'Coverage is not yet verified. Capture approval remains blocked.'};
  const box=panel('Archive coverage',descriptions[check.status] || check.status.replaceAll('_',' '),check.complete===false ? 'attention' : '');
  if (check.message) box.append(node('p',check.message));
  if (check.progress) box.append(node('p',`${check.progress.checked.toLocaleString()} of ${check.progress.total.toLocaleString()} archive snapshots checked on ${check.progress.host}.`,'meta'));
  if (check.archive_path) {
    box.append(node('p',check.archive_path,'meta'));
    if (/^[a-f0-9]{40}$/.test(check.archive_sha)) box.append(external(`https://github.com/dbsanfte/eq-archives/tree/${check.archive_sha}/${check.archive_path.split('/').map(encodeURIComponent).join('/')}`,'View existing archived capture'));
  }
  if (row.state==='coverage_unverified') box.append(mutation(check.retryable ? 'Continue coverage check' : 'Recheck archive coverage',()=>request('/api/coverage',{id:row.id,manifest_sha256:row.manifest_sha256}),coverageMessage));
  return box;
}
function renderCandidate(root,row) {
  const columns=node('div',undefined,'site-columns'),left=node('div'),right=node('div');
  const evidence=panel('Why capture this site?',row.rating?.reason || row.error || 'This site needs verified source evidence before approval.');
  if (row.rating) evidence.prepend(badge(`Luna grade ${row.rating.grade}/3 · ${row.rating.category.replaceAll('_',' ')}`));
  for (const excerpt of row.rating?.evidence || []) evidence.append(node('blockquote',excerpt.excerpt,'evidence'));
  if (row.rating) evidence.append(node('p',`${row.rating.confidence} confidence · AI assessment, awaiting your decision`,'meta'));
  appendEvidenceLink(evidence,row);left.append(evidence);
  const coverage=coveragePanel(row);if (coverage) left.append(coverage);
  if (row.evidence?.length) {
    const origin=panel('How it was found');const details=node('details');details.append(node('summary',`${row.evidence.length} discovery references`));
    for (const entry of row.evidence) {
      if (entry.kind==='manual_submission') {const source=node('p','Submitted by you: ','meta');source.append(external(entry.source_url));details.append(source);}
      else details.append(node('p',typeof entry==='string' ? entry : entry.source_url || entry.source || entry.path || JSON.stringify(entry),'meta'));
    }
    origin.append(details);left.append(origin);
  }
  const settings=panel('Choose capture scope');settings.append(node('p','Approve this scope once. The download starts automatically after the Undo grace period.','meta'));
  const draft=getDraft(row),fields=node('div',undefined,'fields'),scopeLabel=node('label','Download scope'),select=node('select');select.id='capture-scope';select.setAttribute('aria-label','Download scope');
  for (const [value,label] of [['directory','Linked directory and below'],['page','Linked page only'],['site','Whole site / shared account'],['custom','Custom folder and below']]) {
    const option=node('option',label);option.value=value;select.append(option);
  }
  select.value=draft.mode;scopeLabel.append(select);fields.append(scopeLabel);
  const custom=node('div'),pathLabel=node('label','Custom capture folder path'),path=node('input');path.type='text';path.placeholder='/eq/research/';path.value=draft.path;pathLabel.append(path);custom.append(pathLabel,node('p','An absolute URL path inside this site or shared account.','field-help'));custom.hidden=draft.mode!=='custom';fields.append(custom);
  const saved=node('p',row.scope,'scope-value'),dirty=node('p','Scope edited. Save it before approving capture.','scope-draft');dirty.hidden=!draft.dirty;
  const save=mutation('Save capture scope',()=>request('/api/scope',{id:row.id,manifest_sha256:row.manifest_sha256,mode:draft.mode,...(draft.mode==='custom' ? {path:draft.path} : {})}),'Capture scope saved.');save.hidden=!draft.dirty;
  function edit() { draft.mode=select.value;draft.path=path.value;draft.dirty=true;custom.hidden=draft.mode!=='custom';dirty.hidden=false;save.hidden=false;renderDock(true); }
  select.addEventListener('change',edit);path.addEventListener('input',edit);
  const editable=row.state==='approval_pending';select.disabled=!editable;path.disabled=!editable;save.dataset.blocked=String(!editable);save.disabled=!editable || busy;
  settings.append(fields,saved,dirty,save);right.append(settings);
  const secondary=node('div',undefined,'secondary-actions');
  const decidable=['approval_pending','discovered','sampled','sample_error','unavailable','identity_unresolved'].includes(row.state);
  secondary.append(mutation('Save for later',()=>request('/api/decisions',[{id:row.id,manifest_sha256:row.manifest_sha256,decision:'defer'}]),'Moved to Saved for later.',false,!decidable),mutation('Dismiss site',()=>request('/api/decisions',[{id:row.id,manifest_sha256:row.manifest_sha256,decision:'reject'}]),'Moved to History.',false,!decidable));
  right.append(secondary);columns.append(left,right);root.append(columns);
}
function captureSummary(row,review) {
  const captures=review?.manifest.captures || [],count=new Set(review?.page_identities || captures.map(c=>c.url)).size;
  const box=panel('Your captured site');const stats=node('div',undefined,'stat-grid');
  for (const [amount,label] of [[count,'pages'],[captures.length,'dated captures']]) {const stat=node('div',undefined,'stat');stat.append(node('strong',String(amount)),node('span',label));stats.append(stat);}
  box.append(stats);
  const dates=captures.map(c=>c.timestamp).sort();
  if (dates.length) box.append(node('p',`Capture dates: ${captureDate(dates[0]).slice(0,10)} to ${captureDate(dates.at(-1)).slice(0,10)}`,'meta'));
  box.append(node('p','This is a bounded capture of the chosen scope. It may contain only part of the original site.','meta'));
  if (count) box.append(control(`Browse ${count} captured ${count===1 ? 'page' : 'pages'}`,()=>go({panel:'pages',page:0,slot:0}),'wide primary'));
  return box;
}
function renderCaptureReview(root,row,review) {
  if (!review) {root.append(panel('Capture details unavailable','Refresh to reload the saved review.'));return;}
  root.append(captureSummary(row,review));
  if (review.manifest.notes?.length) {
    const notes=panel('Capture coverage',`${review.manifest.notes.length} coverage notes. Check these before approving the captured subset.`,'attention');
    const list=node('details');list.append(node('summary','Read coverage notes'));
    for (const note of review.manifest.notes) list.append(node('p',`${note.url}: ${note.note}`));notes.append(list);root.append(notes);
  }
  const decision=panel('Decide for the whole site',`Approval publishes all ${review.manifest.captures.length} captured files and queues AI-enriched indexing. Maximum enrichment spend: $${review.manifest.indexing?.max_enrichment_usd ?? 2} for this site.`);
  decision.append(node('p','Declining retains these sources in History. You can reconsider later.','meta'),mutation('Decline indexing',()=>siteDecision('decline'),'Indexing declined. Sources retained in History.'));root.append(decision);
}
function renderHistory(root,row,review) {
  const indexed=(row.review_state || row.state)==='indexed';
  const box=panel(indexed ? 'Indexed · complete' : statusLabel(row),indexed ? 'This site is published and indexed. It has retired from the active workflow; its sources and completion record remain here.' : 'This site is outside the active workflow. Its decision and source evidence are retained.');
  if (review?.publication?.commit) box.append(external(`https://github.com/dbsanfte/eq-archives/commit/${review.publication.commit}`,'View published files in the archive'));
  root.append(box);
  if (row.state==='already_archived') {const coverage=coveragePanel(row);if (coverage) root.append(coverage);}
  if (review) root.append(captureSummary(row,review));else appendEvidenceLink(root,row);
}
function renderLive() {
  const host=$('stage-live');if (!host || !detail) return;
  const {candidate:row,review,capture_operation:op}=detail;
  const signature=JSON.stringify([row.stage,detail.queue_position,detail.queue_blocker,detail.capture_queue_error,op,review?.state,review?.operation,review?.job,review?.error]);
  if (signature===liveSignature) return;
  const expanded=new Set([...host.querySelectorAll('details[open]')].map(n=>n.dataset.key));
  host.replaceChildren();
  if (row.stage==='queued') {
    const box=panel('Ready for automatic capture',`Queue position ${detail.queue_position ?? 'pending'}. New approvals have a ${data.undo_seconds ?? 60}-second Undo grace period.`);
    const until=node('p',undefined,'grace-period');until.dataset.until=row.decision?.capture_after || '';box.append(until);
    if (detail.capture_queue_error) box.append(node('p',detail.capture_queue_error));
    const blocker=detail.queue_blocker;
    if (blocker) box.append(node('p',blocker.state==='interrupted' ? 'Waiting for a paused capture to be resumed.' : `Waiting for the current ${blocker.kind==='publish' ? 'publication' : blocker.kind==='discover' ? 'discovery' : 'capture'} operation to finish.`,'meta'));
    box.append(node('p','You can undo until the worker starts. The queue has no item-count limit.','meta'));host.append(box);updateCountdown();
  } else if (row.stage==='capturing') {
    const paused=op?.state==='interrupted',progress=op?.result?.progress;
    const box=panel(paused ? 'Capture paused' : 'Capture in progress',paused ? 'Staged files are retained. Resume within the original capture budget.' : 'This screen updates automatically as URLs are checked and downloaded.',paused ? 'attention' : '');
    if (op?.error) box.append(node('p',op.error));
    if (progress) {
      const phases={preparing:'Preparing sources',checking_wayback:'Checking Wayback captures',downloading:'Downloading a capture',ready_for_review:'Preparing site reviews'};
      box.append(node('p',phases[progress.phase] || 'Working through the approved scope','progress-title'),node('p',`${progress.files} HTML files staged · ${(progress.bytes/1048576).toFixed(2)} MiB · ${progress.urls_checked} URLs checked`));
      if (progress.site_url) box.append(node('p',`Current site: ${progress.site_url}`,'meta'));
      if (progress.current_url) box.append(external(progress.current_url));
      box.append(node('p',`Worker batch progress · ${progress.sites_total} approved ${progress.sites_total===1 ? 'site' : 'sites'}. Each completed site receives its own review.`,'meta'));
    } else box.append(node('p','Waiting for the worker’s first progress update.'));
    if (!paused) {const bar=node('progress');bar.setAttribute('aria-label','Capture in progress');box.append(bar);}
    host.append(box);
  } else if (row.stage==='indexing' && review) {
    const state=review.state,publishing=state==='publication_requested',paused=publishing && review.operation?.state==='interrupted',failed=state==='index_failed';
    const title=paused ? 'Publication paused' : failed ? 'Indexing needs attention' : publishing ? 'Publishing approved files' : state==='published_waiting_index' ? 'Waiting to index' : 'AI enrichment & indexing';
    const box=panel(title,paused ? 'Your approval is saved. Retry publication to continue with the retained sources.' : failed ? 'Published sources are retained. Retry with saved AI results and the same site budget; existing entries are skipped.' : publishing ? 'The approved site is being added to the archive. Git publication can take several minutes.' : state==='published_waiting_index' ? 'Indexing starts when the worker is available and other indexing Jobs finish.' : 'Luna enrichment and indexing are running. The site will retire from the active portal when the import completes.',paused || failed ? 'attention' : '');
    if (review.error || review.operation?.error) box.append(node('p',review.error || review.operation.error));
    const step=publishing ? 0 : state==='published_waiting_index' ? 1 : 2,pipeline=node('ol',undefined,'pipeline');
    ['Publish approved site','Wait for an available worker','AI enrichment & indexing','Complete and retire'].forEach((text,index)=>{const li=node('li',text);li.dataset.progress=index<step ? 'done' : index===step ? 'current' : 'upcoming';pipeline.append(li);});box.append(pipeline);
    if (review.job) {
      const diagnostics=node('details');diagnostics.dataset.key='indexing';diagnostics.append(node('summary','Indexing details'));
      if (review.job.name) diagnostics.append(node('p',`Job: ${review.job.name}`,'meta'));
      if (review.job.waiting_for?.length) diagnostics.append(node('p',`Waiting for: ${review.job.waiting_for.join(', ')}`,'meta'));box.append(diagnostics);
    }
    if (review.publication?.commit) box.append(external(`https://github.com/dbsanfte/eq-archives/commit/${review.publication.commit}`,'View archive commit'));host.append(box);
  }
  for (const item of host.querySelectorAll('details')) if (expanded.has(item.dataset.key)) item.open=true;
  liveSignature=signature;
}
function siteDecision(decision) { const review=detail.review;return request('/api/site-decision',{id:review.id,manifest_sha256:review.manifest_sha256,decision}); }
function pageGroups(row,review) {
  const captures=review?.manifest.captures || row.captures || [],groups=new Map();
  captures.forEach((capture,index)=>{const identity=review?.page_identities?.[index] || capture.url;if (!groups.has(identity)) groups.set(identity,[]);groups.get(identity).push(index);});
  return {captures,groups:[...groups].map(([url,slots])=>({url,slots}))};
}
function backFromSite() {
  const destination=route.panel==='reader' ? {...route,panel:'pages'} : route.panel==='pages' ? {...route,panel:'site',page:0,slot:0} : {...route,candidate:null,panel:'site',page:0,slot:0};
  const parent=history.state?.parent;
  if (parent && parent.candidate===destination.candidate && parent.panel===destination.panel && parent.view===destination.view) history.back();
  else go(destination,{replace:true});
}
function renderPages(root,row,review) {
  const {captures,groups}=pageGroups(row,review);
  if (!groups.length) { root.append(panel('No pages captured','Return to the site for its current status.'));return; }
  const chosen=Math.min(route.page,groups.length-1),group=groups[chosen],slot=group.slots.includes(route.slot) ? route.slot : group.slots[0];
  if (chosen!==route.page || slot!==route.slot) {route.page=chosen;route.slot=slot;writeRoute(true);}
  const layout=node('div',undefined,'reading-layout'),browser=node('section',undefined,'page-browser');browser.setAttribute('aria-label',review ? 'Captured pages' : 'Source evidence');
  browser.append(node('p',review ? 'Captured pages' : 'Source evidence','panel-label'),node('h2',`${groups.length} ${groups.length===1 ? 'page' : 'pages'}`));
  const filter=node('input');filter.type='search';filter.placeholder='Find a page';filter.setAttribute('aria-label','Find a captured page');filter.value=pageQueries.get(row.id) || '';browser.append(filter);
  const list=node('ul',undefined,'site-pages');list.tabIndex=0;list.setAttribute('aria-label','Page list');
  const matchCount=node('p',undefined,'list-hint');
  const renderList=()=>{
    const query=filter.value.trim().toLowerCase();pageQueries.set(row.id,filter.value);
    list.replaceChildren();
    groups.forEach((entry,index)=>{
      const first=captures[entry.slots[0]],title=first.title || new URL(entry.url).pathname;
      if (query && !(title+' '+entry.url).toLowerCase().includes(query)) return;
      const item=node('li',undefined,'site-page'),choose=control('',()=>go({panel:'reader',page:index,slot:entry.slots[0]}));
      choose.setAttribute('aria-label',`Read ${title}`);choose.dataset.page=index;
      if (index===chosen) choose.setAttribute('aria-current','page');
      choose.append(node('strong',title),node('span',new URL(entry.url).pathname+new URL(entry.url).search,'path'),node('span',`${entry.slots.length} ${entry.slots.length===1 ? 'capture' : 'captures'} · ${captureDate(first.timestamp).slice(0,10)}`,'versions'));
      item.append(choose);
      for (const captureSlot of entry.slots) {const capture=captures[captureSlot];item.append(external(`https://web.archive.org/web/${capture.timestamp}/${capture.url}`,`${captureDate(capture.timestamp).slice(0,10)} · Wayback`));}
      list.append(item);
    });
    matchCount.textContent=`${list.childElementCount} of ${groups.length} pages${query ? ' match' : ' · scroll to browse'}`;
  };
  filter.addEventListener('input',renderList);renderList();browser.append(list,matchCount,scrollButtons(list,'pages'));
  const reader=node('section',undefined,'document-reader');reader.setAttribute('aria-label','Document reader');
  const header=node('div',undefined,'reader-heading');header.append(node('p',review ? 'Captured document' : 'Source evidence','panel-label'),node('h2',captures[slot].title || new URL(captures[slot].url).pathname));
  const controls=node('div',undefined,'reader-controls'),label=node('label','Capture date'),version=node('select');version.setAttribute('aria-label','Capture version');
  for (const captureSlot of group.slots) {const option=node('option',captureDate(captures[captureSlot].timestamp));option.value=captureSlot;version.append(option);}version.value=slot;label.append(version);
  version.addEventListener('change',()=>go({slot:Number(version.value)},{replace:true}));
  controls.append(label,external(`https://web.archive.org/web/${captures[slot].timestamp}/${captures[slot].url}`,'Open this capture in Wayback'),node('p',captures[slot].url,'meta'));controls.querySelector('a').className='site-citation';
  const text=node('pre','Loading verified source…','document-text');text.tabIndex=0;text.setAttribute('aria-label','Document text');
  reader.append(header,controls,text,scrollButtons(text,'document'));
  layout.append(browser,reader);root.append(layout);
  if (route.panel==='reader' || matchMedia('(min-width:1000px)').matches) readSource(row,review,slot,text);
  else text.textContent='Choose a page to read its complete source.';
}
async function readSource(row,review,slot,target) {
  const token=++sourceGeneration;
  const key=review ? `batch=${encodeURIComponent(review.id)}&slot=${review.source_slots[slot]}` : `candidate=${encodeURIComponent(row.id)}&slot=${slot}`;
  const cacheKey=`${key}:${review?.manifest_sha256 || row.manifest_sha256}`;
  try {
    let source=sourceCache.get(cacheKey);
    if (source===undefined) { const result=await request(`/api/source?${key}`);source=result.complete_extracted_text;sourceCache.set(cacheKey,source);if (sourceCache.size>12) sourceCache.delete(sourceCache.keys().next().value); }
    if (token!==sourceGeneration || !target.isConnected) return;
    target.textContent=source;
    const saved=positions.get(routeKey());if (saved) {target.scrollTop=saved.text;window.scrollTo(0,saved.window);}
  } catch (error) { if (token===sourceGeneration && target.isConnected) target.textContent=error.message; }
}
function scrollButtons(target,name) {
  const box=node('div',undefined,'scroll-controls');box.append(node('span',name==='pages' ? 'Scroll the page list' : 'Scroll the document'));
  for (const [label,direction] of [['↑',-1],['↓',1]]) {const button=control(label,()=>target.scrollBy({top:direction*target.clientHeight*.8,behavior:'instant'}));button.setAttribute('aria-label',`Scroll ${name} ${direction<0 ? 'up' : 'down'}`);box.append(button);}
  return box;
}
function renderDock(force=false) {
  if (!detail || !route.candidate || detail.candidate.id!==route.candidate) { $('action-dock').hidden=true;measureDock();return; }
  const row=detail.candidate,review=detail.review,draft=getDraft(row),op=detail.capture_operation;
  const signature=JSON.stringify([row.id,row.state,row.stage,row.manifest_sha256,review?.state,review?.operation?.state,review?.job,op?.id,op?.state,hasOperation(),route.panel,route.page,route.slot,draft.dirty,busy]);
  if (!force && signature===dockSignature) return;
  const dock=$('action-dock'),inner=node('div',undefined,'dock-inner'),buttons=node('div',undefined,'dock-buttons');let hint='';
  if (route.panel==='reader') {
    const {groups}=pageGroups(row,review);
    const previous=control('← Previous',()=>{const index=route.page-1;go({page:index,slot:groups[index].slots[0]},{replace:true});});previous.setAttribute('aria-label','Previous page');previous.disabled=route.page<=0;
    const next=control('Next →',()=>{const index=route.page+1;go({page:index,slot:groups[index].slots[0]},{replace:true});});next.setAttribute('aria-label','Next page');next.disabled=route.page>=groups.length-1;
    hint=`Page ${route.page+1} of ${groups.length}`;
    const pages=control('Pages',()=>backFromSite());pages.setAttribute('aria-label','Back to page list');buttons.append(previous,pages,next);
  } else if (route.panel==='pages') buttons.append(control('Back to site decision',()=>backFromSite(),'primary'));
  else if (row.stage==='candidates') {
    hint=draft.dirty ? 'Save your scope changes before approving.' : row.state==='coverage_unverified' ? 'Verify archive coverage before approving.' : !row.rating || !row.captures?.length ? 'Source evidence and a Luna grade are needed before capture approval.' : 'Entire chosen scope · 60 seconds to undo before capture';
    buttons.append(mutation('Approve site for capture',()=>request('/api/decisions',[{id:row.id,manifest_sha256:row.manifest_sha256,decision:'approve'}]),'Approved for capture.',true,draft.dirty || !row.rating || !row.captures?.length || row.state!=='approval_pending',{returnToCandidates:true}));
  } else if (row.stage==='queued') {
    hint='Your approval is saved. Capture starts automatically.';
    buttons.append(mutation('Undo approval',()=>request('/api/undo',{id:row.id,manifest_sha256:row.manifest_sha256}),'Returned to Candidates.',true));
  } else if (row.stage==='capturing' && op?.state==='interrupted') {
    hint='Resume the interrupted worker batch within its original budget.';
    buttons.append(mutation('Resume capture',()=>request('/api/resume',{id:op.id}),'Capture resumed.',true,hasOperation()));
  } else if (row.stage==='review' && review) {
    hint=`Whole captured site · AI enrichment included · $${review.manifest.indexing?.max_enrichment_usd ?? 2} maximum`;
    buttons.append(mutation('Approve site & index',()=>siteDecision('approve'),'Approved. Publication and indexing are queued.',true,!review.manifest.captures.length));
  } else if (row.stage==='indexing' && review?.state==='publication_requested' && review.operation?.state==='interrupted') {
    hint='Approval retained · no recapture needed';buttons.append(mutation('Retry publication',()=>request('/api/resume',{id:review.operation.id}),'Publication queued again.',true,hasOperation()));
  } else if (row.stage==='indexing' && review?.state==='index_failed' && review.job?.name) {
    hint='Reuse saved AI results and the original site budget';buttons.append(mutation('Retry indexing',()=>request('/api/index-retry',{id:review.id,manifest_sha256:review.manifest_sha256,job_name:review.job.name}),'Indexing queued again.',true));
  } else if (row.stage==='saved' || row.stage==='history' && row.state==='rejected') {
    buttons.append(mutation('Restore to Candidates',()=>request('/api/restore',{id:row.id,manifest_sha256:row.manifest_sha256}),'Restored to Candidates.',true));
  } else if (row.stage==='history' && review?.state==='indexing_declined') buttons.append(mutation('Reconsider indexing',()=>siteDecision('reconsider'),'Returned to Review capture.',true));
  else if (row.stage==='history') buttons.append(control('Back to active sites',()=>openStage('candidates'),'primary'));
  if (hint || busy) inner.append(node('p',busy ? 'Saving your decision…' : hint,'dock-hint'));inner.append(buttons);dock.replaceChildren(inner);dock.hidden=!buttons.childElementCount;dockSignature=signature;measureDock();
}
function measureDock() { document.documentElement.style.setProperty('--dock',`${$('action-dock').hidden ? 0 : $('action-dock').getBoundingClientRect().height}px`); }
function hasOperation() { return data?.operations.some(op=>['queued','running'].includes(op.state)); }
function currentDiscovery() { return data?.operations.find(op=>op.kind==='discover' && ['queued','running','interrupted'].includes(op.state)); }
function operationLabel(op) { return op.kind==='discover' && op.payload.target ? 'Site check' : {publish:'Archive publication',capture:'Capture',discover:'Discovery'}[op.kind] || 'Another task'; }
function renderDiscovery() {
  $('discovery').hidden=route.view!=='candidates' || Boolean(route.candidate);
  $('manual-site').hidden=$('discovery').hidden;
  const active=data?.operations.find(op=>['queued','running'].includes(op.state)),discovery=currentDiscovery();
  const paused=discovery?.state==='interrupted';
  $('discover').textContent=paused ? discovery.payload.target ? 'Resume site check' : 'Resume discovery' : 'Discover & grade';
  $('discover').disabled=!data || busy || Boolean(active);
  $('discovery-limits').textContent=discovery ? `Up to ${discovery.payload.max_candidates} candidates · $${discovery.payload.max_usd} original cap` : 'Up to 50 candidates · $2 cap per run';
  $('discovery').dataset.state=active || paused ? 'blocked' : 'ready';
  const status=!data ? 'Checking worker availability…' : busy ? 'Submitting your request…' : active ?
    `${operationLabel(active)} is ${active.state}. ${active.kind==='discover' ? 'Results will appear here for your capture approval.' : 'Discovery becomes available when current work finishes.'}` : paused ?
    `${operationLabel(discovery)} paused. Resume within the original limits; previous spending still counts.${discovery.error ? ' '+discovery.error : ''}` :
    'Ready. Results still need your capture approval.';
  if ($('discovery-status').textContent!==status) $('discovery-status').textContent=status;
  $('manual-submit').disabled=!data || busy || Boolean(active) || !$('manual-url').value.trim();
  renderManualResult();
}
function renderManualResult() {
  const operation=manualResult?.operation ? data?.operations.find(op=>op.id===manualResult.operation) : data?.operations.find(op=>op.kind==='discover' && op.payload.target);
  const result=manualResult?.candidate_id ? manualResult : operation?.result;
  const url=manualResult?.url || operation?.payload.target.url;
  const signature=JSON.stringify([url,result?.candidate_id,result?.existing,operation?.state,operation?.error]);
  if (signature===manualSignature) return;
  const box=$('manual-result');box.replaceChildren();box.hidden=!url;manualSignature=signature;
  if (!url) return;
  box.append(node('p',url,'address'));
  box.append(node('p',result?.candidate_id ? result.existing ? 'This site is already in the portal. Its existing evidence and decision are preserved.' : 'Site check complete. Open the site to review its evidence and archive coverage.' : operation?.state==='interrupted' ? 'Site check paused. Use Resume site check above to continue within the original budget.' : 'Site check queued or running: checking archive coverage, sampling Wayback captures and grading with Luna.'));
  if (result?.candidate_id) {const link=node('a','Open site');link.href=`/?view=candidates&candidate=${encodeURIComponent(result.candidate_id)}`;link.addEventListener('click',event=>{event.preventDefault();openSite(result.candidate_id);});box.append(link);}
}
async function submitManualSite(event) {
  event.preventDefault();
  if (!data || busy || hasOperation() || !$('manual-url').value.trim()) return;
  const url=$('manual-url').value;
  await act('Adding site',async()=>{
    try {
      const result=await request('/api/submit-site',{url,max_usd:2});manualResult=result;
      if ($('manual-url').value===url) $('manual-url').value='';
      route.query='';route.offset=0;writeRoute(true);return result;
    } catch (error) { await refresh();throw error; }
  },result=>result.candidate_id ? 'Site already found. Open its existing entry below.' : result.existing ? 'This site check already exists. Its original budget is retained.' : 'Site check queued: one site, up to $2.');
}
async function startDiscovery() {
  if (!data || busy || hasOperation()) return;
  const discovery=currentDiscovery(),paused=discovery?.state==='interrupted';
  await act(paused ? 'Resuming discovery' : 'Starting discovery',async()=>{
    try { return await request(paused ? '/api/resume' : '/api/discover',paused ? {id:discovery.id} : {max_candidates:50,max_usd:2}); }
    catch (error) { await refresh();throw error; }
  },paused ? 'Discovery resumed within its original budget.' : 'Discovery queued: at most 50 candidates and $2.');
}
function updateCountdown() {
  const item=document.querySelector('.grace-period');if (!item) return;
  const seconds=Math.ceil((Date.parse(item.dataset.until)-Date.now())/1000);
  item.textContent=seconds>0 ? `Eligible to start in ${seconds} seconds. Undo is available.` : 'Ready for the worker. You can still undo until capture starts.';
}
function renderTools() {
  const operations=data.operations.map(op=>{
    const log=node('div',undefined,'operation-log');log.append(node('p',`${operationLabel(op)} · ${op.state}`));
    if (op.error) log.append(node('p',op.error));
    if (op.kind==='discover') {log.append(node('p',`${op.payload.max_candidates} candidates · $${op.payload.max_usd} maximum`,'meta'));
      if (op.state==='interrupted') log.append(mutation('Resume discovery',()=>request('/api/resume',{id:op.id}),'Discovery resumed within its original budget.',false,hasOperation()));}
    return log;
  });
  $('operations').replaceChildren(...operations);
}
$('home').addEventListener('click',event=>{event.preventDefault();openStage('candidates');});
for (const item of document.querySelectorAll('[data-view]')) item.addEventListener('click',()=>openStage(item.dataset.view));
$('tools-open').addEventListener('click',()=>$('tools').showModal());$('tools-close').addEventListener('click',()=>$('tools').close());
$('tools').addEventListener('click',event=>{if (event.target===$('tools') && (event.clientX<$('tools').getBoundingClientRect().left || event.clientX>$('tools').getBoundingClientRect().right)) $('tools').close();});
$('refresh').addEventListener('click',()=>{if (!busy) refresh();});
$('discover').addEventListener('click',startDiscovery);
$('manual-form').addEventListener('submit',submitManualSite);
$('manual-url').addEventListener('input',renderDiscovery);
$('previous').addEventListener('click',()=>go({offset:Math.max(0,route.offset-50)}));$('next').addEventListener('click',()=>go({offset:route.offset+50}));
$('site-search').addEventListener('input',()=>{
  clearTimeout(searchTimer);route.query=$('site-search').value;route.offset=0;writeRoute(true);
  ++generation;controller?.abort();searchTimer=setTimeout(()=>refresh(),200);
});
window.addEventListener('popstate',()=>{remember();route=readRoute();renderShell();refresh(true);});
window.addEventListener('resize',()=>{measureDock();if (detail && route.panel==='pages' && matchMedia('(min-width:1000px)').matches) {workspaceSignature='';renderWorkspace();restorePosition();}});
new ResizeObserver(measureDock).observe($('action-dock'));
renderShell();writeRoute(true);refresh(true);
setInterval(()=>{if (!busy && !document.hidden) refresh();},5000);
setInterval(updateCountdown,1000);
