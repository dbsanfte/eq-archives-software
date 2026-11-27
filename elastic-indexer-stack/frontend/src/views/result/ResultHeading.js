import React, { useState } from 'react';
import siteIcons from './site-icons.json';

function siteKey(domain) {
  if (typeof domain !== 'string') return '';
  const host = domain.trim().toLowerCase().replace(/\.$/, '').replace(/:(80|443)$/, '').replace(/^www\./, '');
  return /^(?:(?:server|pub|p)\d+\.)?ezboard\.com$/.test(host) ? 'ezboard.com' : host;
}

function SiteIcon({ asset, initials }) {
  const [failed, setFailed] = useState(false);
  return <span className="archive-result-site-icon" aria-hidden="true">
    {asset && !failed
      ? <img src={`/static/site-icons/${asset}`} width="48" height="36" alt="" loading="lazy"
        decoding="async" onError={() => setFailed(true)} />
      : <span>{initials}</span>}
  </span>;
}

export default function ResultHeading({ domain, children }) {
  const key = siteKey(domain);
  const site = Object.prototype.hasOwnProperty.call(siteIcons, key) ? siteIcons[key] : undefined;
  return <h2 className="archive-result-heading">
    {site && <SiteIcon key={key} asset={site.logo} initials={site.monogram} />}
    {children}
  </h2>;
}
