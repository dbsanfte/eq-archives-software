import React, { useEffect, useRef, useState } from 'react';
import HeaderContent from '../HeaderContent';
import ArchiveStatusBar from '../ArchiveStatusBar';
import { DEFAULT_SELECTION, readSelection, validSelection, selectionParams, exploreSearchUrl, fetchExplore } from './ExploreService';
import '../sites/Sites.css';
import './Explore.css';

const number = value => value.toLocaleString();
const compact = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });

function useExplore(kind, selection, revision) {
  const key = selectionParams(selection);
  const [state, setState] = useState({ key: '', data: null, loading: true, error: false });
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setState(old => ({ key, data: old.key === key ? old.data : null, loading: true, error: false }));
    const timeout = setTimeout(() => controller.abort(), 25000);
    fetchExplore(kind, { ...DEFAULT_SELECTION, ...Object.fromEntries(new URLSearchParams(key)) }, controller.signal).then(data => {
      if (active) setState({ key, data, loading: false, error: false });
    }).catch(() => {
      if (active) setState(old => ({ ...old, loading: false, error: true }));
    }).finally(() => clearTimeout(timeout));
    return () => { active = false; clearTimeout(timeout); controller.abort(); };
  }, [kind, key, revision]);
  return state.key === key ? state : { data: null, loading: true, error: false };
}

function RequestStatus({ resource, label, retry }) {
  if (resource.error) return <div role="alert" className="explore-error">
    Could not update {label}. {resource.data && 'The previous snapshot for this selection is still shown. '}
    <button onClick={retry}>Retry {label}</button>
  </div>;
  if (resource.loading) return <p role="status" className="explore-note">Loading {label}…</p>;
  return null;
}

