import React, { useEffect, useState } from 'react';
import HeaderContent from '../HeaderContent';
import ArchiveStatusBar from '../ArchiveStatusBar';
import { fetchRecentSites, RECENT_SITE_LIMIT, siteSearchUrl } from '../../search/RecentSitesService';
import './Sites.css';

const dateFormat = new Intl.DateTimeFormat('en-GB', {
  day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC'
});

export default function RecentSites() {
  const [sites, setSites] = useState([]);
  const [loadedAt, setLoadedAt] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setLoading(true);
    setError(false);
    const timeout = setTimeout(() => controller.abort(), 15000);
    fetchRecentSites(controller.signal).then(result => {
      if (!active) return;
      setSites(result);
      setLoadedAt(Date.now());
    }).catch(() => { if (active) setError(true); })
      .finally(() => { clearTimeout(timeout); if (active) setLoading(false); });
    return () => { active = false; clearTimeout(timeout); controller.abort(); };
  }, [revision]);

  return <div className="archive-sites">
    <div className="sites-shell">
      <ArchiveStatusBar />
      <HeaderContent compact />
      <main>
        <div className="sites-introduction">
          <div>
            <h1>Recently indexed sites</h1>
            <p>Choose a site to search its captured pages.</p>
          </div>
        </div>
        <p className="sites-note">The {RECENT_SITE_LIMIT} most recently indexed domains, newest first. Includes new and refreshed records.</p>
        <div className="sites-toolbar">
          <div role="status" className="sites-status">
            {loading ? 'Checking recent indexing activity…' : loadedAt && `List updated ${dateFormat.format(loadedAt)} UTC`}
          </div>
          <button className="sites-refresh" disabled={loading} onClick={() => setRevision(value => value + 1)}>
            {loading ? 'Loading…' : 'Refresh list'}
          </button>
        </div>
        {error && <p role="alert" className="sites-error">
          Could not update the site list. {loadedAt ? 'The previous list is still shown. ' : ''}Use Refresh list to try again.
        </p>}
        {!loading && !error && sites.length === 0 && <p className="sites-empty">No indexed websites are available yet. Check back after indexing completes.</p>}
        <ul className="sites-grid" aria-label="Recently indexed sites" aria-busy={loading}>
          {sites.map(site => {
            const firstYear = site.firstCapture === null ? null : new Date(site.firstCapture).getUTCFullYear();
            const lastYear = site.lastCapture === null ? null : new Date(site.lastCapture).getUTCFullYear();
            return <li key={site.domain}>
              <a className="site-card" href={siteSearchUrl(site.domain)} aria-label={`Search captures from ${site.domain}`}>
                <h2>{site.domain}</h2>
                <p className="site-card__indexed">Indexed <time dateTime={new Date(site.indexedAt).toISOString()}>{dateFormat.format(site.indexedAt)} UTC</time></p>
                <p className="site-card__records">{site.partial ? 'At least ' : ''}{site.records.toLocaleString()} indexed {site.records === 1 ? 'record' : 'records'}</p>
                {!site.partial && firstYear !== null && lastYear !== null && <p className="site-card__captures">Captures from {firstYear}{firstYear !== lastYear ? `–${lastYear}` : ''}</p>}
                <span className="site-card__action">Search this site <span aria-hidden="true">→</span></span>
              </a>
            </li>;
          })}
        </ul>
        {sites.length > 0 && <p className="sites-note">Sites appear as records become searchable; indexing may still be in progress. Records include dated versions of the same page. Shared hosting domains may contain more than one site.</p>}
      </main>
    </div>
  </div>;
}
