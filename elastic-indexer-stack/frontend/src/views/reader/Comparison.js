import React, { useEffect, useRef, useState } from 'react';
import { Button } from '@mui/material';
import { diffLines } from 'diff';

export const MAX_DIFF_LENGTH = 1000000;
export const MAX_DIFF_LINES = 20000;
export const DIFF_LIMIT_MESSAGE = 'These captures are too large or too different for an interactive comparison. Open or download each complete document to compare them separately.';

export function compareText(before, after, callback) {
  if (before.length + after.length > MAX_DIFF_LENGTH || before.split('\n').length + after.split('\n').length > MAX_DIFF_LINES) {
    callback(undefined);
    return;
  }
  // Async mode yields between iterations; bound pathological edits on mobile.
  diffLines(before, after, { timeout: 1000, maxEditLength: 2000, callback });
}

function Context({ text }) {
  const lines = text.match(/[^\n]*\n|[^\n]+$/g) || [];
  if (lines.length <= 8) return <pre>{text}</pre>;
  return <><pre>{lines.slice(0, 3).join('')}</pre><details><summary>{lines.length - 6} unchanged lines</summary><pre>{lines.slice(3, -3).join('')}</pre></details><pre>{lines.slice(-3).join('')}</pre></>;
}

export default function Comparison({ before, after, label }) {
  const [state, setState] = useState(null);
  const [active, setActive] = useState(-1);
  const region = useRef();
  useEffect(() => {
    let current = true;
    setState(null);
    setActive(-1);
    compareText(before, after, changes => {
      if (current) setState({ before, after, changes });
    });
    return () => { current = false; };
  }, [before, after]);
  if (!state || state.before !== before || state.after !== after) return <p role="status">Comparing source text…</p>;
  if (!state.changes) return <p role="status">{DIFF_LIMIT_MESSAGE}</p>;
  const changes = state.changes;
  const added = changes.reduce((sum, part) => sum + (part.added ? part.count : 0), 0);
  const removed = changes.reduce((sum, part) => sum + (part.removed ? part.count : 0), 0);
  if (!added && !removed) return <p role="status">The {label} is identical in these two captures.</p>;
  const move = step => {
    const nodes = Array.from(region.current.querySelectorAll('[data-change]'));
    const next = active === -1 ? (step > 0 ? 0 : nodes.length - 1) : (active + step + nodes.length) % nodes.length;
    setActive(next);
    nodes[next].focus({ preventScroll: true });
    nodes[next].scrollIntoView({ block: 'center', behavior: 'smooth' });
  };
  return <section className="reader-comparison" aria-label="Text changes" ref={region}>
    <div className="reader-diff-tools"><p role="status">{added} added · {removed} removed lines</p>
      <div><Button onClick={() => move(-1)}>Previous change</Button><Button onClick={() => move(1)}>Next change</Button></div>
    </div>
    <p className="reader-note">Comparing preserved text, including whitespace and line endings. Changes may include navigation or extraction differences.</p>
    {changes.map((part, i) => <div key={i} className={`reader-diff-part ${part.added ? 'reader-added' : part.removed ? 'reader-removed' : 'reader-unchanged'}`} data-change={part.added || part.removed ? true : undefined} tabIndex={part.added || part.removed ? -1 : undefined}>
      {part.added || part.removed ? <><span className="reader-diff-label">{part.added ? '+ Added' : '− Removed'}</span><pre>{part.value}</pre>{!part.value.endsWith('\n') && <span className="reader-note">No newline at end of text</span>}</> : <Context text={part.value} />}
    </div>)}
  </section>;
}
