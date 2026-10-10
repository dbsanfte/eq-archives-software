import React from 'react';
import '../sites/Sites.css';

export function scopedDomains(filters = []) {
  return filters.filter(filter => filter.field === 'domain_name' && filter.type !== 'none')
    .flatMap(filter => filter.values).filter(value => typeof value === 'string');
}

export default function SiteSearchScope({ filters, removeFilter }) {
  const domains = scopedDomains(filters);
  if (!domains.length) return null;
  return <section className="site-search-scope" aria-label="Site search scope">
    <div><span>Searching within</span><h1>{domains.join(', ')}</h1></div>
    <button onClick={() => removeFilter('domain_name')}>Search all sites</button>
  </section>;
}