export default function Explore() {
  const [route, setRoute] = useState(() => readSelection(window.location.search));
  const { selection, invalid } = route;
  const [draft, setDraft] = useState(selection);
  const [revision, setRevision] = useState(0);
  const [phraseRevision, setPhraseRevision] = useState(0);
  const [metric, setMetric] = useState('records');
  const [panel, setPanel] = useState('timeline');
  const previousPanel = useRef(panel);
  const [cloud, setCloud] = useState(true);
  const [share, setShare] = useState('');
  const overview = useExplore('overview', selection, revision);
  const phrases = useExplore('phrases', selection, phraseRevision);
  const data = overview.data;
  const words = phrases.data;

  useEffect(() => {
    if (previousPanel.current === panel) return;
    previousPanel.current = panel;
    if (window.innerWidth <= 900) document.getElementById(`explore-${panel}`)?.scrollIntoView?.({ block: 'start' });
  }, [panel]);

  useEffect(() => {
    const pop = () => { setRoute(readSelection(window.location.search)); setShare(''); };
    window.addEventListener('popstate', pop);
    return () => window.removeEventListener('popstate', pop);
  }, []);
  useEffect(() => {
    setDraft(current => ({ ...current, start: selection.start, end: selection.end, basis: selection.basis }));
  }, [selection.start, selection.end, selection.basis]);

  function select(patch) {
    const next = { ...selection, ...patch };
    window.history.pushState({}, '', `/explore?${selectionParams(next)}`);
    setRoute({ selection: next, invalid: false });
    setShare('');
  }
  async function copyLink() {
    const url = `${window.location.origin}/explore?${selectionParams(selection)}`;
    try { await navigator.clipboard.writeText(url); setShare('Link copied.'); }
    catch (_) { setShare(url); }
  }
  const maxBar = Math.max(1, ...(data?.timeline.map(b => b[metric]) || []));
  const maxTheme = Math.max(1, ...(data?.themes.map(b => b.count) || []));
  const maxWord = Math.max(1, ...(words?.phrases.map(p => p.pages) || []));
  const minWord = Math.min(maxWord, ...(words?.phrases.map(p => p.pages) || []));
  const busy = overview.loading;

  return <div className="archive-explore"><div className="sites-shell">
    <ArchiveStatusBar /><HeaderContent compact />
    <main>
      <div className="explore-introduction">
        <p className="archive-eyebrow">Follow a thread through the archive</p>
        <h1>Explore early EverQuest.</h1>
        <p>Choose a time. Find a theme. Discover the pages behind it.</p>
      </div>
      {invalid && <p role="alert" className="explore-error">This link has invalid filters. Showing the default 1999–2006 selection.</p>}
      <details className="explore-date-settings" open={window.innerWidth > 900}>
        <summary>Choose dates <span>{selection.start} → {selection.end}</span></summary>
      <form className="explore-controls" aria-label="Explore dates" onSubmit={event => {
        event.preventDefault();
        if (validSelection({ ...selection, ...draft })) select({ start: draft.start, end: draft.end, basis: draft.basis });
      }}>
        <label>Date meaning<select value={draft.basis} onChange={e => setDraft({ ...draft, basis: e.target.value })}>
          <option value="capture_date">Archive capture date</option>
          <option value="llm_guessed_date">Estimated publication date</option>
        </select></label>
        <label>From<input type="date" min="1990-01-01" max="2099-12-31" required value={draft.start}
          onChange={e => setDraft({ ...draft, start: e.target.value })} /></label>
        <label>Through<input type="date" min="1990-01-01" max="2099-12-31" required value={draft.end}
          onChange={e => setDraft({ ...draft, end: e.target.value })} /></label>
        <button className="explore-primary" type="submit" disabled={!validSelection({ ...selection, ...draft })}>Apply dates</button>
        <div className="explore-presets" aria-label="Date presets">
          <span>Jump to</span>
          <button type="button" onClick={() => select({ start: '1999-01-01', end: '2001-12-31' })}>1999–2001</button>
          <button type="button" onClick={() => select({ start: '2002-01-01', end: '2006-12-31' })}>2002–2006</button>
          <button type="button" onClick={() => select({ start: '1999-01-01', end: '2006-12-31' })}>1999–2006</button>
        </div>
      </form>
      </details>
      <div className="explore-selection" aria-label="Current exploration">
        <div className="explore-chips">
          {['theme', 'site', 'phrase'].filter(key => selection[key]).map(key => <button key={key}
            aria-label={`Remove ${key}: ${selection[key]}`} onClick={() => select({ [key]: '' })}>
            {selection[key]} <span aria-hidden="true">×</span>
          </button>)}
        </div>
        <div className="explore-actions">
          <button onClick={() => select(DEFAULT_SELECTION)}>Reset exploration</button>
          <button onClick={copyLink}>Share selection</button>
          <a className="explore-primary" href={exploreSearchUrl(selection)}>Search these pages <span aria-hidden="true">↗</span></a>
        </div>
        {share && (share.startsWith('http') ? <label className="explore-share">Copy this link<input readOnly value={share} onFocus={e => e.target.select()} /></label> : <span role="status">{share}</span>)}
      </div>
      <p className="explore-note">{selection.basis === 'capture_date' ? 'Capture dates record preservation, not original publication.' : 'Publication dates are AI estimates; check the source before relying on them.'} Records without this date are excluded.</p>
      {data && <>
        <div className="explore-stats" aria-label="Selection totals">
          <div><strong>{number(data.records)}</strong><span>indexed captures</span></div>
          <div><strong>≈ {number(data.sites_count)}</strong><span>domains represented</span></div>
          <div><strong>{data.records ? Math.round(data.tagged / data.records * 100) : 0}%</strong><span>with theme tags · {number(data.tagged)} captures</span></div>
        </div>
        {data.records === 0 && <p className="explore-empty">No indexed website captures match this selection. Remove a filter or widen the dates.</p>}
      </>}
      <nav className="explore-panel-nav" aria-label="Explore charts">
        {[['timeline', 'Timeline'], ['themes', 'Themes'], ['words', 'Words'], ['sites', 'Sites']].map(([id, label]) =>
          <button key={id} aria-pressed={panel === id} aria-controls={`explore-${id}`} onClick={() => setPanel(id)}>{label}</button>)}
      </nav>
      <RequestStatus resource={overview} label="charts" retry={() => setRevision(v => v + 1)} />
      {data && <>
        <section id="explore-timeline" data-selected={panel === 'timeline'} className="explore-panel explore-timeline" aria-labelledby="timeline-title" aria-busy={busy}>
          <div className="explore-panel-heading"><div><h2 id="timeline-title">Through the years</h2><p>Tap a year to focus your exploration.</p></div>
            <div className="explore-switch" aria-label="Timeline measure">
              <button aria-pressed={metric === 'records'} onClick={() => setMetric('records')}>Captures</button>
              <button aria-pressed={metric === 'sites'} onClick={() => setMetric('sites')}>Site count</button>
            </div>
          </div>
          <div className="explore-year-scroll" tabIndex="0" role="group" aria-label="Year chart; scroll horizontally for more years">
            <div className="explore-years">
              {data.timeline.map(b => <button className="explore-year" key={b.year} disabled={busy}
                aria-label={`${b.year}: ${number(b[metric])} ${metric === 'sites' ? 'estimated domains' : 'captures'}. Explore this year`}
                onClick={() => select({ start: [selection.start, `${b.year}-01-01`].sort()[1], end: [selection.end, `${b.year}-12-31`].sort()[0] })}>
                <span className="explore-year-value">{metric === 'sites' ? '≈ ' : ''}{compact.format(b[metric])}</span>
                <span className="explore-year-track" aria-hidden="true"><span style={{ height: `${Math.max(b[metric] ? 2 : 0, b[metric] / maxBar * 100)}%` }} /></span>
                <span className="explore-year-label">{b.year}</span>
              </button>)}
            </div>
          </div>
          <p className="explore-note">Counts describe the surviving archive, including repeated captures. Site counts are estimates of distinct domains.</p>
        </section>
      </>}
      <div className="explore-grid">
        <section id="explore-themes" data-selected={panel === 'themes'} className="explore-panel" aria-labelledby="themes-title" aria-busy={busy}>
          <div className="explore-panel-heading"><div><h2 id="themes-title">Follow a theme</h2><p>Topics identified in enriched pages.</p></div></div>
          {data && <>
            <ul className="explore-bars" aria-label="Themes">{data.themes.map(b => <li key={b.key}>
              <button disabled={busy} aria-pressed={selection.theme === b.key} onClick={() => select({ theme: selection.theme === b.key ? '' : b.key })}>
                <span className="explore-bar-fill" style={{ width: `${b.count / maxTheme * 100}%` }} aria-hidden="true" />
                <span>{b.key}</span><span>{b.approximate ? '≥ ' : ''}{number(b.count)}</span>
              </button>
            </li>)}</ul>
            {!data.themes.length && <p className="explore-empty">No theme tags in this selection yet. Try the source phrases or browse the sites.</p>}
            <p className="explore-note">{number(data.tagged)} of {number(data.records)} captures have tags. A capture can have several themes; untagged pages remain searchable.</p>
          </>}
        </section>
        <section id="explore-words" data-selected={panel === 'words'} className="explore-panel" aria-labelledby="phrases-title" aria-busy={phrases.loading}>
          <div className="explore-panel-heading"><div><h2 id="phrases-title">Words from the pages</h2><p>Distinctive source terms, with everyday English filtered out. Tap to explore.</p></div>
            <div className="explore-switch" aria-label="Phrase display"><button aria-pressed={cloud} onClick={() => setCloud(true)}>Cloud</button><button aria-pressed={!cloud} onClick={() => setCloud(false)}>List</button></div>
          </div>
          <RequestStatus resource={phrases} label="phrases" retry={() => setPhraseRevision(v => v + 1)} />
          {words && <>
            <ul className={cloud ? 'explore-cloud' : 'explore-phrase-list'} aria-label="Source phrases">{words.phrases.map(p => <li key={p.text}>
              <button disabled={phrases.loading} aria-pressed={selection.phrase === p.text}
                style={cloud ? {
                  fontSize: `${1 + 1.75 * (maxWord > minWord ? (p.pages - minWord) / (maxWord - minWord) : .35)}rem`,
                  fontWeight: p.pages === maxWord ? 600 : 400
                } : undefined}
                aria-label={`${p.text}: ${p.pages} sampled pages`}
                title={`${p.pages} sampled pages`}
                onClick={() => select({ phrase: selection.phrase === p.text ? '' : p.text })}>
                {p.text}{!cloud && <><span>{p.pages} pages</span>
                  <span className="explore-phrase-meter" aria-hidden="true"><span style={{ width: `${p.pages / maxWord * 100}%` }} /></span>
                </>}
              </button>
            </li>)}</ul>
            {cloud && words.phrases.length > 0 && <p className="explore-note explore-cloud-scale">{maxWord > minWord
              ? `Relative frequency: ${minWord}–${maxWord} sampled pages. See List for exact counts.`
              : `Equal frequency: every term appears on ${maxWord} sampled ${maxWord === 1 ? 'page' : 'pages'}.`}</p>}
            {!words.phrases.length && <p className="explore-empty">No distinctive source phrases in this selection. Try a broader range or another site.</p>}
            <p className="explore-note">Sample: {number(words.sampled_pages)} distinct pages across {number(words.sampled_sites)} domains.</p>
            <details className="explore-method"><summary>How this sample works</summary>
              <p>Up to {words.sample_limit} captures, spread across domains, using the first {number(words.excerpt_chars)} characters per page. Repeated pages, identical excerpts, common navigation and stop words are removed, including repeated multiword labels concentrated in one site's pages. In samples of at least eight pages, words shared by 80% or more of the sample are omitted.</p>
              <p>Everyday English words are filtered as standalone terms, but can still appear in specific phrases such as “fire resist”. Terms rank by sampled page frequency, rarity in general English and how consistently their words occur together. Phrases need an occurrence outside long lists; rare spellings receive no extra bonus. This favours distinctive names and topics without a fixed EverQuest dictionary. English-only filtering may miss other languages.</p>
              <p>The bundled English frequency reference comes from <a href="https://github.com/rspeer/wordfreq">wordfreq by Robyn Speer</a> (<a href="https://creativecommons.org/licenses/by-sa/4.0/">CC BY-SA 4.0</a>).</p>
              <p>Font sizes stretch the frequency range of the displayed terms; equal counts have equal sizes. List bars start at zero and show exact sampled page counts. This is a discovery sample, not a complete or statistically representative word count.</p>
              <p>{words.duplicate_captures} duplicate captures excluded; {words.clipped_pages} excerpts reached the length limit. Summaries are cached for up to ten minutes. No AI is used to generate these phrases.</p>
              {words.vocabulary_limited && <p>This sample reached the 50,000-term vocabulary limit; additional terms were omitted.</p>}
            </details>
          </>}
        </section>
      </div>
      {data && <section id="explore-sites" data-selected={panel === 'sites'} className="explore-panel" aria-labelledby="sites-title" aria-busy={busy}>
        <div className="explore-panel-heading"><div><h2 id="sites-title">Explore the sites</h2><p>The 20 domains with the most matching captures. Choose one to focus the charts.</p></div></div>
        <ul className="explore-sites" aria-label="Contributing sites">{data.sites.map(b => <li key={b.key}>
          <button disabled={busy} aria-pressed={selection.site === b.key} onClick={() => select({ site: selection.site === b.key ? '' : b.key })}>
            <span>{b.key}</span><span>{b.approximate ? '≥ ' : ''}{number(b.count)} captures <span aria-hidden="true">→</span></span>
          </button>
          <a href={exploreSearchUrl({ ...selection, site: b.key })} aria-label={`Search selected pages from ${b.key}`}>Search pages ↗</a>
        </li>)}</ul>
        {!data.sites.length && <p className="explore-empty">No sites match these filters.</p>}
        <p className="explore-note">Domains can contain multiple hosted sites. More captures do not necessarily mean more original pages or a larger community.</p>
      </section>}
      <div className="explore-footer"><p className="explore-note">{data && `Charts updated ${new Date(data.generated_at).toLocaleString('en-GB', { timeZone: 'UTC' })} UTC. `}Snapshots refresh on selection and are cached for up to ten minutes.</p>
        <button disabled={busy || phrases.loading} onClick={() => { setRevision(v => v + 1); setPhraseRevision(v => v + 1); }}>Check for updates</button>
      </div>
    </main>
  </div></div>;
}
