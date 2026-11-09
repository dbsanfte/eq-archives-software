'use strict';
const $=id=>document.getElementById(id);
const labels={fixed:'Policy improved',covered:'Existing policy retained',generic:'Generic policy retained',pending:'Awaiting audit'};
let report=null;
function element(tag,text,className){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;}
function render(){
  if(!report)return;
  const term=$('search').value.toLowerCase().trim(),decision=$('decision').value;
  const visible=report.sites.filter(site=>(decision==='all'||site.status===decision)&&JSON.stringify(site).toLowerCase().includes(term));
  $('sites').replaceChildren();
  const max=Math.max(...report.sites.map(site=>site.pages));
  for(const site of visible){
    const card=element('details',undefined,'site'),heading=element('summary');card.id='site-'+site.rank;
    heading.append(element('span',String(site.rank),'rank'));
    const name=element('div');name.append(element('div',site.site,'site-name'),element('div',site.pages.toLocaleString()+' HTML captures','count'),element('span',labels[site.status],'badge'));
    const meter=element('div',undefined,'meter'),bar=element('span');bar.style.width=String(site.pages/max*100)+'%';meter.append(bar);name.append(meter);heading.append(name,element('span','›','chevron'));
    const body=element('div',undefined,'audit-body');
    body.append(element('p',site.finding||'Source audit pending.'),element('p','Policies: '+(site.policies||[]).join(', '),'muted'));
    if(site.samples){
      const metrics=element('div',undefined,'metrics');
      for(const value of [site.samples+' source samples',site.verified+' validated plans',site.changed+' changed plans','Maximum '+site.max_tokens+' tokens',site.before_chunks+' → '+site.after_chunks+' chunks'])metrics.append(element('span',value));
      body.append(metrics);
    }
    if(site.checks?.length){body.append(element('h3','Verification'));const list=element('ul',undefined,'checks');for(const check of site.checks)list.append(element('li',check));body.append(list);}
    if(site.comparison){
      body.append(element('h3','Example improvement'));
      const comparison=element('div',undefined,'comparison');
      for(const [label,key] of [['Before','before'],['After','after']]){const section=element('div');section.append(element('strong',label),element('pre',site.comparison[key]));comparison.append(section);}
      body.append(comparison);
    }
    if(site.evidence?.length){
      body.append(element('h3','Captured sources'));const list=element('ul',undefined,'evidence');
      for(const evidence of site.evidence){
        const url=new URL(evidence.url);
        if(url.origin!=='https://web.archive.org')continue;
        const li=element('li'),link=element('a',evidence.label);link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';li.append(link);list.append(li);
      }body.append(list);
    }
    if(site.issues?.length){
      body.append(element('h3','Unverified samples'));const list=element('ul',undefined,'checks');
      for(const issue of site.issues)list.append(element('li',issue.reason+' '+issue.id));body.append(list);
    }
    if(site.limitations)body.append(element('p',site.limitations,'muted'));
    card.append(heading,body);$('sites').append(card);
  }
  $('status').textContent=visible.length+' of '+report.sites.length+' site groups shown. Expand a row for evidence.';
}
async function load(){
  $('reload').disabled=true;
  try{
    const response=await fetch('/assets/chunking-audit.json',{cache:'no-store'});
    if(!response.ok)throw new Error('Audit report unavailable');
    const value=await response.json();
    if(!Array.isArray(value.sites)||value.sites.length!==50)throw new Error('Incomplete audit report');
    report=value;$('scope').textContent=report.scope+' '+(report.verification||'');
    $('deployment').textContent='Baseline: '+report.baseline+' · Compared policy: '+(report.policy||'pending')+' · Report: '+(report.created?new Date(report.created).toLocaleString():'in progress')+'. Existing records advance through normal maintenance; this audit does not start a reindex.';
    $('summary').replaceChildren();
    for(const [label,n] of [['Sites audited',report.sites.filter(s=>s.status!=='pending').length],['Policy improved',report.sites.filter(s=>s.status==='fixed').length],['Source samples',report.sites.reduce((n,s)=>n+(s.samples||0),0)],['Validated plans',report.sites.reduce((n,s)=>n+(s.verified||0),0)]]){const el=element('div',undefined,'stat');el.append(element('strong',n.toLocaleString()),element('span',label));$('summary').append(el);}
    render();
  }catch(error){$('status').textContent='Could not load the audit. Use Refresh to try again.';}
  finally{$('reload').disabled=false;}
}
$('search').addEventListener('input',render);$('decision').addEventListener('change',render);$('reload').addEventListener('click',load);load();
