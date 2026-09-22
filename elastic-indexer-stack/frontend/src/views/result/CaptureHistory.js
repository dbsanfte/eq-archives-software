import React, { useContext, useEffect, useId, useState } from 'react';
import { Button } from '@mui/material';
import { fetchCaptures } from '../../search/CaptureService';
import { comparisonUrl, readerUrl, recordId, ReaderSearchContext } from '../reader/reader-utils';
import DocumentPreview from './DocumentPreview';

export function captureDate(result) {
  const date = result.capture_date?.raw;
  return typeof date === 'string' && /^\d{4}-\d{2}-\d{2}/.test(date) ? date.slice(0, 10) : 'Date unknown';
}

export default function CaptureHistory({ identity, compareWith }) {
  const find = useContext(ReaderSearchContext);
  const regionId = useId();
  const [open, setOpen] = useState(false);
  const [records, setRecords] = useState([]);
  const [page, setPage] = useState(null);
  const [offset, setOffset] = useState(0);
  const [failure, setFailure] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [preview, setPreview] = useState(null);
  const loadedOffset = page?.offset;

  useEffect(() => {
    if (!open || loadedOffset === offset) return;
    const controller = new AbortController();
    setFailure(false);
    fetchCaptures(identity, offset, controller.signal).then(data => {
      if (controller.signal.aborted) return;
      setRecords(previous => Array.from(new Map([...previous, ...data.records].map(result => [result._meta.id, result])).values()));
      setPage({ ...data, offset });
    }).catch(() => { if (!controller.signal.aborted) setFailure(true); });
    return () => controller.abort();
  }, [open, identity, offset, loadedOffset, attempt]);

  const loading = loadedOffset !== offset && !failure;
  return (
    <section className="archive-captures">
      <Button aria-expanded={open} aria-controls={regionId} onClick={() => setOpen(value => !value)}>
        {open ? 'Hide captures' : 'View captures'}{page && ` (${records.length}${page.nextOffset !== null || page.limited ? '+' : ''})`}
      </Button>
      {open && <div id={regionId} className="archive-capture-history">
        <h3>Captures of this page</h3>
        <p className="archive-original-url">{identity.originalUrl}</p>
        <p className="archive-capture-note">All archived versions, including captures outside your current search filters. Newest first.</p>
        <ol className="archive-capture-list">
          {records.map(result => <li key={result._meta.id}>
            <div><span className="archive-capture-date">{captureDate(result)}</span><span className="archive-capture-title">{result.title?.raw || 'Untitled capture'}</span></div>
            <div className="archive-capture-actions">
              <a href={readerUrl(recordId(result), { find })} aria-label={`Read capture from ${captureDate(result)}`}>Read</a>
              {recordId(compareWith) && recordId(result) !== recordId(compareWith) && <a href={comparisonUrl(compareWith, result)} aria-label={`Compare capture from ${captureDate(result)} with selected capture`}>Compare</a>}
              {/^(https?):\/\//i.test(result.url?.raw) && <a href={result.url.raw} target="_blank" rel="noopener noreferrer" aria-label={`Open capture from ${captureDate(result)}`}>Open archive</a>}
              <Button onClick={() => setPreview(result)} aria-label={`Preview capture from ${captureDate(result)}`}>Preview</Button>
            </div>
          </li>)}
        </ol>
        {loading && <p role="status">Loading captures…</p>}
        {failure && <div role="alert"><p>Could not load captures. Please try again.</p><Button onClick={() => { setFailure(false); setAttempt(value => value + 1); }}>Try again</Button></div>}
        {!loading && !failure && page && records.length === 0 && <p>No other capture records are available.</p>}
        {!loading && !failure && page?.nextOffset != null && <Button onClick={() => setOffset(page.nextOffset)}>Load more captures</Button>}
        {page?.limited && <p className="archive-capture-note">Showing captures from the first 1,000 records for this page.</p>}
      </div>}
      {preview && <DocumentPreview open onClose={() => setPreview(null)} result={preview} title={`Capture · ${captureDate(preview)}`} />}
    </section>
  );
}
