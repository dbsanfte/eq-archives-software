'use strict';
const $ = id => document.getElementById(id);
const stages = ['candidates','queued','capturing','indexing'];
const names = {candidates:'Candidates',queued:'Capture queue',capturing:'Capturing',indexing:'Indexing',saved:'Saved for later',history:'History'};
const descriptions = {
  candidates:'Find the next piece of EverQuest history. Review a site’s evidence and choose what to capture.',
  queued:'Approved sites download automatically, independently of publication and indexing. Undo is available until capture starts.',
  capturing:'Follow downloads here. Completed captures publish and index automatically.',
  indexing:'Approved sites are published, enriched with AI, and indexed automatically.',
  saved:'Sites you set aside. Restore one when you’re ready to review it.',
  history:'Completed and declined sites stay here, away from your active work.'
};
const aliases = {recommended:'candidates',approved:'queued',captured:'indexing',review:'indexing',pending:'candidates',all:'history'};
const labels = {approval_pending:'Needs a capture decision',coverage_unverified:'Coverage needs checking',deferred:'Saved for later',rejected:'Dismissed',already_archived:'Already archived',duplicate_candidate:'Duplicate candidate',approved_waiting_batch:'Queued for capture',capture_resume_queued:'Resume queued',capturing:'Capture in progress',captured_awaiting_review:'Preparing indexing',approved_waiting_publication:'Publication approved',awaiting_review:'Preparing indexing',index_preflight_failed:'Indexing preparation paused',publication_requested:'Publishing',published_waiting_index:'Waiting to index',indexing:'Enriching & indexing',index_failed:'Indexing needs attention',index_budget_waiting:'Waiting for daily Luna budget',indexed:'Indexed',indexing_declined:'Indexing declined'};
let route = readRoute(), data = null, detail = null, busy = false, generation = 0, controller = null;
let listSignature = '', workspaceSignature = '', dockSignature = '', liveSignature = '', sourceGeneration = 0;
let searchTimer,gradeTimer,dismissSnapshot=null;
let manualResult=null,manualSignature='';
let activeSwipe=null;
const drafts = new Map(), positions = new Map(), pageQueries = new Map(), sourceCache = new Map(), pageOffsets = new Map();
const gradingDrafts = new Map();
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
function tone(row) { return row.capture_queue_error || row.capture_state==='interrupted' || candidateIssue(row) || ['index_failed','index_preflight_failed'].includes(row.review_state) || row.state==='coverage_unverified' ? 'attention' : (row.review_state || row.state)==='indexed' ? 'complete' : ['queued','capturing','indexing'].includes(row.stage) ? 'active' : ''; }
function siteName(row) { if (row.sitepowerup) return `SitePowerUp · Board ${row.sitepowerup}`;const url=new URL(row.scope);return url.host+(url.pathname==='/' ? '' : url.pathname); }
function statusLabel(row) {
  if (row.capture_queue_error) return 'Capture needs attention';
  if (row.capture_state==='interrupted') return 'Capture paused';
  const check=row.candidate_check;
  if (row.stage==='candidates' && check && ['queued','running'].includes(check.state)) return {coverage:'Checking coverage',sampling:'Finding Wayback samples',grading:'Grading with Luna'}[check.phase] || 'Evidence check queued';
  const issue=candidateIssue(row);if (issue) return issue.title;
  if (row.stage==='candidates' && row.state==='approval_pending' && (!row.rating || !row.captures?.length)) return row.captures?.length ? 'Needs a grade' : 'Needs source evidence';
  return labels[row.review_state] || labels[row.state] || {unavailable:'No samples found',sample_error:'Sampling failed',identity_unresolved:'Exact source needed',grade_error:'Grading failed',sampled:'Needs a grade',discovered:'Needs source evidence'}[row.state] || row.state.replaceAll('_',' ');
}
function captureDate(stamp) { return `${stamp.slice(0,4)}-${stamp.slice(4,6)}-${stamp.slice(6,8)} ${stamp.slice(8,10)}:${stamp.slice(10,12)} UTC`; }
function readRoute() {
  const query=new URLSearchParams(location.search),view=aliases[query.get('view')] || query.get('view');
  let preferred='2';try {preferred=localStorage.getItem('candidate-min-grade') || '2';} catch (_) {}
  const minimum=query.get('min_grade') ?? preferred;
  return {view:Object.hasOwn(names,view) ? view : 'candidates',candidate:query.get('candidate') || null,
    panel:['pages','reader'].includes(query.get('screen')) ? query.get('screen') : 'site',
    page:Math.max(0,Number(query.get('page')) || 0),slot:Math.max(0,Number(query.get('slot')) || 0),
    filetype:['files','all'].includes(query.get('filetype')) ? query.get('filetype') : 'pages',
    offset:Math.max(0,Number(query.get('offset')) || 0),query:(query.get('search') || '').slice(0,200),
    minGrade:/^[0-3]$/.test(minimum) ? Number(minimum) : 2,needsGrade:query.get('needs_grade')==='1'};
}
function routeKey(value=route) { return JSON.stringify(value); }
function writeRoute(replace=false, parent=null) {
  const url=new URL(location.href);url.search='';url.searchParams.set('view',route.view);
  if (route.view==='candidates' || route.minGrade!==2) url.searchParams.set('min_grade',route.minGrade);
  if (route.view==='candidates' && route.needsGrade) url.searchParams.set('needs_grade','1');
  if (route.candidate) { url.searchParams.set('candidate',route.candidate);
    if (route.filetype!=='pages') url.searchParams.set('filetype',route.filetype);
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
  activeSwipe?.cancel();
  remember();const parent={...route};route={...route,...change};writeRoute(replace,parent);$('tools').close();$('error').hidden=true;$('notice').hidden=true;
  if (!route.candidate) {
    if (parent.view!==route.view || parent.query!==route.query || parent.offset!==route.offset || parent.minGrade!==route.minGrade || parent.needsGrade!==route.needsGrade) {
      $('candidates').replaceChildren(node('p','Loading sites…','description'));listSignature='';
    } else if (data) renderList(); // Apply retained scope drafts before the next response.
  }
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
function notification(id,text,repeat=true) {
  const box=$(id);
  if (!repeat && box.dataset.dismissed===text) return;
  delete box.dataset.dismissed;
  const content=node('div',undefined,'notification-content');content.append(node('span',text));
  const close=control('×',()=>{if (box.contains(close)) {box.dataset.dismissed=text;box.hidden=true;}},'notification-close quiet');
  // Mobile browsers can suppress a compatibility click immediately after a
  // swipe/scroll. Accept a stationary touch release; cancelled drags do nothing.
  let touch=null;
  close.addEventListener('pointerdown',event=>{touch=event.isPrimary && event.pointerType==='touch' ? {id:event.pointerId,x:event.clientX,y:event.clientY} : null;});
  close.addEventListener('pointermove',event=>{if (touch && Math.hypot(event.clientX-touch.x,event.clientY-touch.y)>=12) touch=null;});
  close.addEventListener('pointercancel',()=>{touch=null;});
  // Closing moves the content beneath the finger. Suppress the later synthetic
  // click so it cannot activate a newly exposed decision button.
  close.addEventListener('touchend',event=>event.preventDefault(),{passive:false});
  close.addEventListener('pointerup',event=>{
    const start=touch;touch=null;
    if (start && start.id===event.pointerId && Math.hypot(event.clientX-start.x,event.clientY-start.y)<12) close.click();
  });
  close.setAttribute('aria-label','Close notification');close.title='Close notification';
  box.replaceChildren(content,close);box.hidden=false;return content;
}
function message(text,undo,tone='') {
  const content=notification('notice',text);
  $('notice').dataset.tone=tone;
  if (undo) content.append(mutation(undo.label || 'Undo approval',()=>request(undo.path || '/api/undo',undo.payload || undo),undo.confirmation || 'Returned to Candidates.'));
}
async function act(label,action,confirmation,{returnToCandidates=false,undo=null,quiet=false}={}) {
  if (busy) return;
  const actedId=route.candidate;busy=true;++generation;controller?.abort();$('error').hidden=true;
  for (const item of document.querySelectorAll('[data-mutation]')) item.disabled=true;
  $('status').textContent=label;renderDock(true);renderDiscovery();
  try { const outcome=await action();
    // Only confirmed actions move the site. Read its durable state before following it.
    if (route.candidate && route.candidate===actedId) {
      const result=await request(`/api/candidate?id=${encodeURIComponent(route.candidate)}`);
      if (route.candidate===actedId) {
        detail=result;
        if (returnToCandidates) {
          route={...route,view:'candidates',candidate:null,panel:'site',page:0,slot:0};
        } else route={...route,view:result.candidate.stage,panel:'site',page:0,slot:0};
        writeRoute(true);
      }
    }
    const confirmed=(typeof confirmation==='function' ? confirmation(outcome) : confirmation) || 'Saved.';
    if (quiet) { $('notice').hidden=true;$('status').textContent=confirmed; }
    else message(confirmed,typeof undo==='function' ? undo(outcome) : undo,outcome?.coverage?.complete===false ? 'attention' : '');
    workspaceSignature='';dockSignature='';
    await refresh(true);
  } catch (error) { notification('error',error.message); }
  finally { busy=false;for (const button of document.querySelectorAll('[data-mutation]')) button.disabled=button.dataset.blocked==='true';renderDock(true);renderDiscovery(); }
}
function mutation(text,action,confirmation,primary=false,disabled=false,options={}) {
  const button=control(text,()=>act(text,action,confirmation,options),primary ? 'primary' : '');
  button.dataset.mutation='';button.dataset.blocked=String(disabled);button.disabled=busy || disabled;return button;
}
async function refresh(navigated=false,reportErrors=false) {
  const token=++generation,requested={...route};controller?.abort();controller=new AbortController();
  const signal=controller.signal;
  try {
    const [listing,context]=await Promise.all([
      request(`/api/queue?compact=1&filter=${requested.view}&offset=${requested.offset}&search=${encodeURIComponent(requested.query)}${requested.view==='candidates' ? `&min_grade=${requested.minGrade}&needs_grade=${requested.needsGrade ? 1 : 0}` : ''}`,undefined,signal),
      requested.candidate ? request(`/api/candidate?id=${encodeURIComponent(requested.candidate)}${detail?.candidate.id===requested.candidate && detail.review?.manifest ? `&known_manifest=${detail.review.manifest_sha256}` : ''}`,undefined,signal) : Promise.resolve(null)
    ]);
    if (token!==generation || routeKey(requested)!==routeKey()) return;
    if (context?.review?.manifest_unchanged) context.review={...detail.review,...context.review};
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
    delete $('error').dataset.dismissed;
    activityReceived=Date.now();activityDisconnected=false;
    data=listing;detail=context;renderShell();renderList();renderWorkspace();renderDock();renderTools();
    $('version').textContent=`Build ${data.version}`;
    $('status').textContent=`${names[route.view]}: ${data.total} sites. ${detail ? statusLabel(detail.candidate) : ''}`;
    if (navigated) restorePosition();
  } catch (error) {
    if (error.name==='AbortError' || token!==generation) return;
    activityDisconnected=true;updateActivityClocks();
    notification('error',error.message,navigated || reportErrors);
  }
}
function renderShell() {
  renderPipelineActivity();
  renderDiscovery();

  renderSpending();
  $('swipe-help').hidden=Boolean(route.candidate) || route.view!=='candidates';
  document.body.dataset.panel=route.panel;
  $('stage-list').hidden=Boolean(route.candidate);$('site-workspace').hidden=!route.candidate;
  if (!route.candidate) { $('action-dock').hidden=true;measureDock(); }
  for (const item of $('stages').querySelectorAll('button')) {
    if (item.dataset.view===route.view) item.setAttribute('aria-current','step');else item.removeAttribute('aria-current');
  }
  for (const item of document.querySelectorAll('[data-count]')) item.textContent=data?.stage_counts?.[item.dataset.count] ?? '0';
  $('view-title').textContent=names[route.view];$('view-step').textContent=stages.includes(route.view) ? `Step ${stages.indexOf(route.view)+1} of 4` : 'Your records';
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
  const signature=JSON.stringify([route.view,route.offset,route.query,route.minGrade,route.needsGrade,data.candidates,data.candidates.map(row=>Boolean(drafts.get(row.id)?.dirty)),hasOperation(),route.view==='capturing' ? [data.operations,data.workers?.capture] : null]);
  if (signature!==listSignature) {
    activeSwipe?.cancel();
    const items=data.candidates.map(row=>{
      const item=control('',()=>openSite(row.id),'site-tile');item.dataset.candidate=row.id;
      item.setAttribute('aria-label',`Open ${siteName(row)}`);
      const top=node('div',undefined,'tile-top');top.append(badge(statusLabel(row),tone(row)));
      if (row.stage==='candidates' && row.rating) top.append(node('span',`Grade ${row.rating.grade}/3`,'meta'));
      item.append(top,node('h2',siteName(row)),node('span',row.url,'address'));
      const capture=row.coverage?.capture;
      const issue=candidateIssue(row);
      const description=row.state==='capture_resume_queued' ? 'Waiting to resume with saved files and progress. Cancel until capture starts.' : row.capture_queue_error || row.capture_error || (row.stage==='candidates' ? row.rating?.reason || (issue ? '' : 'Source review is needed.') :
        row.stage==='queued' ? 'Approved scope saved. Undo before capture starts.' : row.stage==='capturing' ? '' :
        row.stage==='indexing' ? (['index_failed','index_preflight_failed'].includes(row.review_state) ? 'Sources are retained. Open this site to retry.' : 'Publication and AI-enriched indexing are automatic.') :
        row.stage==='saved' ? row.decision?.automatic ? `Saved by automatic mode: Grade ${row.decision.automatic.grade} is below ${row.decision.automatic.min_grade}. Restore to review and approve it yourself.` : 'Set aside for a later decision.' : capture ? `${capture.pages} pages · ${capture.files} dated captures${row.stage==='indexing' && capture.needs_regeneration ? ' · Full capture needed' : ''}` : 'Saved decision and source evidence.');
      if (description) item.append(node('p',description));
      if (row.stage==='capturing') {
        const op=[data.workers?.capture,...data.operations].find(op=>op?.kind==='capture' && op.payload.sites?.some(site=>site.id===row.id));
        item.append(captureMeter(op,true));
      }
      if (issue) item.append(node('p',issue.message,'candidate-issue-summary'));
      if (row.rating?.grading_criteria) item.append(node('p',`Graded for: ${row.rating.grading_criteria}`,'grading-focus'));
      const bottom=node('div',undefined,'tile-bottom');bottom.append(node('span',row.rating?.category?.replaceAll('_',' ') || 'Website'),node('span','Open site →','open-label'));item.append(bottom);
      if (row.stage==='queued') {
        const card=node('article',undefined,'queue-card');
        const label=undoCaptureLabel(row);
        const undo=mutation(label,()=>undoApproval(row),undoCaptureMessage(row));
        undo.setAttribute('aria-label',`${label}: ${siteName(row)}`);
        card.append(item,undo);return card;
      }
      return row.stage==='candidates' ? candidateCard(row,item) : item;
    });
    if (!items.length) {
      const empty=node('div',undefined,'empty');empty.append(node('h2',route.query ? 'No matching sites' : `Nothing in ${names[route.view].toLowerCase()}`),node('p',route.query ? 'Try another name or address.' : ({candidates:'Use Discover & grade above to find candidates, or add a site URL.',queued:'Approve a candidate and it will wait here until capture starts.',capturing:'Downloads appear here as soon as the worker starts.',indexing:'Finished captures publish and index here automatically, then move to History.',saved:'Sites you save for later will appear here.',history:'Completed, declined, and dismissed sites will appear here.'})[route.view]));
      if (route.query) empty.append(control('Clear search',()=>go({query:'',offset:0},{replace:true})));
      else if (route.view==='candidates' && !route.needsGrade) {
        empty.replaceChildren(node('h2',`No candidates at Grade ${route.minGrade}+`),node('p','Lower the minimum grade, check sites that need grading, or discover more sites.'));
        if (data.candidate_grades?.['-1']) empty.append(control('View sites needing grading',()=>go({needsGrade:true,offset:0})));
      }
      else if (route.view==='candidates' && data.stage_counts.review) empty.append(control('Review captured sites',()=>openStage('review')));
      items.push(empty);
    }
    $('candidates').replaceChildren(...items);listSignature=signature;
  }
  $('pagination').hidden=data.total<=50 && route.offset===0;
  $('page').textContent=data.total ? `${route.offset+1}–${Math.min(route.offset+50,data.total)} of ${data.total}` : '0 sites';
  $('previous').disabled=route.offset===0;$('next').disabled=route.offset+50>=data.total;
  const paused=data.capture_attention?.interrupted ?? data.operations.filter(op=>op.kind==='capture' && op.state==='interrupted').length;
  const held=data.capture_attention?.preflight || 0;
  $('stage-activity').hidden=!(['queued','capturing'].includes(route.view) && (paused || held || data.capture_queue_error));
  if (!$('stage-activity').hidden) {
    const box=panel(data.capture_queue_error ? 'Capture worker needs attention' : 'Sites need attention',data.capture_queue_error ? 'The worker could not access the capture queue. Saved approvals are retained.' : `${paused+held} interrupted or unstarted captures need your decision. Other approved sites continue automatically.`,'attention');
    if (data.capture_queue_error) box.append(node('p',data.capture_queue_error));
    if (paused) box.append(control('Open paused captures',()=>openStage('capturing')));$('stage-activity').replaceChildren(box);
  }
}
function approvalBlock(row) {
  if (getDraft(row).dirty) return 'Open site to save your scope changes.';
  if (['queued','running'].includes(row.candidate_check?.state)) return 'Wait for this site’s evidence and grading check before approving.';
  if (row.state==='coverage_unverified') return 'Verify archive coverage before approving.';
  if ((!row.rating || !row.captures?.length) && row.candidate_check?.state==='interrupted') return 'Use Retry evidence & grading below before approving.';
  if (!row.captures?.length) return 'No source samples yet. Use Find samples & grade below.';
  if (!row.rating) return 'Source samples are ready. Use Grade source evidence below.';
  if (row.scope_has_source===false) return 'The saved scope excludes the graded source. Choose a scope containing it.';
  if (row.state!=='approval_pending') return 'Review this site’s current status before approving.';
  return '';
}
function canDismiss(row) { return ['approval_pending','discovered','sampled','sample_error','unavailable','identity_unresolved','grade_error','coverage_unverified'].includes(row.state); }
function decideCandidate(row,decision) {
  if (busy || route.candidate || route.view!=='candidates') return;
  const current=data.candidates.find(item=>item.id===row.id);
  if (!current || current.manifest_sha256!==row.manifest_sha256) return;
  const blocked=decision==='approve' ? approvalBlock(current) : !canDismiss(current);
  if (blocked) return;
  remember();
  const payload={id:row.id,manifest_sha256:row.manifest_sha256};
  act(decision==='approve' ? 'Approving capture' : 'Dismissing site',
    ()=>request('/api/decisions',[{...payload,decision}]),
    decision==='approve' ? `Queued ${siteName(row)} for capture. Undo is available in Queue until capture starts.` : `Dismissed ${siteName(row)} to History.`,
    decision==='approve' ? {quiet:true} : {undo:{payload,path:'/api/restore',label:'Undo dismissal'}});
}
async function undoApproval(row) {
  try { return row.state==='capture_resume_queued' ? await request('/api/cancel-resume',{id:row.capture_operation_id}) : await request('/api/undo',{id:row.id,manifest_sha256:row.manifest_sha256}); }
  catch (error) { await refresh();throw error; }
}
function candidateCard(row,item) {
  const card=node('article',undefined,'candidate-card'),front=node('div',undefined,'candidate-front');
  const approveCue=node('span','Approve capture →','swipe-cue approve-cue'),rejectCue=node('span','← Dismiss site','swipe-cue reject-cue');
  const backdrop=node('div',undefined,'swipe-backdrop');backdrop.setAttribute('aria-hidden','true');backdrop.append(approveCue,rejectCue);
  const blocked=approvalBlock(row),dismissible=canDismiss(row);
  item.append(node('p',`${scopeDescription(row)} · ${row.scope}`,'candidate-scope'));
  if (blocked) item.append(node('p',blocked,'candidate-block'));
  const actions=node('div',undefined,'candidate-actions');
  for (const [decision,label,disabled] of [['reject','Dismiss',!dismissible],['approve','Approve capture',Boolean(blocked)]]) {
    const button=control(label,()=>decideCandidate(row,decision),decision==='approve' ? 'primary' : '');
    button.setAttribute('aria-label',`${label}: ${siteName(row)}`);button.dataset.mutation='';button.dataset.blocked=String(disabled);button.disabled=disabled || busy;actions.append(button);
  }
  front.append(item,actions);
  if (!row.rating || !row.captures?.length) front.append(recoveryControls(row));
  card.append(backdrop,front);
  let gesture=null,suppressUntil=0;
  const reset=()=>{
    if (gesture?.horizontal) suppressUntil=performance.now()+500;
    gesture=null;front.style.transform='';card.removeAttribute('data-direction');card.classList.remove('dragging','swipe-ready');
    if (activeSwipe?.card===card) activeSwipe=null;
  };
  item.addEventListener('click',event=>{if (performance.now()<suppressUntil) {event.preventDefault();event.stopImmediatePropagation();}},true);
  item.addEventListener('pointerdown',event=>{
    if (!event.isPrimary) {activeSwipe?.cancel();return;}
    if (busy || event.pointerType==='mouse' || !matchMedia('(max-width:999px)').matches) return;
    activeSwipe?.cancel();
    suppressUntil=0;
    gesture={id:event.pointerId,x:event.clientX,y:event.clientY,dx:0,horizontal:false,threshold:Math.max(80,Math.min(120,card.clientWidth*.3))};
    activeSwipe={card,cancel:reset};
  });
  item.addEventListener('pointermove',event=>{
    if (!gesture || gesture.id!==event.pointerId) return;
    const dx=event.clientX-gesture.x,dy=event.clientY-gesture.y;
    if (!gesture.horizontal) {
      if (Math.abs(dy)>12 && Math.abs(dy)>=Math.abs(dx)) {reset();return;}
      if (Math.abs(dx)<12 || Math.abs(dx)<Math.abs(dy)*1.5) return;
      gesture.horizontal=true;item.setPointerCapture(event.pointerId);card.classList.add('dragging');
    }
    event.preventDefault();gesture.dx=dx;
    const allowed=dx>0 ? !blocked : dismissible;
    card.dataset.direction=dx>0 ? 'approve' : 'reject';
    card.classList.toggle('swipe-ready',allowed && Math.abs(dx)>=gesture.threshold);
    front.style.transform=`translateX(${Math.sign(dx)*Math.min(Math.abs(dx),allowed ? card.clientWidth*.6 : 35)}px)`;
    approveCue.textContent=blocked ? 'Approval blocked' : dx>=gesture.threshold ? 'Release to approve' : 'Approve capture →';
    rejectCue.textContent=!dismissible ? 'Dismissal blocked' : -dx>=gesture.threshold ? 'Release to dismiss' : '← Dismiss site';
  });
  item.addEventListener('pointerup',event=>{
    if (!gesture || gesture.id!==event.pointerId) return;
    const tapped=!gesture.horizontal && Math.hypot(event.clientX-gesture.x,event.clientY-gesture.y)<12;
    const dx=event.clientX-gesture.x;
    const decision=gesture.horizontal && Math.abs(dx)>=gesture.threshold ? (dx>0 ? 'approve' : 'reject') : null;
    reset();
    if (tapped) {suppressUntil=performance.now()+500;openSite(row.id);}
    else if (decision) decideCandidate(row,decision);
  });
  item.addEventListener('pointercancel',reset);
  // Taking capture from a touched heading/paragraph emits a bubbling loss on
  // that child. Only losing the button's own capture cancels this gesture.
  item.addEventListener('lostpointercapture',event=>{if (event.target===item) reset();});
  return card;
}
function usd(value) { return new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:3,maximumFractionDigits:3}).format(value); }
function renderSpending() {
  const spend=data?.luna_spend,valid=spend?.complete===true;
  $('spend-today').textContent=valid ? usd(spend.today.estimated_usd) : '—';
  $('spend-month').textContent=valid ? usd(spend.month.estimated_usd) : '—';
  $('spend-status').textContent=!spend ? 'Loading spend…' : !valid ? 'Spend is temporarily incomplete. Retrying automatically.' :
    `Includes discovery, manual grading and indexing enrichment in this portal. UTC calendar days and months. Usage-based estimates, not an account bill. Unresolved reservations: today ${usd(spend.today.unresolved_usd)}, this month ${usd(spend.month.unresolved_usd)}.`;
}
function candidateIssue(row) {
  const excluded=row.coverage?.candidate_exclusion;
  if (excluded) return {title:'Excluded promotion',message:excluded,next:'This suggestion is retained in History. Genuine hosted EQ sites remain eligible.'};
  const check=row.candidate_check;
  if (row.stage!=='candidates' || ['queued','running'].includes(check?.state)) return null;
  const error=(check?.state==='interrupted' && check.error) || row.error;
  if (!error) return null;
  const next='Retry the evidence check when the problem is resolved. Saved sources are retained.';
  if (/archived responses.*no usable HTML/i.test(error))
    return {title:'Archived responses need checking',message:error,next:'The calendar includes redirects and error responses. Check the archived page below or submit its destination URL.',error,wayback:true};
  if (/redirect.*outside.*scope/i.test(error))
    return {title:'Redirect leaves the chosen scope',message:error,next:'Choose a scope containing the destination, save it, then retry the evidence check. A different site or account needs its own candidate.',error,wayback:true};
  if (/No (exact HTML captures|usable Wayback capture|readable Wayback samples)/i.test(error))
    return {title:'No archived source found',message:'Wayback returned no matching HTML page for this exact URL in 1999–2006.',
      next:'Check the original URL in Wayback below. Save this suggestion for later or dismiss it; Retry checks the same URL again.',error,wayback:true};
  if (/different original URL|exact source identity/i.test(error))
    return {title:'Archived URL needs checking',message:'Wayback listed a different original URL, so the source could not be verified.',
      next:'Check the original URL below. A redirect must be verified within the saved scope before it can provide evidence.',error,wayback:true};
  if (/source exceeds grading limit/i.test(error))
    return {title:'Source is too large to grade',message:'The complete saved text exceeds the grading limit. Retrying cannot reduce its size.',
      next:'You can read the saved evidence or save this site for later. An operator must review the source limit before grading can proceed.',error};
  if (/budget|limit reached/i.test(error))
    return {title:'Check reached its saved limit',message:error,
      next:'Retries keep the original limits and spending. Save this site for later if its budget needs operator attention.',error};
  if (/Wayback.*429|429 after/i.test(error))
    return {title:'Wayback is limiting requests',message:'Wayback temporarily refused further requests. Saved sources are retained.',
      next:'Wait before retrying. The same operation keeps its original request, time and spending limits.',error};
  const title=check?.phase==='grading' || row.state==='grade_error' ? 'Luna grading failed' : check?.phase==='coverage' ? 'Archive check failed' : 'Source check failed';
  return {title,message:error,next,error};
}
function candidateIssuePanel(row) {
  const issue=candidateIssue(row);if (!issue) return null;
  const box=panel(issue.title,issue.message,'attention candidate-issue');box.setAttribute('role','alert');
  box.append(node('p',issue.next,'issue-next'));
  if (issue.wayback) {
    box.append(node('p',`Original URL: ${row.url}`,'scope-value'));
    box.append(external(`https://web.archive.org/web/*/${row.url}`,'Check this URL in Wayback'));
  }
  if (issue.error && issue.error!==issue.message) {
    const detail=node('details');detail.append(node('summary','Technical detail'),node('p',issue.error));box.append(detail);
  }
  return box;
}
function recoveryControls(row) {
  const box=node('div',undefined,'candidate-recovery'),check=row.candidate_check;
  const active=check && ['queued','running'].includes(check.state),paused=check?.state==='interrupted';
  const label=active ? statusLabel(row) : paused ? 'Retry evidence & grading' : row.captures?.length ? 'Grade source evidence' : 'Find samples & grade';
  const focus=check?.grading_criteria ?? $('grading-criteria').value.trim();
  const button=mutation(label,()=>request('/api/check-candidate',{id:row.id,manifest_sha256:row.manifest_sha256,max_usd:2,...(!check && focus ? {grading_criteria:focus} : {})}),
    'Evidence check queued for this site. Progress appears on its card.',true,Boolean(active || hasOperation()));
  button.setAttribute('aria-label',`${label}: ${siteName(row)}`);box.append(button);
  box.append(node('p',active ? 'This site is being checked. Approval becomes available after readable samples receive a grade.' :
    `${check ? 'Original' : 'One site ·'} $${check?.max_usd ?? 2} cap for Luna. ${row.captures?.length ? 'Uses the saved source samples.' : 'Finds exact Wayback samples from 1999–2006 before grading.'}`,'meta'));
  if (!active && hasOperation()) box.append(node('p','Discovery or another evidence check is using the candidate worker. This action becomes available when it finishes. Capture and indexing run separately.','meta'));
  box.append(node('p',`Grading focus: ${focus || 'General EverQuest relevance'}`,'meta'));
  return box;
}
function gradingOptions(row) {
  const check=row.candidate_check,saved=row.rating?.grading_criteria ?? check?.grading_criteria ?? $('grading-criteria').value.trim();
  let draft=gradingDrafts.get(row.id);
  if (!draft || !draft.dirty && draft.saved!==saved) { draft={value:saved,saved,dirty:false,open:false};gradingDrafts.set(row.id,draft); }
  const options=node('details',undefined,'grading-options panel');options.open=draft.open;
  options.append(node('summary','Advanced · Grade this site with Luna'));
  options.addEventListener('toggle',()=>{draft.open=options.open;});
  const label=node('label','Additional grading criteria for this site'),input=node('textarea');
  input.id='site-grading-criteria';label.htmlFor=input.id;input.rows=3;input.maxLength=1000;input.value=draft.value;
  input.placeholder='For example: Cleric guides or EverQuest guild sites';
  const active=['queued','running'].includes(check?.state);
  input.disabled=active;
  const button=mutation(row.rating ? 'Regrade with these criteria' : 'Grade with these criteria',()=>request('/api/check-candidate',
    {id:row.id,manifest_sha256:row.manifest_sha256,max_usd:2,grading_criteria:draft.value}),
    'Grading queued with your saved criteria and the original site budget.');
  function changed() {
    const blocked=active || hasOperation() || !canDismiss(row) || Boolean(row.rating && draft.value.trim()===(row.rating.grading_criteria || ''));
    button.dataset.blocked=String(blocked);button.disabled=busy || blocked;
  }
  input.addEventListener('input',()=>{draft.value=input.value;draft.dirty=true;changed();});changed();
  options.append(label,input,node('p',`Blank uses general EQ relevance. A high grade requires both EQ relevance and a match to your focus. Explicit grading only · ${check ? 'original' : 'one-site'} $${check?.max_usd ?? 2} total cap, including earlier attempts and regrades.`,'meta'));
  if (check) options.append(node('p',`${active ? 'Current check' : 'Last check'}: ${check.grading_criteria || 'General EverQuest relevance'}. ${active ? 'Criteria are fixed until this check finishes.' : 'Retries keep the saved criteria and budget.'}`,'meta'));
  options.append(button);
  return options;
}
function panel(title,text,className='') {
  const box=node('section',undefined,`panel ${className}`);box.append(node('h2',title));if (text) box.append(node('p',text));return box;
}
function scopeDescription(row) { return row.scope_mode==='sitepowerup' ? 'Whole SitePowerUp board · dated indexes and messages' : row.scope_mode==='ezboard' ? 'Whole Ezboard · forums and threads across servers' : row.scope_mode==='page' ? 'Exact linked page only' : row.scope_mode==='site' ? 'Whole site / shared account' : row.scope_mode==='custom' ? 'Custom folder and descendants' : 'Linked directory and descendants'; }
function getDraft(row) {
  let draft=drafts.get(row.id);
  if (!draft || draft.hash!==row.manifest_sha256) {
    // A new source grade must not discard edits to the unchanged saved scope.
    draft=draft?.dirty && draft.savedScope===row.scope && draft.savedMode===row.scope_mode ? {...draft,hash:row.manifest_sha256} :
      {hash:row.manifest_sha256,mode:row.scope_mode,path:new URL(row.scope).pathname,dirty:false,savedScope:row.scope,savedMode:row.scope_mode};
    drafts.set(row.id,draft);
  }
  return draft;
}
function renderWorkspace() {
  if (!detail || !route.candidate) { workspaceSignature='';return; }
  const row=detail.candidate,review=detail.review;
  if (route.panel!=='site') { const {groups}=pageGroups(row,review);if (groups.length) { route.page=Math.min(route.page,groups.length-1);if (!groups[route.page].slots.includes(route.slot)) route.slot=groups[route.page].slots[0];writeRoute(true); } }
  const signature=JSON.stringify([row.id,row.stage,row.state,row.manifest_sha256,review?.manifest_sha256,route.panel,route.page,route.slot,route.filetype,route.panel==='site' ? [row.coverage?.site_check,row.coverage?.candidate_exclusion,row.error,row.candidate_check,review?.state,hasOperation()] : null]);
  if (signature!==workspaceSignature) {
    const root=$('site-workspace');root.replaceChildren();
    const navigation=node('div',undefined,'workspace-nav');
    navigation.append(control(route.panel==='reader' ? `← Back to ${review && route.filetype!=='pages' ? 'files' : 'pages'}` : route.panel==='pages' ? '← Back to site' : `← ${names[route.view]}`,()=>backFromSite(),'quiet back'));
    if (route.panel==='site' && stages.includes(row.stage)) {
      const next=control('Next site →',()=>{
        const rows=data.candidates,index=rows.findIndex(item=>item.id===route.candidate),target=rows[(index+1)%rows.length];
        if (target && target.id!==route.candidate) openSite(target.id);
      },'quiet back');next.id='next-site';navigation.append(next);
    }
    root.append(navigation);
    const header=node('header',undefined,'site-header');
    header.append(node('p',stages.includes(row.stage) ? `Step ${stages.indexOf(row.stage)+1} of 4 · ${names[row.stage]}` : names[row.stage],'eyebrow'),node('h1',siteName(row)),node('p',`${scopeDescription(row)} · ${row.scope}`,'scope'));
    root.append(header);
    if (route.panel!=='site') renderPages(root,row,review);
    else {
      const live=node('div');live.id='stage-live';root.append(live);
      if (row.stage==='candidates') renderCandidate(root,row);
      else if (row.stage==='indexing') renderCapturedSite(root,row,review);
      else if (row.stage==='saved') {
        root.append(panel('Saved for later',row.decision?.automatic ? `Automatic mode saved this Grade ${row.decision.automatic.grade} site below its Grade ${row.decision.automatic.min_grade} threshold. Evidence and scope are retained. Restore it to Candidates to make your own decision; it will stay under manual control.` : 'Your source evidence and capture scope are retained. Restore this site to Candidates when you’re ready.'));
        if (row.coverage?.ezboard_parent_required) root.append(panel('Parent board needed',row.error));
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
  const issue=candidateIssuePanel(row);if (issue) root.append(issue);
  if (!row.rating || !row.captures?.length) root.append(recoveryControls(row));
  const columns=node('div',undefined,'site-columns'),left=node('div'),right=node('div');
  const evidence=panel(row.rating ? 'Why capture this site?' : 'Source evidence',row.rating?.reason || 'This site needs verified source evidence before approval.');
  if (row.rating) evidence.prepend(badge(`Luna grade ${row.rating.grade}/3 · ${row.rating.category.replaceAll('_',' ')}`));
  for (const excerpt of row.rating?.evidence || []) evidence.append(node('blockquote',excerpt.excerpt,'evidence'));
  if (row.rating) evidence.append(node('p',`${row.rating.confidence} confidence · AI assessment, awaiting your decision`,'meta'));
  if (row.rating) evidence.append(node('p',`Graded for: ${row.rating.grading_criteria || 'General EverQuest relevance'}`,'grading-focus'));
  if (['queued','running','interrupted'].includes(row.candidate_check?.state) && row.rating) evidence.append(recoveryControls(row));
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
  const settings=panel('Choose capture scope');settings.append(node('p','Approve once to capture, publish and index this scope automatically. AI enrichment is included, up to $2 per site. Undo remains available in Queue until capture starts.','meta'));
  settings.append(node('p','Capture window: 1 January 1999–31 December 2006. Download all available dated versions within this scope. Sites with 1999–2001 captures have discovery priority.','meta'));
  settings.append(node('p','Preserves pages, images, CSS, scripts and downloads, including supporting files referenced by saved sources. Large captures continue beyond thousands of files; resource limits pause the capture for explicit resume.','meta'));
  if (!row.ezboard && !row.sitepowerup) settings.append(node('p','Whole-site and folder scopes include HTTP/HTTPS and www variants within the same account path. Linked supporting files are checked at their exact URLs.','meta'));
  const draft=getDraft(row),fields=node('div',undefined,'fields'),scopeLabel=node('label','Download scope'),select=node('select');select.id='capture-scope';select.setAttribute('aria-label','Download scope');
  const scopeOptions=row.sitepowerup ? [['sitepowerup','Whole SitePowerUp board · all messages'],['page','Exact linked page only'],...(['directory','site','custom'].includes(row.scope_mode) ? [[row.scope_mode,'Keep saved scope']] : [])] : row.ezboard ? [['ezboard','Whole Ezboard · all forums and threads'],['page','Board index page only'],...(['directory','site','custom'].includes(row.scope_mode) ? [[row.scope_mode,'Keep saved scope']] : [])] : [['directory','Linked directory and below'],['page','Linked page only'],['site','Whole site / shared account'],['custom','Custom folder and below']];
  if (row.sitepowerup) settings.append(node('p',`BoardID: ${row.sitepowerup}. Capture includes this board’s indexes, messages, pagination and referenced supporting files across 1999–2006. Other boards and posting forms are excluded. Unfinished captures remain paused until resumed.`,'meta'));
  if (row.ezboard) settings.append(node('p',`Board identity: ${row.ezboard}. Whole-board capture checks historical servers, dated forums/messages and referenced supporting files across the full 1999–2006 window. Unfinished captures remain paused until resumed.`,'meta'));
  for (const [value,label] of scopeOptions) {
    const option=node('option',label);option.value=value;select.append(option);
  }
  select.value=draft.mode;scopeLabel.append(select);fields.append(scopeLabel);
  const custom=node('div'),pathLabel=node('label','Custom capture folder path'),path=node('input');path.type='text';path.placeholder='/eq/research/';path.value=draft.path;pathLabel.append(path);custom.append(pathLabel,node('p','An absolute URL path inside this site or shared account.','field-help'));custom.hidden=draft.mode!=='custom';fields.append(custom);
  const saved=node('p',row.scope,'scope-value'),dirty=node('p','Scope edited. Save it before approving capture.','scope-draft');dirty.hidden=!draft.dirty;
  const save=mutation('Save capture scope',async()=>{
    const mode=draft.mode,path=draft.path;
    const result=await request('/api/scope',{id:row.id,manifest_sha256:row.manifest_sha256,mode,...(mode==='custom' ? {path} : {})});
    // Saving the original scope can leave its manifest hash unchanged. Clear
    // only this confirmed draft; retain any edits made while saving it.
    if (drafts.get(row.id)===draft && draft.mode===mode && draft.path===path) drafts.delete(row.id);
    return result;
  },'Capture scope saved.');save.hidden=!draft.dirty;
  function edit() { draft.mode=select.value;draft.path=path.value;draft.dirty=true;custom.hidden=draft.mode!=='custom';dirty.hidden=false;save.hidden=false;renderDock(true); }
  select.addEventListener('change',edit);path.addEventListener('input',edit);
  const editable=canDismiss(row) && row.state!=='coverage_unverified';select.disabled=!editable;path.disabled=!editable;save.dataset.blocked=String(!editable);save.disabled=!editable || busy;
  settings.append(fields,saved,dirty,save);right.append(settings);
  const secondary=node('div',undefined,'secondary-actions');
  const decidable=canDismiss(row);
  secondary.append(mutation('Save for later',()=>request('/api/decisions',[{id:row.id,manifest_sha256:row.manifest_sha256,decision:'defer'}]),'Moved to Saved for later.',false,!decidable),mutation('Dismiss site',()=>request('/api/decisions',[{id:row.id,manifest_sha256:row.manifest_sha256,decision:'reject'}]),'Moved to History.',false,!decidable));
  right.append(gradingOptions(row),secondary);columns.append(left,right);root.append(columns);
}
function captureSummary(row,review) {
  const captures=review?.manifest.captures || [],count=new Set(captures.filter(isPage).map((c)=>c.url)).size;
  const files=new Set(captures.filter(c=>!isPage(c)).map(c=>c.url)).size;
  const box=panel('Your captured site');const stats=node('div',undefined,'stat-grid');
  for (const [amount,label] of [[count,'pages'],[captures.length,'dated captures']]) {const stat=node('div',undefined,'stat');stat.append(node('strong',String(amount)),node('span',label));stats.append(stat);}
  if (files) box.append(node('p',`${files} supporting files · images, stylesheets and downloads are preserved with the pages.`));
  box.append(stats);
  const dates=captures.map(c=>c.timestamp).sort();
  if (dates.length) box.append(node('p',`Capture dates: ${captureDate(dates[0]).slice(0,10)} to ${captureDate(dates.at(-1)).slice(0,10)}`,'meta'));
  const window=review?.manifest.capture_window,coverage=review?.manifest.capture_coverage?.[row.id];
  if (window) box.append(node('p',`Requested window: ${captureDate(window.from).slice(0,10)} to ${captureDate(window.to).slice(0,10)} · all available dated versions`,'meta'));
  if (coverage) box.append(node('p',coverage.reason,coverage.state==='complete' ? 'meta' : 'candidate-block'));
  box.append(node('p',review?.manifest.capture_policy==='complete-files-v1' ? 'The site inventory was processed. Unavailable files and failed supporting-file lookups are listed as coverage gaps.' : 'This is a legacy bounded capture of the chosen scope. It may contain only part of the original site.','meta'));
  if (review?.manifest.sitepowerup) {
    const board=review.manifest.sitepowerup;
    box.append(node('p',`SitePowerUp BoardID ${board.board} · ${board.coverage.catalogs_remaining} catalog queries remaining · ${board.coverage.reason}`,'meta'));
  }
  if (review?.manifest.ezboard) {
    const coverage=review.manifest.ezboard.coverage;
    box.append(node('p',`${coverage.hosts} Ezboard servers checked or queued · ${coverage.forums} forums identified · ${coverage.catalogs_remaining} catalog queries remaining`,'meta'));
    box.append(node('p',coverage.reason,coverage.state==='complete' ? 'meta' : 'candidate-block'));
    const gaps=(coverage.counts?.excluded || 0)+(coverage.counts?.unavailable || 0);
    if (gaps) box.append(node('p',`${gaps} captures unavailable or excluded, including pages whose board ownership could not be verified. Their evidence is retained.`,'candidate-block'));
  }
  if (count) box.append(control(`Browse ${count} captured ${count===1 ? 'page' : 'pages'}`,()=>go({panel:'pages',page:0,slot:0,filetype:'pages'}),'wide primary'));
  if (files) box.append(control(`Browse ${files} supporting ${files===1 ? 'file' : 'files'}`,()=>go({panel:'pages',page:0,slot:0,filetype:'files'}),'wide'));
  return box;
}
function undoCaptureLabel(row) {
  if (row.state==='capture_resume_queued') return 'Cancel queued resume';
  const continuation=row.coverage?.capture?.continuation;
  return continuation?.mode==='retry_failed' ? 'Undo file retry' : continuation ? 'Undo regeneration' : 'Undo approval';
}
function undoCaptureMessage(row) {
  return row.state==='capture_resume_queued' ? 'Resume cancelled. The site remains paused with its saved files.' : row.coverage?.capture?.continuation ? 'Capture retry cancelled. Original capture status restored.' : 'Returned to Candidates.';
}
function canRetryFiles(review) {
  const manifest=review?.manifest,retry=manifest?.capture_retry;
  return ['published_waiting_index','indexing','index_failed','indexed','index_preflight_failed'].includes(review?.state) && !(review.state==='index_preflight_failed' && manifest?.enrichment_budget_id) && manifest?.capture_policy==='complete-files-v1' && manifest.sites.length===1 &&
    ['page','directory','site','custom'].includes(manifest.sites[0].scope_mode) && (retry?.files || retry?.lookups);
}
function needsRegeneration(review) {
  const manifest=review?.manifest,site=manifest?.sites?.[0];
  return Boolean(manifest && manifest.capture_policy!=='complete-files-v1' && manifest.sites.length===1 &&
    ['page','directory','site','custom'].includes(site.scope_mode) && (manifest.limits || manifest.capture_coverage));
}
function renderCapturedSite(root,row,review) {
  if (!review) {root.append(panel('Capture details unavailable','Refresh to reload the saved capture.'));return;}
  root.append(captureSummary(row,review));
  if (needsRegeneration(review)) {
    root.append(panel('Full capture needed','This older download used an HTML-only, 100-file batch limit. It has not checked the complete site inventory. Regenerate this approved scope to collect all available pages, images and files from 1999–2006. Verified downloads will be reused; the original files are retained.','attention'));
  }
  if (review.manifest.notes?.length) {
    const notes=panel('Capture coverage',`${review.manifest.notes.length} coverage notes. Available files proceed to indexing; missing files stay listed here.`,'attention');
    const list=node('details');list.append(node('summary','Read coverage notes'));
    for (const note of review.manifest.notes) list.append(node('p',`${note.url}${note.timestamp || note.stamp ? ` (${captureDate(note.timestamp || note.stamp)})` : ''}: ${note.note || note.reason}`));notes.append(list);
    if (canRetryFiles(review)) {
      const retry=review.manifest.capture_retry;
      notes.append(node('p',`${retry.files} file versions and ${retry.lookups} supporting-file lookups can be retried. Saved files are reused. The site returns to the capture queue, with Undo until it starts; publication and indexing continue automatically after retry. ${review.manifest.indexing?.daily_budget ? 'The daily Luna budget continues to apply across retries.' : 'The original $2 site budget is shared across retries.'}`));
      notes.append(mutation('Retry failed files',()=>request('/api/continue-capture',{id:review.id,manifest_sha256:review.manifest_sha256}),'Failed files queued for retry. Saved captures are retained.'));
    }
    root.append(notes);
  }

}
function renderHistory(root,row,review) {
  const issue=candidateIssuePanel(row);if (issue) root.append(issue);
  const indexed=(row.review_state || row.state)==='indexed';
  const box=panel(indexed ? 'Indexed · complete' : row.coverage?.candidate_exclusion ? 'History record' : statusLabel(row),indexed ? 'This site is published and indexed. It has retired from the active workflow; its sources and completion record remain here.' : 'This site is outside the active workflow. Its decision and source evidence are retained.');
  if (review?.publication?.commit) box.append(external(`https://github.com/dbsanfte/eq-archives/commit/${review.publication.commit}`,'View published files in the archive'));
  root.append(box);
  if (row.state==='already_archived') {const coverage=coveragePanel(row);if (coverage) root.append(coverage);}
  if (review) renderCapturedSite(root,row,review);else appendEvidenceLink(root,row);
}
function captureMeter(op,compact=false) {
  const progress=op?.result?.progress,completion=progress?.completion || {},paused=op?.state==='interrupted';
  const {total,remaining,completed}=completion;
  const known=Number.isInteger(total) && total>0 && Number.isInteger(remaining) && remaining>=0 && remaining<=total && completed===total-remaining;
  const board=progress?.ezboard || progress?.sitepowerup;
  const catalogs=progress?.catalogs_pending ?? board?.catalogs_remaining;
  const listing=progress?.phase==='checking_wayback' || catalogs>0 && known && remaining===0;
  const catalogTotal=progress?.catalogs_total ?? board?.catalogs_total;
  const catalogsKnown=Number.isInteger(catalogTotal) && catalogTotal>0 && Number.isInteger(catalogs) && catalogs>=0 && catalogs<=catalogTotal;
  const phases={preparing:'Preparing sources',checking_wayback:progress?.ezboard ? 'Checking Ezboard archive listings' : progress?.sitepowerup ? 'Checking board archive listings' : 'Checking Wayback archive listings',downloading:'Downloading a capture',ready_for_review:'Preparing automatic indexing'};
  const meter=node('div',undefined,'capture-meter');meter.dataset.paused=String(paused);
  meter.append(node('p',`${paused ? 'Paused · ' : ''}${phases[progress?.phase] || 'Working through the approved scope'}`,'capture-phase'));
  if (op?.payload?.sites?.length>1) meter.append(node('p',`Batch of ${op.payload.sites.length} sites · Current site: ${progress?.site_url || 'preparing'}`,'meta'));
  if (Number.isInteger(progress?.files)) meter.append(node('p',`${progress.files.toLocaleString()} files saved${board ? ` · ${Number(board.forums || 0).toLocaleString()} forums identified` : ''}`,'capture-saved'));
  if (!listing) meter.append(node('p',known ? `${remaining.toLocaleString()} captures left` : 'Counting remaining captures…','capture-remaining'));
  const bar=node('progress');bar.setAttribute('aria-label',listing ? 'Archive listing progress' : 'Listed capture progress');
  if (listing) {
    if (catalogsKnown) {
      bar.max=catalogTotal;bar.value=catalogTotal-catalogs;
      const text=`${(catalogTotal-catalogs).toLocaleString()} of ${catalogTotal.toLocaleString()} archive listings checked · ${catalogs.toLocaleString()} left`;
      meter.append(node('p',text,'capture-fraction'));bar.setAttribute('aria-valuetext',text);
    } else {
      const text=catalogs>0 ? `${catalogs.toLocaleString()} archive listings still being checked.` : 'The capture inventory is still being listed';
      meter.append(node('p',text,'capture-fraction'));bar.setAttribute('aria-valuetext',text);
    }
  } else if (known) {
    bar.max=total;bar.value=completed;
    bar.setAttribute('aria-valuetext',`${completed.toLocaleString()} of ${total.toLocaleString()} listed captures processed; ${remaining.toLocaleString()} left`);
    meter.append(node('p',`${completed.toLocaleString()} of ${total.toLocaleString()} listed captures processed · ${Math.floor(completed/total*100)}%`,'capture-fraction'));
  } else bar.setAttribute('aria-valuetext','The capture inventory is still being listed');
  meter.append(bar);
  if (!listing && catalogs>0) meter.append(node('p',`${catalogs.toLocaleString()} archive listings still being checked.`,'meta'));
  if (listing && Number.isInteger(board?.catalog_pages)) meter.append(node('p',`${board.catalog_pages.toLocaleString()} listing pages read · ${Number(board.catalog_rows || 0).toLocaleString()} records found`,'meta'));
  const query=board?.current_query;
  if (listing && query) meter.append(node('p',`${progress.ezboard ? (query.kind==='b' ? 'Board indexes' : 'Forum & message listings') : 'Board listings'} · ${query.from.slice(0,4)}–${query.to.slice(0,4)}`,'capture-query'));
  if (progress?.current_url) {
    const current=node('p',progress.current_url,'capture-url');current.title=progress.current_url;
    meter.append(node('p',paused ? 'Last activity' : listing ? 'Looking up' : 'Current file','capture-url-label'),current);
  }
  const activity=node('p',undefined,'capture-updated');activity.dataset.updated=String(Date.parse(completion.updated_at || op?.updated));
  activity.dataset.paused=String(paused);activity.dataset.waiting=String(['checking_wayback','downloading'].includes(progress?.phase));
  meter.append(activity);updateCaptureActivity(activity);
  const eta=node('p',undefined,'capture-eta');eta.dataset.paused=String(paused);
  eta.dataset.listing=String(listing);
  eta.dataset.empty=String(known && remaining===0);eta.dataset.phase=progress?.phase || '';
  const stamp=Date.parse(completion.updated_at),seconds=completion.eta_seconds;
  if (known && Number.isFinite(stamp) && Number.isFinite(seconds) && seconds>0) eta.dataset.finish=String(stamp+seconds*1000);
  meter.append(eta);updateCaptureEta(eta);
  if (!compact) {
    meter.append(node('p','Counts cover the files listed so far and can grow as more pages and supporting files are found. ETA uses recent capture speed.','meta'));
    if (Number.isFinite(progress?.bytes)) meter.append(node('p',`${(progress.bytes/1048576).toFixed(2)} MiB ${board ? 'transferred from Wayback' : 'saved'} · ${Number(progress.urls_checked || 0).toLocaleString()} records checked`,'meta'));
    if (progress?.unavailable>0) meter.append(node('p',`${progress.unavailable.toLocaleString()} unavailable captures will be listed as coverage gaps.`,'meta'));
    if (progress?.failed_lookups>0) meter.append(node('p',`${progress.failed_lookups.toLocaleString()} supporting-file lookups failed. Other files continue downloading; retry missing files from Indexing or History after publication.`,'meta'));
    if (progress?.excluded_urls>0) meter.append(node('p',`${progress.excluded_urls.toLocaleString()} advertising URLs intentionally skipped (ad.* and ads.*).`,'meta'));
  }
  return meter;
}
function updateCaptureEta(target) {
  for (const eta of target ? [target] : document.querySelectorAll('.capture-eta')) {
    if (eta.dataset.paused==='true') eta.textContent='ETA paused. Recalculates after resume.';
    else if (eta.dataset.listing==='true') eta.textContent='Finish time unknown during archive listing.';
    else if (eta.dataset.empty==='true') eta.textContent='Checking for any remaining files…';
    else if (!eta.dataset.finish) eta.textContent='ETA calculating after the next captures…';
    else {
      const seconds=(Number(eta.dataset.finish)-Date.now())/1000;
      if (seconds<=0) eta.textContent='ETA updating: waiting for the next capture.';
      else {
        const minutes=Math.ceil(seconds/60),hours=Math.floor(minutes/60);
        const duration=seconds<60 ? 'less than a minute' : hours ? `${hours} hr${minutes%60 ? ` ${minutes%60} min` : ''}` : `${minutes} min`;
        const finish=new Date(Number(eta.dataset.finish)).toLocaleString(undefined,{...(hours>=24 ? {weekday:'short'} : {}),hour:'2-digit',minute:'2-digit'});
        eta.textContent=`ETA for listed captures: about ${duration} remaining (around ${finish}).`;
      }
    }
  }
}
function updateCaptureActivity(target) {
  for (const activity of target ? [target] : document.querySelectorAll('.capture-updated')) {
    const stamp=Number(activity.dataset.updated),elapsed=Math.max(0,Math.floor((Date.now()-stamp)/1000));
    if (!Number.isFinite(stamp)) activity.textContent='Waiting for the worker’s first progress update.';
    else if (activity.dataset.paused==='true') activity.textContent=`Progress saved ${new Date(stamp).toLocaleString()}`;
    else if (elapsed<60) activity.textContent=elapsed<5 ? 'Updated just now' : `Updated ${elapsed} sec ago`;
    else activity.textContent=`No progress update for ${Math.floor(elapsed/60)} min${activity.dataset.waiting==='true' ? ' · waiting for Wayback; the connection may be busy or retrying' : ''}`;
  }
}
function renderLive() {
  const host=$('stage-live');if (!host || !detail) return;
  const {candidate:row,review,capture_operation:op}=detail;
  const signature=JSON.stringify([row.stage,row.capture_queue_error,detail.queue_position,detail.queue_blocker,detail.capture_queue_error,op,review?.state,review?.operation,review?.job,review?.error]);
  if (signature===liveSignature) return;
  const expanded=new Set([...host.querySelectorAll('details[open]')].map(n=>n.dataset.key));
  host.replaceChildren();
  if (row.stage==='queued') {
    const resuming=row.state==='capture_resume_queued';
    const box=panel(resuming ? 'Resume queued' : row.capture_queue_error ? 'Capture needs attention' : 'Ready for automatic capture',resuming ? `Queue position ${detail.queue_position ?? 'pending'}. Resume will continue the original capture, reusing its saved files and checkpoint.` : row.capture_queue_error ? 'Capture could not start. Other sites continue. Undo this approval to review the scope and evidence before approving again.' : `Queue position ${detail.queue_position ?? 'pending'}. New approvals have a ${data.undo_seconds ?? 60}-second Undo grace period.`,row.capture_queue_error ? 'attention' : '');
    if (row.capture_queue_error) box.append(node('p',row.capture_queue_error));
    if (resuming) {
      if (op?.result?.progress) box.append(node('p',`${Number(op.result.progress.files || 0).toLocaleString()} files already saved. Original scope and cumulative budgets retained.`));
      if (op?.error) box.append(node('p',`Previous stop: ${op.error}`,'meta'));
      if (op?.payload?.sites?.length>1) box.append(node('p',`Resumes the original batch of ${op.payload.sites.length} sites together.`,'meta'));
    } else {const until=node('p',undefined,'grace-period');until.dataset.until=row.decision?.capture_after || '';box.append(until);}
    if (detail.capture_queue_error) box.append(node('p',detail.capture_queue_error));
    const blocker=detail.queue_blocker;
    if (blocker && !row.capture_queue_error) box.append(node('p','Waiting for the current capture to finish.','meta'));
    box.append(node('p',`${resuming ? 'You can cancel this resume' : 'You can undo'} until capture starts. Candidate gathering and indexing run separately. The queue has no item-count limit.`,'meta'));host.append(box);updateCountdown();
  } else if (row.stage==='capturing') {
    const paused=op?.state==='interrupted',progress=op?.result?.progress;
    const box=panel(paused ? 'Capture paused' : 'Capture in progress',paused ? (progress?.capture_policy==='complete-files-v1' ? 'Saved files and catalog progress are retained. Resume continues pending files and extends an exhausted transport allowance without resetting usage.' : 'Staged files are retained. Resume within the original capture budget.') : 'This screen updates automatically as URLs are checked and downloaded.',paused ? 'attention' : '');
    if (op?.error) box.append(node('p',op.error));
    if (paused) box.append(node('p','This site is waiting for your decision. Other approved sites continue; this failure does not block the queue.','meta'));
    if (progress) {
      box.append(captureMeter(op));
      if (progress.site_url) box.append(node('p',`Current site: ${progress.site_url}`,'meta'));
      if (progress.current_url) box.append(external(progress.current_url,'Open current URL'));
      box.append(node('p',`Worker batch progress · ${progress.sites_total} approved ${progress.sites_total===1 ? 'site' : 'sites'}. Each completed site publishes and indexes automatically.`,'meta'));
    } else {box.append(node('p','Waiting for the worker’s first progress update.'),captureMeter(op));}
    host.append(box);
  } else if (row.stage==='indexing' && review) {
    const state=review.state,preparing=['awaiting_review','index_preflight_failed'].includes(state),preflightFailed=state==='index_preflight_failed',publishing=state==='publication_requested',paused=publishing && review.operation?.state==='interrupted',failed=state==='index_failed',budgetWaiting=state==='index_budget_waiting';
    const title=budgetWaiting ? 'Waiting for daily Luna budget' : preflightFailed ? 'Indexing preparation paused' : preparing ? 'Preparing automatic indexing' : paused ? 'Publication paused' : failed ? 'Indexing needs attention' : publishing ? 'Publishing approved files' : state==='published_waiting_index' ? 'Waiting to index' : 'AI enrichment & indexing';
    const box=panel(title,budgetWaiting ? `Paid results and indexed pages are retained. The next request needs a reservation of $${Number(review.job.required_usd).toFixed(4)}. This site resumes automatically when the daily budget allows, resetting at ${new Date(data.automation?.resets_at || review.job.retry_at).toUTCString()}. You can raise the daily limit in Candidates.` : preflightFailed ? 'Saved files are retained. Resolve the issue below and retry preparation.' : preparing ? 'Checking saved files, archive coverage and your original capture approval. Publication and indexing follow automatically.' : paused ? 'Your approval is saved. Retry publication to continue with the retained sources.' : failed ? 'Published sources are retained. Retry with saved AI results and the same site budget; existing entries are skipped.' : publishing ? 'The approved site is being added to the archive. Git publication can take several minutes.' : state==='published_waiting_index' ? 'Indexing starts when the worker is available and other indexing Jobs finish.' : 'Luna enrichment and indexing are running. The site will retire from the active portal when the import completes.',paused || failed || preflightFailed ? 'attention' : '');
    box.append(node('p','Capture and discovery continue independently of publication and indexing.','meta'));
    box.append(node('p',review.manifest.indexing?.daily_budget ? 'AI enrichment continues across days under your shared daily Luna limit, including retries.' : `AI enrichment: up to $${review.manifest.indexing?.max_enrichment_usd ?? 2} for this site, including retries.`,'meta'));
    if (review.error || review.operation?.error) box.append(node('p',review.error || review.operation.error));
    const step=preparing ? 0 : publishing ? 1 : state==='published_waiting_index' ? 2 : 3,pipeline=node('ol',undefined,'pipeline');
    ['Verify captured files','Publish approved site','Wait for an available worker','AI enrichment & indexing','Complete and retire'].forEach((text,index)=>{const li=node('li',text);li.dataset.progress=index<step ? 'done' : index===step ? 'current' : 'upcoming';pipeline.append(li);});box.append(pipeline);
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
function isPage(capture) { return capture.kind!=='file' && !capture.supporting_source; }
function pageGroups(row,review) {
  const captures=review?.manifest.captures || row.captures || [],groups=new Map();
  captures.forEach((capture,index)=>{const identity=review?.page_identities?.[index] || capture.url;if (!groups.has(identity)) groups.set(identity,[]);groups.get(identity).push(index);});
  return {captures,groups:[...groups].map(([url,slots])=>({url,slots})).filter(group=>!review || route.filetype==='all' || (route.filetype==='files' ? !isPage(captures[group.slots[0]]) : isPage(captures[group.slots[0]])))};
}
function backFromSite() {
  const destination=route.panel==='reader' ? {...route,panel:'pages'} : route.panel==='pages' ? {...route,panel:'site',page:0,slot:0} : {...route,candidate:null,panel:'site',page:0,slot:0};
  const parent=history.state?.parent;
  if (parent && parent.candidate===destination.candidate && parent.panel===destination.panel && parent.view===destination.view) history.back();
  else go(destination,{replace:true});
}
function renderPages(root,row,review) {
  const {captures,groups}=pageGroups(row,review);
  const filesMode=Boolean(review && route.filetype!=='pages');
  if (!groups.length) { root.append(panel('No pages captured','Return to the site for its current status.'));return; }
  const chosen=Math.min(route.page,groups.length-1),group=groups[chosen],slot=group.slots.includes(route.slot) ? route.slot : group.slots[0];
  if (chosen!==route.page || slot!==route.slot) {route.page=chosen;route.slot=slot;writeRoute(true);}
  const layout=node('div',undefined,'reading-layout'),browser=node('section',undefined,'page-browser');browser.setAttribute('aria-label',review ? 'Captured pages' : 'Source evidence');
  browser.append(node('p',review ? (filesMode ? 'Captured files' : 'Captured pages') : 'Source evidence','panel-label'),node('h2',`${groups.length} ${groups.length===1 ? 'page' : 'pages'}`));
  if (review && captures.some(c=>!isPage(c))) {
    const label=node('label','Browse'),kind=node('select');kind.setAttribute('aria-label','Captured content type');
    for (const [value,title] of [['pages','Readable pages'],['files','Supporting files'],['all','All captured files']]) {const option=node('option',title);option.value=value;kind.append(option);}
    kind.value=route.filetype;kind.addEventListener('change',()=>go({filetype:kind.value,page:0,slot:0},{replace:true}));label.append(kind);browser.append(label);
    browser.querySelector('h2').textContent=`${groups.length} ${route.filetype==='pages' ? 'pages' : 'files'}`;
  }
  const filter=node('input');filter.type='search';filter.placeholder=filesMode ? 'Find a file' : 'Find a page';filter.setAttribute('aria-label',filesMode ? 'Find a captured file' : 'Find a captured page');filter.value=pageQueries.get(row.id) || '';browser.append(filter);
  const list=node('ul',undefined,'site-pages');list.tabIndex=0;list.setAttribute('aria-label','Page list');
  const matchCount=node('p',undefined,'list-hint');
  const pager=node('div',undefined,'page-navigation');
  const renderList=()=>{
    const query=filter.value.trim().toLowerCase();pageQueries.set(row.id,filter.value);
    const key=`${row.id}:${route.filetype}:${query}`;
    const matches=groups.map((entry,index)=>({entry,index})).filter(({entry})=>!query || ((captures[entry.slots[0]].title || '')+' '+entry.url).toLowerCase().includes(query));
    const selected=Math.max(0,matches.findIndex(item=>item.index===chosen));
    const offset=pageOffsets.get(key) ?? Math.floor(selected/100)*100;
    list.replaceChildren();
    matches.slice(offset,offset+100).forEach(({entry,index})=>{
      const first=captures[entry.slots[0]],title=first.title || new URL(entry.url).pathname;
      const item=node('li',undefined,'site-page'),choose=control('',()=>go({panel:'reader',page:index,slot:entry.slots[0]}));
      choose.setAttribute('aria-label',`Read ${title}`);choose.dataset.page=index;
      if (index===chosen) choose.setAttribute('aria-current','page');
      choose.append(node('strong',title),node('span',new URL(entry.url).pathname+new URL(entry.url).search,'path'),node('span',`${entry.slots.length} ${entry.slots.length===1 ? 'capture' : 'captures'} · ${captureDate(first.timestamp).slice(0,10)}`,'versions'));
      item.append(choose);
      for (const captureSlot of entry.slots.slice(0,3)) {const capture=captures[captureSlot];item.append(external(`https://web.archive.org/web/${capture.timestamp}/${capture.url}`,`${captureDate(capture.timestamp).slice(0,10)} · Wayback`));}
      if (entry.slots.length>3) item.append(node('span','Open to browse every dated version.','meta'));
      list.append(item);
    });
    matchCount.textContent=`${matches.length ? offset+1 : 0}–${offset+list.childElementCount} of ${matches.length} ${route.filetype==='pages' ? 'pages' : 'files'}${query ? ' match' : ' · scroll to browse'}`;
    pager.replaceChildren();
    if (matches.length>100) {
      for (const [title,next] of [['Previous files',offset-100],['Next files',offset+100]]) {const button=control(title,()=>{pageOffsets.set(key,next);renderList();list.scrollTop=0;});button.disabled=next<0 || next>=matches.length;pager.append(button);}
    }
  };
  filter.addEventListener('input',renderList);renderList();browser.append(list,matchCount,pager,scrollButtons(list,'pages'));
  const reader=node('section',undefined,'document-reader');reader.setAttribute('aria-label','Document reader');
  const header=node('div',undefined,'reader-heading');header.append(node('p',review ? (isPage(captures[slot]) ? 'Captured document' : 'Supporting file') : 'Source evidence','panel-label'),node('h2',captures[slot].title || new URL(captures[slot].url).pathname));
  const controls=node('div',undefined,'reader-controls'),label=node('label','Capture date'),version=node('select');version.setAttribute('aria-label','Capture version');
  for (const captureSlot of group.slots) {const option=node('option',captureDate(captures[captureSlot].timestamp));option.value=captureSlot;version.append(option);}version.value=slot;label.append(version);
  version.addEventListener('change',()=>go({slot:Number(version.value)},{replace:true}));
  controls.append(label,external(`https://web.archive.org/web/${captures[slot].timestamp}/${captures[slot].url}`,'Open this capture in Wayback'),node('p',captures[slot].url,'meta'));controls.querySelector('a').className='site-citation';
  if (review) {
    const download=node('a','Download original file');download.href=`/api/source?batch=${encodeURIComponent(review.id)}&slot=${review.source_slots[slot]}&download=1`;download.setAttribute('download','');controls.append(download);
    if (!isPage(captures[slot])) controls.append(node('p',`${captures[slot].content_type || captures[slot].mimetype || 'Supporting file'} · ${captures[slot].bytes.toLocaleString()} bytes · preserved in the archive`,'meta'));
  }
  if (captures[slot].entry_redirect) controls.append(node('p',`Verified archived redirect from ${captures[slot].entry_redirect.requested_url} to the source shown above.`,'meta'));
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
    if (source===undefined) { const result=await request(`/api/source?${key}`);source=result.complete_extracted_text ?? result.file_message;sourceCache.set(cacheKey,source);if (sourceCache.size>12) sourceCache.delete(sourceCache.keys().next().value); }
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
  const signature=JSON.stringify([row.id,row.state,row.stage,row.manifest_sha256,row.candidate_check,review?.state,review?.operation?.state,review?.job,op?.id,op?.state,hasOperation(),hasOperation('capture'),hasOperation('indexing'),route.panel,route.page,route.slot,draft.dirty,busy]);
  if (!force && signature===dockSignature) return;
  const dock=$('action-dock'),inner=node('div',undefined,'dock-inner'),buttons=node('div',undefined,'dock-buttons');let hint='';
  if (route.panel==='reader') {
    const {groups}=pageGroups(row,review);
    const previous=control('← Previous',()=>{const index=route.page-1;go({page:index,slot:groups[index].slots[0]},{replace:true});});previous.setAttribute('aria-label','Previous page');previous.disabled=route.page<=0;
    const next=control('Next →',()=>{const index=route.page+1;go({page:index,slot:groups[index].slots[0]},{replace:true});});next.setAttribute('aria-label','Next page');next.disabled=route.page>=groups.length-1;
    const files=review && route.filetype!=='pages';
    hint=`${files ? 'File' : 'Page'} ${route.page+1} of ${groups.length}`;
    const pages=control(files ? 'Files' : 'Pages',()=>backFromSite());pages.setAttribute('aria-label',files ? 'Back to file list' : 'Back to page list');buttons.append(previous,pages,next);
  } else if (route.panel==='pages') buttons.append(control('Back to site',()=>backFromSite(),'primary'));
  else if (row.stage==='candidates') {
    hint=approvalBlock(row) || 'Capture → publish → index · AI enrichment up to $2/site';
    buttons.append(mutation('Approve site for capture',()=>request('/api/decisions',[{id:row.id,manifest_sha256:row.manifest_sha256,decision:'approve'}]),'Approved for capture. Undo is available in Queue until capture starts.',true,Boolean(approvalBlock(row)),{returnToCandidates:true,quiet:true}));
  } else if (row.stage==='queued') {
    hint=row.state==='capture_resume_queued' ? 'Resume is queued. Cancel until the worker starts it.' : 'Your approval is saved. Capture starts automatically.';
    buttons.append(mutation(undoCaptureLabel(row),()=>undoApproval(row),undoCaptureMessage(row),true));
  } else if (row.stage==='capturing' && op?.state==='interrupted') {
    hint='Add this capture to the queue. Saved files and progress are retained; automatic mode can stay on.';
    buttons.append(mutation('Resume capture',()=>request('/api/resume',{id:op.id}),'Resume added to the capture queue. Cancel it there until capture starts.',true));
  } else if (row.stage==='indexing' && needsRegeneration(review) && ['awaiting_review','index_preflight_failed'].includes(review.state)) {
    hint='All files · 1999–2006 · reuse saved captures';
    buttons.append(mutation('Regenerate full capture',()=>request('/api/continue-capture',{id:review.id,manifest_sha256:review.manifest_sha256}),'Full capture queued. Undo is available until it starts.',true));
  } else if (row.stage==='indexing' && review?.state==='index_preflight_failed') {
    hint='Recheck saved files and the original approval';
    buttons.append(mutation('Retry indexing preparation',()=>request('/api/prepare-indexing',{id:review.id,manifest_sha256:review.manifest_sha256}),'Indexing preparation queued again.',true));
  } else if (row.stage==='indexing' && review?.state==='publication_requested' && review.operation?.state==='interrupted') {
    hint='Approval retained · no recapture needed';buttons.append(mutation('Retry publication',()=>request('/api/resume',{id:review.operation.id}),'Publication queued again.',true,hasOperation('indexing')));
  } else if (row.stage==='indexing' && review?.state==='index_failed' && review.job?.name) {
    hint='Reuse saved AI results and the original site budget';buttons.append(mutation('Retry indexing',()=>request('/api/index-retry',{id:review.id,manifest_sha256:review.manifest_sha256,job_name:review.job.name}),'Indexing queued again.',true));
  } else if (row.coverage?.candidate_exclusion || row.coverage?.ezboard_parent_required) {
    buttons.append(control('Back to Candidates',()=>openStage('candidates'),'primary'));
  } else if (row.stage==='saved' || row.stage==='history' && row.state==='rejected') {
    buttons.append(mutation('Restore to Candidates',()=>request('/api/restore',{id:row.id,manifest_sha256:row.manifest_sha256}),'Restored to Candidates.',true));
  } else if (row.stage==='history' && review?.state==='indexing_declined') buttons.append(mutation('Resume automatic indexing',()=>siteDecision('reconsider'),'Automatic indexing queued.',true));
  else if (row.stage==='history') buttons.append(control('Back to active sites',()=>openStage('candidates'),'primary'));
  if (hint || busy) inner.append(node('p',busy ? 'Saving your decision…' : hint,'dock-hint'));inner.append(buttons);dock.replaceChildren(inner);dock.hidden=!buttons.childElementCount;dockSignature=signature;measureDock();
}
function measureDock() { document.documentElement.style.setProperty('--dock',`${$('action-dock').hidden ? 0 : $('action-dock').getBoundingClientRect().height}px`); }
function activeOperation(lane='candidates') {
  return data?.workers?.[lane] || data?.operations.find(op=>['queued','running'].includes(op.state) && (op.kind==='publish' ? 'indexing' : op.kind==='capture' ? 'capture' : 'candidates')===lane);
}
function hasOperation(lane='candidates') { return Boolean(activeOperation(lane)); }
function currentDiscovery() { const active=activeOperation();return active?.kind==='discover' ? active : data?.operations.find(op=>op.kind==='discover' && !op.payload.automatic && ['queued','running','interrupted'].includes(op.state)); }
function operationLabel(op) { return op.kind==='discover' && op.payload.target ? 'Site check' : {publish:'Archive publication',capture:'Capture',discover:'Discovery',candidate_check:'Evidence & grading'}[op.kind] || 'Another task'; }
function renderDiscovery() {
  renderAutomation();
  const automatic=Boolean(data?.automation?.settings?.enabled);
  $('discovery').disabled=!data || automatic;
  $('grading-advanced').inert=!data || automatic;
  $('discovery').hidden=route.view!=='candidates' || Boolean(route.candidate);
  $('manual-site').hidden=$('discovery').hidden;
  $('minimum-grade').value=route.minGrade;$('minimum-grade-value').value=route.minGrade;
  $('grade-help').textContent=route.needsGrade ? 'Sites needing source evidence or a grade. Completed grades appear in Graded sites at your chosen minimum.' : `Show Grade ${route.minGrade} and above, highest first. Lower the slider to see lower grades.`;
  $('graded-view').setAttribute('aria-pressed',String(!route.needsGrade));$('ungraded-view').setAttribute('aria-pressed',String(route.needsGrade));
  $('ungraded-view').textContent=`Needs grading (${data?.candidate_grades?.['-1'] ?? 0})`;
  $('dismiss-all').textContent=`Dismiss all candidates (${data?.dismissal?.count ?? data?.stage_counts.candidates ?? 0})`;
  $('dismiss-all').disabled=busy || !data?.dismissal?.count;
  const active=activeOperation(),discovery=currentDiscovery();
  const paused=discovery?.state==='interrupted' && !discovery.payload.automatic;
  $('discover').textContent=paused ? discovery.payload.target ? 'Resume site check' : 'Resume discovery' : 'Discover & grade';
  $('discover').disabled=!data || busy || Boolean(active);
  $('run-criteria').hidden=!discovery;
  $('run-criteria').textContent=discovery ? `Saved run criteria: ${discovery.payload.grading_criteria || 'General EverQuest relevance'}. ${paused ? 'Resume uses these criteria; Advanced edits apply only to new runs.' : 'Advanced edits apply only to new runs.'}` : '';
  $('discovery-limits').textContent=discovery ? discovery.payload.fill_queue ? `Target ${discovery.payload.max_candidates} candidates at Grade ${discovery.payload.min_grade}+ · 1 hour · $${discovery.payload.max_usd} total cap` : `Up to ${discovery.payload.max_candidates} candidates · $${discovery.payload.max_usd} original cap` : `Target 50 candidates at Grade ${route.minGrade}+ · 1 hour · $2 total cap`;
  $('discovery').dataset.state=automatic ? 'automatic' : active || paused ? 'blocked' : 'ready';
  const status=automatic ? 'Automatic mode is on. Use its settings above, or turn it off to discover manually.' : !data ? 'Checking worker availability…' : busy ? 'Submitting your request…' : active ?
    `${operationLabel(active)} is ${active.state}. ${active.kind==='discover' ? 'New sites are checked against the current approval settings.' : 'Discovery becomes available when current work finishes.'}` : paused ?
    `${operationLabel(discovery)} paused. Resume within the original limits; previous spending still counts.${discovery.error ? ' '+discovery.error : ''}` :
    'Ready. Capture and indexing run separately. Wayback requests take turns. Automatic mode, when enabled, promotes qualifying sites.';
  if ($('discovery-status').textContent!==status) $('discovery-status').textContent=status;
  $('manual-submit').disabled=!data || busy || Boolean(active) || !$('manual-url').value.trim();
  renderDiscoveryProgress();renderManualResult();
}
function renderDiscoveryProgress() {
  const operation=data?.activity?.discovery?.payload.fill_queue ? data.activity.discovery : data?.operations.find(op=>op.kind==='discover' && op.payload.fill_queue);
  const progress=operation?.result?.progress,box=$('discovery-progress');box.hidden=!progress;
  if (data?.automation?.settings.enabled) box.hidden=true;
  if (!progress) return;
  $('discovery-meter').max=progress.target;$('discovery-meter').value=progress.accepted;
  const phases={finding_links:'Finding linked sites',resolving_ezboard:'Identifying the parent Ezboard',checking_coverage:'Checking archive coverage',sampling:'Reading Wayback samples',grading:'Grading with Luna',paused:'Paused'};
  const reasons={target_reached:'Target reached',time_limit:'One-hour limit reached',spend_limit:'Luna budget reached',links_exhausted:'No more new sites in the available link graph'};
  const remaining=Math.max(0,Math.ceil((progress.deadline-Date.now()/1000)/60));
  const state=operation.state==='interrupted' ? 'Paused — resume within the original limits' : reasons[progress.stop_reason] || phases[progress.phase] || 'Waiting for the worker';
  $('discovery-progress-text').textContent=`${progress.accepted}/${progress.target} new Grade ${progress.min_grade}+ sites · ${progress.checked} checked. ${state}. ${operation.state==='running' ? `${remaining} min left. ` : ''}Luna: ${usd(progress.estimated_usd)} estimated; ${usd(progress.reserved_usd)} reserved of $${progress.max_usd}. Results refresh every 5 seconds while this page is open.`;
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
      const result=await request('/api/submit-site',{url,max_usd:2,...newGradingCriteria()});manualResult=result;
      if ($('manual-url').value===url) $('manual-url').value='';
      route.query='';route.offset=0;writeRoute(true);return result;
    } catch (error) { await refresh();throw error; }
  },result=>result.candidate_id ? 'Site already found. Open its existing entry below.' : result.existing ? 'This site check already exists. Its original budget is retained.' : 'Site check queued: one site, up to $2.');
}
async function startDiscovery() {
  if (!data || busy || hasOperation() || data.automation?.settings?.enabled) return;
  const discovery=currentDiscovery(),paused=discovery?.state==='interrupted';
  await act(paused ? 'Resuming discovery' : 'Starting discovery',async()=>{
    try { return await request(paused ? '/api/resume' : '/api/discover',paused ? {id:discovery.id} : {max_candidates:50,max_usd:2,min_grade:route.minGrade,...newGradingCriteria()}); }
    catch (error) { await refresh();throw error; }
  },paused ? 'Discovery resumed within its original budget.' : 'Discovery queued: target 50 new sites at the selected grade, within one hour and $2 total.');
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
      log.append(node('p',`Grading focus: ${op.payload.grading_criteria || 'General EverQuest relevance'}`,'meta'));
      if (op.state==='interrupted') log.append(mutation('Resume discovery',()=>request('/api/resume',{id:op.id}),'Discovery resumed within its original budget.',false,hasOperation()));}
    return log;
  });
  $('operations').replaceChildren(...operations);
}
let activityReceived=0,activityDisconnected=false,activityView=null;
const discoveryPhases={finding_links:'Finding linked sites',resolving_ezboard:'Identifying the parent Ezboard',checking_coverage:'Checking archive coverage',coverage:'Checking archive coverage',sampling:'Reading Wayback samples',grading:'Grading with Luna',paused:'Paused'};
const discoveryStops={target_reached:'Target reached',time_limit:'One-hour limit reached',spend_limit:'Run spending limit reached',links_exhausted:'No new links remain in this run'};
function activitySite(host,site) {
  if (!site?.id) return;
  const link=node('a',site.url || 'Open site','activity-site');link.href=`/?view=candidates&candidate=${encodeURIComponent(site.id)}`;
  link.addEventListener('click',event=>{event.preventDefault();openSite(site.id);});host.append(link);
}
function activityDeadline(host,stamp,prefix) {
  const text=node('p',undefined,'meta');text.dataset.activityDeadline=stamp;text.dataset.prefix=prefix;host.append(text);
}
function activityAge(host,stamp) {
  const text=node('p',undefined,'meta');text.dataset.activityUpdated=stamp;host.append(text);
}
function updateActivityClocks() {
  const age=activityReceived ? Math.max(0,Math.floor((Date.now()-activityReceived)/1000)) : null;
  const connection=$('pipeline-connection');
  connection.dataset.stale=String(activityDisconnected || age>20);
  connection.textContent=activityDisconnected ? `Updates unavailable. ${age===null ? 'No worker status received.' : `Showing the last snapshot from ${age}s ago.`} Retrying every 5 seconds while visible.` : age===null ? 'Connecting to workers…' : `${age>20 ? 'Status may be stale. ' : ''}Checked ${age}s ago · refreshes every 5 seconds while visible`;
  for (const text of document.querySelectorAll('[data-activity-deadline]')) {
    const seconds=Math.max(0,Math.ceil((Date.parse(text.dataset.activityDeadline)-Date.now())/1000));
    text.textContent=seconds>0 ? `${text.dataset.prefix} in ${Math.floor(seconds/60)}m ${seconds%60}s (${new Date(text.dataset.activityDeadline).toLocaleTimeString()}).` : `${text.dataset.prefix} is due; waiting for the worker’s next check.`;
  }
  for (const text of document.querySelectorAll('[data-activity-updated]')) {
    const seconds=Math.max(0,Math.floor((Date.now()-Date.parse(text.dataset.activityUpdated))/1000));
    text.textContent=Number.isFinite(seconds) ? `Last worker update ${seconds<60 ? `${seconds}s` : `${Math.floor(seconds/60)}m`} ago${seconds>=60 ? '; no newer progress reported' : ''}.` : 'Waiting for the first worker update.';
  }
}
function renderPipelineActivity() {
  if (!data) return;
  const info=data.activity || {},auto=data.automation,enabled=Boolean(auto?.settings.enabled);
  $('pipeline-mode').textContent=enabled ? 'Automatic on' : 'Manual discovery';
  const selected={candidates:'candidates',queued:'capture',capturing:'capture',indexing:'indexing'}[route.view];
  const laneChanged=activityView!==route.view;activityView=route.view;
  const discovery=activeOperation('candidates'),capture=activeOperation('capture'),publication=activeOperation('indexing');
  for (const [lane,title] of [['candidates','Discovery'],['capture','Capture'],['indexing','Indexing']]) {
    let card=$(`activity-${lane}`);
    if (!card) {
      card=node('details',undefined,'activity-worker');card.id=`activity-${lane}`;
      card.append(node('summary'),node('div',undefined,'activity-body'));$('pipeline-workers').append(card);
    }
    if (laneChanged) card.open=selected===lane;
    const signature=JSON.stringify([lane==='candidates' ? [discovery,info.discovery,auto] : lane==='capture' ? [capture,info.next_capture,data.capture_attention,data.capture_queue_error] : [publication,info.indexing,auto?.resets_at],info.runtime,data.stage_counts]);
    if (card.dataset.signature===signature) continue;card.dataset.signature=signature;
    const body=node('div',undefined,'activity-body');let status='',tone='idle';
    if (lane==='candidates') {
      const latest=discovery || info.discovery,progress=latest?.result?.progress,phase=auto?.activity?.phase;
      if (discovery) {
        status=discovery.state==='queued' ? 'Queued to start' : discoveryPhases[progress?.phase || discovery.result?.phase] || (discovery.kind==='candidate_check' ? 'Checking a site' : 'Discovering sites');tone='running';
        activitySite(body,info.discovery?.id===discovery.id ? info.discovery.site : discovery.payload?.target);
        if (progress?.current_url) body.append(node('p',progress.current_url,'address'));
        body.append(node('p','Wayback requests take turns with captures; Luna grading runs independently.','meta'));
        activityAge(body,discovery.updated);
      } else if (enabled) {
        if (phase==='daily_budget') {
          status='Waiting for daily Luna funds';tone='waiting';
          body.append(node('p',`$${Number(auto.remaining_usd).toFixed(4)} remaining today. Saved work resumes automatically when funded.`));
          activityDeadline(body,auto.resets_at,'Daily budget reset');
          if (auto.activity.required_usd>auto.settings.daily_usd) body.append(node('p','The next request exceeds the daily limit. Raise the limit in automatic settings to continue.','activity-warning'));
        } else if (phase==='links_exhausted' || phase==='retry_wait') {
          status=phase==='links_exhausted' ? 'Waiting for new links' : 'Waiting to retry';tone='waiting';
          body.append(node('p',phase==='links_exhausted' ? 'All currently available new links have been checked. New captures can supply more links; remembered sites are kept.' : auto.activity.error || 'Saved progress is retained.'));
          activityDeadline(body,auto.activity.retry_at,'Next discovery check');
        } else if (phase==='attention') {status='Needs attention';tone='attention';body.append(node('p',auto.activity.error || 'Automatic scheduling is paused.','activity-warning'));}
        else {status='Waiting for next automatic check';tone='waiting';body.append(node('p','The worker checks for eligible sites and new links every 10 seconds.'));}
      } else {status='Ready for manual discovery';body.append(node('p','Use Discover & grade or Add a site in Candidates. Approved capture and indexing work continues.'));}
      if (progress) {
        body.append(node('p',`${discovery ? 'This run' : 'Last run'}: ${progress.accepted}/${progress.target} Grade ${progress.min_grade ?? auto?.settings.min_grade ?? 2}+ sites · ${progress.checked} checked · ${progress.skipped || 0} skipped.`,'activity-counts'));
        if (discovery) {const bar=node('progress');bar.max=progress.target || 50;bar.value=progress.accepted || 0;bar.setAttribute('aria-label','Discovery qualifying sites');body.append(bar);}
        body.append(node('p',`${discoveryStops[progress.stop_reason] || ''}${progress.stop_reason ? '. ' : ''}Luna: ${usd(progress.estimated_usd || 0)} estimated + ${usd(progress.reserved_usd || 0)} reserved${progress.max_usd ? ` of $${progress.max_usd}` : ''}.`,'meta'));
        if (discovery && progress.deadline) activityDeadline(body,new Date(progress.deadline*1000).toISOString(),'Run deadline');
        if (!discovery && latest.updated) body.append(node('p',`Last run updated ${new Date(latest.updated).toLocaleString()}.`,'meta'));
      }
      if (!discovery && latest?.state==='interrupted') {body.append(node('p',`Last check paused: ${latest.error || 'Saved progress needs an explicit resume.'}`,'activity-warning'));activitySite(body,latest.site);}
    } else if (lane==='capture') {
      const waiting=data.stage_counts.queued || 0,next=info.next_capture,attention=data.capture_attention || {};
      if (capture) {
        status=capture.state==='queued' ? 'Starting capture' : capture.result?.progress?.phase==='checking_wayback' ? 'Checking archive listings' : 'Capture running';tone='running';
        if (Number.isInteger(capture.result?.progress?.files)) status+=` · ${capture.result.progress.files.toLocaleString()} files saved`;
        const site=capture.payload?.sites?.find(site=>site.url===capture.result?.progress?.site_url) || capture.payload?.sites?.[0];
        activitySite(body,site);body.append(captureMeter(capture,true));
        body.append(node('p',`${waiting} sites waiting. The next eligible site starts after this capture finishes or pauses.`, 'meta'));
      } else if (data.capture_queue_error) {status='Queue needs attention';tone='attention';body.append(node('p',data.capture_queue_error,'activity-warning'));}
      else if (next) {status='Waiting to start next site';tone='waiting';activitySite(body,next);activityDeadline(body,next.ready,'Next capture eligible');}
      else if (waiting) {status='Queued sites need attention';tone='attention';body.append(node('p','No queued site is eligible to start. Open its entry to resolve the approval or preparation error.'));}
      else {status='No sites waiting';body.append(node('p','Approved sites enter the queue. Failed captures need an explicit Resume; other sites continue.'));}
      if (attention.interrupted || attention.preflight) body.append(node('p',`${attention.interrupted || 0} paused captures · ${attention.preflight || 0} approval errors. These sites wait for your decision while eligible sites continue.`,'activity-warning'));
    } else {
      const index=info.indexing;
      if (publication) {status='Publishing archive files';tone='running';body.append(node('p','Git publication can take several minutes. Capture and discovery continue.'));activityAge(body,publication.updated);}
      else if (index) {
        status=labels[index.state] || 'Preparing indexing';tone=index.state==='indexing' ? 'running' : 'waiting';
        activitySite(body,index.site);
        if (index.job?.waiting_for?.length) {status='Waiting for existing indexing Jobs';body.append(node('p',`Waiting for: ${index.job.waiting_for.join(', ')}`,'address'));}
        else if (index.state==='index_budget_waiting') activityDeadline(body,auto?.resets_at || index.job?.retry_at,'Daily budget reset');
        else body.append(node('p',index.state==='indexing' ? 'AI enrichment and import are running. Completed sites retire to History.' : 'The indexing worker checks approved work every 10 seconds.'));
        if (index.job?.name) body.append(node('p',`Job: ${index.job.name}`,'address'));
        if (index.error) {tone='attention';body.append(node('p',index.error,'activity-warning'));}
      } else if (data.stage_counts.indexing) {status='Sites need attention';tone='attention';body.append(node('p',`${data.stage_counts.indexing} sites remain in Indexing. Open their entries for the saved error and retry options.`));}
      else {status='No sites waiting';body.append(node('p','Completed captures publish and index automatically.'));}
    }
    if (info.runtime?.[lane]===false || lane!=='indexing' && info.runtime?.wayback===false) {
      status=info.runtime?.[lane]===false ? 'Worker unavailable' : 'Wayback connection unavailable';tone='attention';
      body.prepend(node('p','Saved progress is retained. The service needs to recover before this work can continue.','activity-warning'));
    }
    const summary=card.querySelector('summary');summary.replaceChildren(node('strong',title),node('span',status,'activity-state'));card.dataset.tone=tone;
    const focusedSite=card.querySelector('.activity-site')===document.activeElement;
    card.querySelector('.activity-body').replaceWith(body);
    if (focusedSite) body.querySelector('.activity-site')?.focus({preventScroll:true});
  }
  const recent=info.recent || [],signature=JSON.stringify(recent),list=$('pipeline-events');
  if (list.dataset.signature!==signature) {
    list.dataset.signature=signature;list.replaceChildren();
    $('pipeline-recent').querySelector('summary').textContent=recent.length ? `Recent activity · ${recent[0].label}` : 'Recent activity';
    for (const event of recent) {const item=node('li');item.append(node('strong',event.label),node('time',new Date(event.at).toLocaleString()));activitySite(item,event.site);if(event.error) item.append(node('p',event.error,'activity-warning'));list.append(item);}
    if (!recent.length) list.append(node('li','No completed activity recorded yet. Current work is shown above.'));
  }
  updateActivityClocks();
}

