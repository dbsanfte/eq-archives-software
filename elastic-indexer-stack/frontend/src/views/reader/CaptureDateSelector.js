import React, { useEffect, useState } from 'react';
import { Button } from '@mui/material';
import { fetchCaptures } from '../../search/CaptureService';
import { readerUrl, recordId } from './reader-utils';

const dateLabel = record => {
  const value = record.capture_date?.raw;
  return typeof value === 'string' && /^\d{4}-\d\d-\d\d/.test(value)
    ? value.replace('T', ' ').replace(/(?:\.\d+)?(?:Z|\+00:00)$/, '') + ' UTC' : 'Date unknown';
};
const navigateTo = url => window.location.assign(url);

export default function CaptureDateSelector({ record, identity, find, part, returnTo, navigate = navigateTo }) {
  const [records, setRecords] = useState([record]);
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const [failure, setFailure] = useState(false);
  const loadedOffset = page?.offset;
  useEffect(() => {
    if (loadedOffset === offset) return undefined;
    const controller = new AbortController();
    setFailure(false);
    fetchCaptures(identity, offset, controller.signal).then(data => {
      if (controller.signal.aborted) return;
      setRecords(previous => Array.from(new Map([...previous, ...data.records].map(item => [recordId(item), item])).values()));
      setPage({ ...data, offset });
    }).catch(() => { if (!controller.signal.aborted) setFailure(true); });
    return () => controller.abort();
  }, [identity, offset, loadedOffset, attempt]);
  const loading = loadedOffset !== offset && !failure;
  return <section className="reader-capture-selector" aria-label="Capture date selection">
    <label>Captured <select aria-label="Capture date" disabled={loading && records.length === 1} value={recordId(record)} onChange={event => {
      if (event.target.value !== recordId(record)) navigate(readerUrl(event.target.value, { find, part, returnTo }));
    }}>{records.map(item => <option key={recordId(item)} value={recordId(item)}>{dateLabel(item)}</option>)}</select></label>
    <p className="reader-note">{page && records.length === 1 && page.nextOffset === null && !page.limited ? 'Only one known capture of this page.' : 'All available dates for this page, including outside your search filters.'}</p>
    {loading && <p role="status">Loading capture dates…</p>}
    {failure && <div role="alert">Could not load other capture dates. <Button onClick={() => setAttempt(value => value + 1)}>Try again</Button></div>}
    {!loading && !failure && page?.nextOffset != null && <Button onClick={() => setOffset(page.nextOffset)}>Load more capture dates</Button>}
    {page?.limited && <p className="reader-note">Showing the first 1,000 capture records for this page.</p>}
  </section>;
}
