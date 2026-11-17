import React from 'react';
import { LinearProgress } from '@mui/material';

const messages = {
  waiting: 'Waiting for typing to finish…',
  preparing: 'Preparing semantic search…',
  searching: 'Searching the archive…',
  reranking: 'Reranking matches…'
};

export default function SearchProgress({ phase, isLoading, hasResults }) {
  if (!isLoading) return null;
  return <div className="archive-search-progress" role="status" aria-label="Search status" aria-live="polite" aria-atomic="true">
    <span>{messages[phase] || messages.searching}</span>
    {hasResults && <small>Showing previous results while updating.</small>}
    <LinearProgress aria-label="Search progress" />
  </div>;
}