let automationDraft=null,automationDirty=false;
try {const saved=JSON.parse(localStorage.getItem('curation-automatic-draft'));if (saved && typeof saved.enabled==='boolean' && Number.isInteger(saved.revision)) {automationDraft=saved;automationDirty=true;}} catch (_) {}
function automaticFields(value) {
  $('automation-enabled').checked=value.enabled;$('automation-daily').value=value.daily_usd;
  $('automation-grade').value=value.min_grade;$('automation-grade-value').value=value.min_grade;
  $('automation-criteria').value=value.grading_criteria || '';
}
function renderAutomation() {
  const auto=data?.automation,settings=auto?.settings;
  $('automation').hidden=route.view!=='candidates' || Boolean(route.candidate) || !settings;
  if (!settings) return;
  if (!automationDraft || !automationDirty && automationDraft.revision!==settings.revision) {
    automationDraft={...settings};automaticFields(automationDraft);
  }
  const waiting=auto.activity?.phase==='daily_budget',attention=auto.activity?.phase==='attention';
  $('automation-state').textContent=settings.enabled ? waiting ? 'Daily limit' : attention ? 'Needs attention' : 'On' : 'Off';
  $('automation-state').dataset.tone=attention ? 'attention' : settings.enabled ? 'active' : '';
  const reset=new Date(auto.resets_at).toUTCString();
  let status=settings.enabled ? `Continuously discovering and grading. Sites at Grade ${settings.min_grade}+ are automatically captured and indexed; lower grades are saved for later.` : 'Off. Turn on automatic mode to keep discovering, grading, capturing and indexing sites.';
  if (settings.enabled && waiting) status=`Waiting for daily Luna funds. Discovery resumes automatically after the reset at ${reset}. Captures and approved work retain their progress.`;
  if (settings.enabled && waiting && auto.activity.required_usd>settings.daily_usd) status=`The next grading request needs a $${auto.activity.required_usd.toFixed(4)} reservation, exceeding the daily limit. Raise the limit below to continue. Progress is retained.`;
  if (settings.enabled && auto.activity?.phase==='links_exhausted') status='All currently known new links have been checked. Discovery checks again in five minutes; remembered sites are kept.';
  if (settings.enabled && ['attention','retry_wait'].includes(auto.activity?.phase)) status=(auto.activity.phase==='retry_wait' ? `Retrying at ${new Date(auto.activity.retry_at).toUTCString()}. ` : 'Automatic discovery needs attention. ')+(auto.activity.error || 'See Recent activity.');
  $('automation-status').textContent=status;
  $('automation-budget').textContent=!settings.configured ? `Daily limit not configured. Default proposal: $${settings.daily_usd.toFixed(2)}/day. Saving settings includes today's earlier portal spending.` : `Today: $${auto.estimated_usd.toFixed(4)} estimated + $${auto.unresolved_usd.toFixed(4)} reserved · $${auto.remaining_usd.toFixed(4)} remaining of $${settings.daily_usd.toFixed(2)}. Resets at 00:00 UTC.`;
  $('automation-meter').hidden=!settings.configured;$('automation-meter').max=settings.daily_usd;$('automation-meter').value=auto.estimated_usd+auto.unresolved_usd;
  $('automation-draft').textContent=automationDirty ? automationDraft.revision!==settings.revision ? 'Saved settings changed in another session. Reload saved settings before applying your changes.' : 'Unsaved settings. The current mode continues until you save.' : 'Changes take effect only when saved.';
  for (const id of ['automation-enabled','automation-daily','automation-grade','automation-criteria']) $(id).disabled=busy;
  $('automation-save').disabled=busy || automationDraft.revision!==settings.revision;
  $('automation-reload').disabled=busy;
  $('automation-footer').textContent=`Private intranet · automatic mode ${settings.enabled ? 'on' : 'off'}${settings.configured ? ` · $${settings.daily_usd.toFixed(2)}/day Luna limit (UTC)` : ''}`;
}
if (automationDraft) automaticFields(automationDraft);
for (const id of ['automation-enabled','automation-daily','automation-grade','automation-criteria']) $(id).addEventListener('input',()=>{
  automationDraft={enabled:$('automation-enabled').checked,daily_usd:$('automation-daily').value,min_grade:Number($('automation-grade').value),grading_criteria:$('automation-criteria').value,revision:automationDraft?.revision ?? data?.automation?.settings.revision};
  automationDirty=true;$('automation-grade-value').value=automationDraft.min_grade;
  try {localStorage.setItem('curation-automatic-draft',JSON.stringify(automationDraft));} catch (_) {}
  renderAutomation();
});
$('automation-reload').addEventListener('click',()=>{
  if (busy || !data?.automation) return;
  automationDirty=false;automationDraft={...data.automation.settings};automaticFields(automationDraft);
  try {localStorage.removeItem('curation-automatic-draft');} catch (_) {}renderAutomation();
});
$('automation-form').addEventListener('submit',event=>{
  event.preventDefault();if (!data?.automation || busy) return;
  const payload={enabled:$('automation-enabled').checked,daily_usd:Number($('automation-daily').value),min_grade:Number($('automation-grade').value),grading_criteria:$('automation-criteria').value,revision:automationDraft.revision};
  act('Saving automatic mode',async()=>{
    const result=await request('/api/automation',payload);data.automation=result;automationDraft={...result.settings};automationDirty=false;
    automaticFields(automationDraft);try {localStorage.removeItem('curation-automatic-draft');} catch (_) {}
    return result;
  },result=>result.settings.enabled ? 'Automatic mode enabled. Qualifying sites will flow through capture and indexing.' : 'Automatic mode off. Already approved work keeps its progress and daily budget.');
});
$('home').addEventListener('click',event=>{event.preventDefault();openStage('candidates');});
for (const item of document.querySelectorAll('[data-view]')) item.addEventListener('click',()=>openStage(item.dataset.view));
$('tools-open').addEventListener('click',()=>$('tools').showModal());$('tools-close').addEventListener('click',()=>$('tools').close());
$('tools').addEventListener('click',event=>{if (event.target===$('tools') && (event.clientX<$('tools').getBoundingClientRect().left || event.clientX>$('tools').getBoundingClientRect().right)) $('tools').close();});
$('refresh').addEventListener('click',()=>{if (!busy) refresh(false,true);});
$('discover').addEventListener('click',startDiscovery);
$('minimum-grade').addEventListener('input',()=>{
  clearTimeout(gradeTimer);activeSwipe?.cancel();
  route={...route,minGrade:Number($('minimum-grade').value),needsGrade:false,offset:0};
  try {localStorage.setItem('candidate-min-grade',String(route.minGrade));} catch (_) {}
  writeRoute(true);++generation;controller?.abort();renderDiscovery();
  $('candidates').replaceChildren(node('p','Loading sites…','description'));listSignature='';
  gradeTimer=setTimeout(()=>refresh(),180);
});
$('graded-view').addEventListener('click',()=>go({needsGrade:false,offset:0}));
$('ungraded-view').addEventListener('click',()=>go({needsGrade:true,offset:0}));
$('dismiss-all').addEventListener('click',()=>{
  if (busy || !data?.dismissal?.count) return;
  dismissSnapshot={...data.dismissal};
  $('dismiss-description').textContent=`Move all ${dismissSnapshot.count} remaining candidates to History, including sites hidden by grade or search filters and other pages. Queued captures and other stages stay as they are. Undo will be available.`;
  $('dismiss-dialog').showModal();
});
$('dismiss-cancel').addEventListener('click',()=>$('dismiss-dialog').close());
$('dismiss-confirm').addEventListener('click',()=>{
  if (!dismissSnapshot || busy) return;
  const snapshot=dismissSnapshot;dismissSnapshot=null;$('dismiss-dialog').close();remember();
  act('Dismissing candidates',()=>request('/api/dismiss-candidates',{token:snapshot.token}),result=>`Moved ${result.dismissed} candidates to History.`,
    {undo:result=>({path:'/api/undo-dismissal',payload:{dismissal:result.dismissal},label:'Undo dismiss all'})});
});
$('manual-form').addEventListener('submit',submitManualSite);
function newGradingCriteria() { const value=$('grading-criteria').value.trim();return value ? {grading_criteria:value} : {}; }
try { $('grading-criteria').value=localStorage.getItem('candidate-grading-criteria') || ''; } catch (_) {}
function saveGradingCriteria() {
  try { localStorage.setItem('candidate-grading-criteria',$('grading-criteria').value); } catch (_) {}
  listSignature='';workspaceSignature='';if (data) {renderList();renderWorkspace();}
}
$('grading-criteria').addEventListener('input',saveGradingCriteria);
$('grading-default').addEventListener('click',()=>{$('grading-criteria').value='';saveGradingCriteria();});
$('manual-url').addEventListener('input',renderDiscovery);
$('previous').addEventListener('click',()=>go({offset:Math.max(0,route.offset-50)}));$('next').addEventListener('click',()=>go({offset:route.offset+50}));
$('site-search').addEventListener('input',()=>{
  clearTimeout(searchTimer);route.query=$('site-search').value;route.offset=0;writeRoute(true);
  ++generation;controller?.abort();searchTimer=setTimeout(()=>refresh(),200);
});
window.addEventListener('popstate',()=>{activeSwipe?.cancel();remember();route=readRoute();renderShell();refresh(true);});
window.addEventListener('resize',()=>{activeSwipe?.cancel();measureDock();if (detail && route.panel==='pages' && matchMedia('(min-width:1000px)').matches) {workspaceSignature='';renderWorkspace();restorePosition();}});
window.addEventListener('pointerdown',event=>{if (!event.isPrimary) activeSwipe?.cancel();},true);
new ResizeObserver(measureDock).observe($('action-dock'));
renderShell();writeRoute(true);refresh(true);
setInterval(()=>{if (!busy && !document.hidden) refresh();},5000);
setInterval(()=>{updateCountdown();updateCaptureEta();updateCaptureActivity();updateActivityClocks();},1000);
