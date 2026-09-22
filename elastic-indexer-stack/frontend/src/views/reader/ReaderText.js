import React, { useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Button } from '@mui/material';
import { highlightMarkdown, highlightParts, MAX_MARKS, sourceLink } from './reader-utils';

export const MAX_FORMATTED_LENGTH = 500000;

export default function ReaderText({ text, base, find, onFind }) {
  const [source, setSource] = useState(false);
  const [matches, setMatches] = useState([]);
  const [active, setActive] = useState(0);
  const body = useRef();
  const plain = source || text.length > MAX_FORMATTED_LENGTH;
  const plugins = useMemo(() => [[highlightMarkdown, { term: find }]], [find]);
  useEffect(() => {
    setMatches(Array.from(body.current.querySelectorAll('[data-reader-match]')));
    setActive(0);
  }, [text, find, plain]);
  useEffect(() => {
    matches.forEach((mark, i) => mark.classList.toggle('reader-match-active', i === active));
  }, [matches, active]);
  const move = step => {
    if (!matches.length) return;
    const next = (active + step + matches.length) % matches.length;
    setActive(next);
    matches[next].scrollIntoView({ block: 'center', behavior: 'smooth' });
  };
  const components = useMemo(() => {
    const link = ({ href, children }) => {
      const safe = sourceLink(href, base);
      return safe ? <a href={safe} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>;
    };
    return { a: link, img: ({ src, alt }) => link({ href: src, children: `[Image: ${alt || 'view original'}]` }) };
  }, [base]);
  return <section className="reader-text-section" aria-label="Document text">
    <div className="reader-find">
      <label htmlFor="reader-find">Find in document</label>
      <input id="reader-find" type="search" value={find} placeholder="Exact word or phrase" onChange={event => onFind(event.target.value)} onKeyDown={event => {
        if (event.key === 'Enter') { event.preventDefault(); move(event.shiftKey ? -1 : 1); }
      }} />
      <div className="reader-find-controls">
        <span role="status">{find ? (matches.length ? `${active + 1} of ${matches.length}${matches.length === MAX_MARKS ? '+' : ''} matches` : 'No matches') : 'Search within the source text'}</span>
        <Button disabled={!matches.length} onClick={() => move(-1)} aria-label="Previous match">↑</Button>
        <Button disabled={!matches.length} onClick={() => move(1)} aria-label="Next match">↓</Button>
      </div>
      <label className="reader-source-toggle"><input type="checkbox" checked={source} onChange={event => setSource(event.target.checked)} /> Source text</label>
    </div>
    {text.length > MAX_FORMATTED_LENGTH && <p className="reader-note">This long document is shown as complete source text to keep reading responsive.</p>}
    {matches.length === MAX_MARKS && <p className="reader-note">Highlighting the first 1,000 matches. All document text remains available.</p>}
    <div ref={body} className="reader-body">
      {plain ? <pre>{highlightParts(text, find).map((part, i) => part.match ? <mark key={i} data-reader-match>{part.text}</mark> : part.text)}</pre>
        : <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={plugins} components={components}>{text}</ReactMarkdown>}
    </div>
  </section>;
}
