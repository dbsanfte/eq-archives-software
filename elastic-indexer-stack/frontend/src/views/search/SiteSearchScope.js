import React from 'react';
import '../sites/Sites.css';

export function scopedDomains(filters = []) {
  return filters.filter(filter => filter.field === 'domain_name' && filter.type !== 'none')
    .flatMap(filter => filter.values).filter(value => typeof value === 'string');
}

export function websiteScope(filters = []) {
  return filters.some(filter => filter.field === 'id' && filter.type === 'range' &&
    filter.values?.some(value => value.from === 'websites/' && value.to === 'websites0'));
}

export default function SiteSearchScope({ filters, removeFilter }) {
  const domains = scopedDomains(filters);
  const websites = websiteScope(filters);
  if (!domains.length && !websites) return null;
  return <section className="site-search-scope" aria-label="Site search scope">
    <div><span>Searching within{websites ? ' website captures' : ''}</span><h1>{domains.length ? domains.join(', ') : 'Archived websites'}</h1></div>
    {domains.length > 0 && <button onClick={() => removeFilter('domain_name')}>Search all sites</button>}
    {websites && <button onClick={() => removeFilter('id')}>Include other collections</button>}
  </section>;
}
