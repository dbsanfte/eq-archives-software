import React, { useState } from 'react';
import siteIcons from './site-icons.json';

function siteKey(domain) {
  if (typeof domain !== 'string') return '';
  const host = domain.trim().toLowerCase().replace(/\.$/, '').replace(/:(80|443)$/, '').replace(/^www\./, '');
  return /^(?:(?:server|pub|p)\d+\.)?ezboard\.com$/.test(host) ? 'ezboard.com' : host;
}

function SiteIcon({ asset, initials }) {
  const [failed, setFailed] = useState(false);
  return <span className="archive-result-thumbnail archive-result-site-icon" aria-hidden="true">
    {asset && !failed
      ? <img src={`/static/site-icons/${asset}`} width="52" height="52" alt="" loading="lazy"
        decoding="async" onError={() => setFailed(true)} />
      : <span>{initials}</span>}
  </span>;
}

export default function ResultHeader({ domain, thumbnail, children }) {
  const key = siteKey(domain);
  const site = Object.prototype.hasOwnProperty.call(siteIcons, key) ? siteIcons[key] : undefined;
  return <div className="sui-result__header">
    <h2 className="archive-result-heading">{children}</h2>
    {site
      ? <SiteIcon key={key} asset={site.logo} initials={site.monogram} />
      : thumbnail && <img className="archive-result-thumbnail" src={thumbnail} alt="" loading="lazy" />}
  </div>;
}
