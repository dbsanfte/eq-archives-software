import React, { useMemo, useState } from 'react';
import { Button } from '@mui/material';
import ArchiveStatusBar from '../ArchiveStatusBar';
import HeaderContent from '../HeaderContent';
import CaptureHistory from '../result/CaptureHistory';
import { captureIdentity } from '../../search/CaptureIdentity';
import { citation, downloadText, readerUrl, recordId, sourceLink, value } from './reader-utils';
import useDocument from './useDocument';
import ReaderText from './ReaderText';
import Comparison from './Comparison';
import './Reader.css';

export function CopyAction({ label, text }) {
  const [status, setStatus] = useState('');
  async function copy() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(text);
      setStatus('Copied.');
    } catch (_) { setStatus('Select and copy the text below.'); }
  }
  return <div className="reader-copy"><Button onClick={copy}>{label}</Button>
    {status && <span role="status">{status}</span>}
    {status.startsWith('Select') && <textarea aria-label={`${label} manually`} readOnly value={text} onFocus={event => event.target.select()} />}
  </div>;
}

function LoadState({ state, label }) {
  return state.error ? <div role="alert"><p>{label}: {state.error}</p><Button onClick={state.retry}>Try again</Button></div>
    : <p role="status">Loading {label.toLowerCase()}…</p>;
}

function DocumentMeta({ record, heading }) {
  const url = sourceLink(value(record, 'url'));
  const alternate = sourceLink(value(record, 'alternate_url'));
  const identity = captureIdentity(record);
  return <section className="reader-metadata" aria-label={heading || 'Source details'}>
    {heading && <h2>{heading}</h2>}
    {heading && <h3>{value(record, 'title') || 'Untitled document'}</h3>}
    <dl>
      <div><dt>Captured</dt><dd>{value(record, 'capture_date') || 'Date unknown'}</dd></div>
      {value(record, 'llm_guessed_date') && <div><dt>Estimated publication</dt><dd>{value(record, 'llm_guessed_date')} <span className="reader-note">(AI estimate; verify against the source)</span></dd></div>}
      {(value(record, 'domain_name') || value(record, 'mailing_list_name')) && <div><dt>Collection</dt><dd>{value(record, 'domain_name') || value(record, 'mailing_list_name')}</dd></div>}
      <div><dt>Source</dt><dd>{identity?.originalUrl || url || recordId(record)}</dd></div>
    </dl>
    <p className="reader-note">Capture dates record when content was archived, not necessarily when it was published.</p>
    <div className="reader-actions">
      {heading && <Button component="a" href={readerUrl(recordId(record))}>Read this capture</Button>}
      {url && <a href={url} target="_blank" rel="noopener noreferrer">Open original archive ↗</a>}
      {alternate && alternate !== url && <a href={alternate} target="_blank" rel="noopener noreferrer">Alternate source ↗</a>}
    </div>
  </section>;
}

function TextKind({ part, setPart }) {
  return <label className="reader-text-kind">Text to view <select value={part} onChange={event => setPart(event.target.value)}>
    <option value="text">Extracted text</option><option value="ocr">Image transcription (OCR)</option>
  </select></label>;
}

export default function DocumentReader({ search = window.location.search }) {
  const params = new URLSearchParams(search);
  const id = params.get('id') || '';
  const compare = params.get('compare') || '';
  const [find, setFind] = useState(params.get('find') || '');
  const [chosenPart, setPart] = useState(params.get('part') === 'ocr' ? 'ocr' : 'auto');
  const first = useDocument(id);
  const second = useDocument(compare && compare !== id ? compare : '');
  const record = first.record;
  const other = second.record;
  const identity = useMemo(() => record && captureIdentity(record), [record]);
  const otherIdentity = other && captureIdentity(other);
  const comparable = identity && identity.key === otherIdentity?.key;
  const part = chosenPart === 'auto' ? (!value(record, 'text_full') && value(record, 'llm_image_text_full') && (!compare || (!value(other, 'text_full') && value(other, 'llm_image_text_full'))) ? 'ocr' : 'text') : chosenPart;
  const field = part === 'ocr' ? 'llm_image_text_full' : 'text_full';
  const text = value(record, field);
  const otherText = value(other, field);
  const title = value(record, 'title') || 'Untitled document';
  const label = part === 'ocr' ? 'image transcription (OCR)' : 'extracted text';
  const link = window.location.origin + readerUrl(id, { compare, find, part: part === 'ocr' ? 'ocr' : '' });
  return <div className="archive-reader">
    <div className="reader-shell"><ArchiveStatusBar /><HeaderContent compact />
      <nav className="reader-breadcrumb" aria-label="Reader navigation"><a href="/">← Search the archive</a>{compare && <a href={readerUrl(id, { find })}>Back to document</a>}{record && <a href="#reader-content">{compare ? "Jump to changes" : "Jump to text"}</a>}</nav>
      <main id="reader-main">
        <p className="archive-eyebrow">{compare ? 'Across the years' : 'From the archive'}</p>
        <h1>{compare ? 'Compare captures' : record ? title : 'Document reader'}</h1>
        {!id ? <p role="alert">This link is missing a document ID. Open a document from the search results.</p>
          : !record ? <LoadState state={first} label="Document" /> : <>
            <div className={compare ? 'reader-capture-pair' : ''}>
              <DocumentMeta record={record} heading={compare ? 'From' : undefined} />
              {compare && (compare === id ? <p role="alert">Choose two different captures to compare.</p>
                : other ? <DocumentMeta record={other} heading="To" /> : <LoadState state={second} label="Comparison capture" />)}
            </div>
            <div className="reader-actions reader-toolbar">
              <CopyAction label="Copy link" text={link} />
              {!compare && <CopyAction label="Copy citation" text={citation(record, window.location.origin + readerUrl(id))} />}
              {compare && <Button component="a" href={readerUrl(compare, { compare: id, find, part })}>Swap captures</Button>}
              {text && <Button onClick={() => downloadText(text, `${title}${compare ? '-from' : ''}${part === 'ocr' ? '-ocr' : ''}`)}>Download {compare ? 'from text' : 'text'}</Button>}
              {compare && otherText && <Button onClick={() => downloadText(otherText, `${value(other, 'title')}-to${part === 'ocr' ? '-ocr' : ''}`)}>Download to text</Button>}
            </div>
            <div id="reader-content">
            {(!compare || (other && compare !== id && comparable)) && <>
              <TextKind part={part} setPart={setPart} />
              {part === 'ocr' && <p className="reader-note reader-transcription-note">Image transcription (OCR) is model-generated and may contain errors. Check the original images when citing it.</p>}
              {compare ? (!text.trim() || !otherText.trim() ? <p role="status">{!text.trim() && !otherText.trim() ? 'Neither capture has' : !text.trim() ? 'The From capture has' : 'The To capture has'} available {label}. Missing text cannot establish an addition or deletion.</p>
                : <Comparison before={text} after={otherText} label={label} />)
                : !text.trim() ? <p role="status">No {label} is available for this record. Try the original archive or another text type.</p>
                  : <ReaderText text={text} base={value(record, 'url')} find={find} onFind={setFind} />}
            </>}
            {compare && other && !comparable && <p role="alert">These records are not captures of the same original page. Choose a version from this document’s capture history.</p>}
            </div>
            {identity && <CaptureHistory key={identity.key} identity={identity} compareWith={record} />}
          </>}
      </main>
      <footer className="reader-footer">Preserved source text from the EverQuest community. Formatting may differ from the original page.</footer>
    </div>
  </div>;
}
