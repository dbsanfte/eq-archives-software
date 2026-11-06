import React, { useContext, useId, useState } from 'react';
import { Button } from '@mui/material';
import { excerptText, snippetHtml } from '../../search/DiscussionIdentity';
import { readerUrl, recordId, ReaderSearchContext, searchReturnLinkHandlers } from '../reader/reader-utils';
import ResultBody from './ResultBody';

export default function DiscussionResult({ result, returnTo }) {
  const group = result._discussionGroup;
  const find = useContext(ReaderSearchContext);
  const [open, setOpen] = useState(false);
  const [visible, setVisible] = useState(3);
  const region = useId();
  return <li className="sui-result archive-discussion-result">
    <div className="sui-result__header"><h2 className="archive-result-heading">
      <a className="sui-result__title" href={readerUrl(recordId(result), { find, returnTo })} {...searchReturnLinkHandlers}>{group.title}</a>
    </h2></div>
    <p className="archive-discussion-label">Forum discussion · {result.domain_name?.raw || group.forum}</p>
    <ResultBody summaryHTML="" textHTML={snippetHtml(result)} />
    <div className="archive-result-actions">
      <Button component="a" variant="contained" className="archive-result-reader" href={readerUrl(recordId(result), { find, returnTo })} {...searchReturnLinkHandlers}>Read discussion</Button>
      <Button aria-expanded={open} aria-controls={region} onClick={() => setOpen(value => !value)}>{open ? 'Hide matches' : 'Show matches'}</Button>
    </div>
    {open && <section id={region} className="archive-discussion-matches" aria-label="Matching excerpts">
      <p className="archive-capture-note">Distinct excerpts found in the searched captures. Each link opens its exact archived page.</p>
      <ol>{group.matches.slice(0, visible).map(match => <li key={recordId(match)}>
        <p className="archive-discussion-excerpt">{excerptText(match) || 'Open this captured page to read the matching content.'}</p>
        <a href={readerUrl(recordId(match), { find: excerptText(match).slice(0, 240) || find, returnTo })} {...searchReturnLinkHandlers}>Read from here<span className="archive-discussion-date"> · captured {match.capture_date?.raw?.slice(0, 10) || 'date unknown'}</span></a>
      </li>)}</ol>
      {visible < group.matches.length && <Button onClick={() => setVisible(value => value + 3)}>Show more matches</Button>}
    </section>}
  </li>;
}
