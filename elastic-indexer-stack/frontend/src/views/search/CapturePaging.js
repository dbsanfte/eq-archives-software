import React from 'react';
import { Button } from '@mui/material';

export function CapturePaging({ current, totalPages, onChange, isLoading }) {
  return <nav className="archive-group-paging" aria-label="Grouped results pages">
    <Button variant="outlined" disabled={isLoading || current <= 1} onClick={() => onChange(current - 1)}>Previous</Button>
    <span>Page {current}</span>
    <Button variant="outlined" disabled={isLoading || current >= totalPages} onClick={() => onChange(current + 1)}>Next</Button>
  </nav>;
}

export function CaptureSummary({ totalResults, pagingStart, pagingEnd, rawResponse }) {
  return <div className="archive-group-summary" aria-live="polite">
    <span>{pagingEnd > 0 ? `Showing sources ${pagingStart}–${pagingEnd}` : 'No sources found'} · {totalResults >= 10000 ? '10,000+' : totalResults?.toLocaleString()} matching captures</span>
    {totalResults > 1000 && <small>{rawResponse?.captureGroups?.windowReached ? 'Reached the browsing limit. ' : ''}Groups cover the first 1,000 matching captures. Refine your search to explore more.</small>}
  </div>;
}
