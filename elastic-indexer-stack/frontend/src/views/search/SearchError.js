import React from 'react';
import { BUSY_MESSAGE, TIMEOUT_MESSAGE, INVALID_MESSAGE, UNAVAILABLE_MESSAGE } from '../../search/ArchiveFetch';

export default function SearchError({ error, children, onRetry }) {
  if (!error) return children;
  const message = typeof error === 'string' ? error : error.message;
  // Search UI prefixes connector errors. Select only our fixed safe text,
  // never the prefix or any arbitrary upstream diagnostic surrounding it.
  const safe = [BUSY_MESSAGE, TIMEOUT_MESSAGE, INVALID_MESSAGE]
    .find(value => typeof message === 'string' && message.includes(value)) || UNAVAILABLE_MESSAGE;
  return <div className="sui-search-error archive-search-error" role="alert">
    <p>{safe}</p>
    <button type="button" onClick={onRetry}>Try again</button>
  </div>;
}
