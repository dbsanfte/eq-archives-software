import React from 'react';
import { act, render, screen } from '@testing-library/react';
import { createSearchActivity, SearchActivityContext, useSearchActivity } from './SearchActivity';

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const response = (name = 'original') => ({ results: [{ title: name }], totalResults: 1 });
const state = { searchTerm: 'cleric', current: 1, resultsPerPage: 20 };

test('subscriptions keep stable snapshots and preparation belongs to the current draft', () => {
  const activity = createSearchActivity(), changed = jest.fn();
  const initial = activity.getSnapshot();
  const remove = activity.subscribe(changed);
  expect(activity.getSnapshot()).toBe(initial);
  activity.preparing('other');
  expect(changed).not.toHaveBeenCalled();
  activity.draft('cleric');
  activity.preparing('cleric');
  expect(activity.getSnapshot().phase).toBe('preparing');
  expect(changed).toHaveBeenCalledTimes(2);
  remove();
  activity.draft('wizard');
  expect(changed).toHaveBeenCalledTimes(2);
});

test('only changed stages notify, and completed pages retain their counts and query', async () => {
  const activity = createSearchActivity(), changed = jest.fn();
  activity.subscribe(changed);
  await activity.search(state, async request => {
    request._archiveProgress('searching');
    request._archiveProgress('reranking');
    expect(activity.getSnapshot().phase).toBe('reranking');
    return response();
  });
  expect(changed).toHaveBeenCalledTimes(3);
  expect(activity.getSnapshot()).toMatchObject({ phase: 'idle', display: {
    ...response(), resultSearchTerm: 'cleric', current: 1, totalPages: 1,
    wasSearched: true, pagingStart: 1, pagingEnd: 1
  } });
});

test.each(['empty', 'error'])('obsolete %s responses cannot clear results or end the active search', async mode => {
  const activity = createSearchActivity();
  await activity.search(state, async () => response());
  const old = deferred(), next = deferred();
  let oldProgress;
  const older = activity.search({ ...state, searchTerm: 'old' }, request => {
    oldProgress = request._archiveProgress;
    return old.promise;
  });
  const newest = activity.search({ ...state, searchTerm: 'newest' }, () => next.promise);
  oldProgress('reranking');
  expect(activity.getSnapshot().phase).toBe('searching');
  if (mode === 'empty') old.resolve({ results: [], totalResults: 0 });
  else old.reject(new Error('old failure'));
  await older;
  expect(activity.getSnapshot()).toMatchObject({ phase: 'searching', display: response() });
  next.resolve(response('newest'));
  await newest;
  expect(activity.getSnapshot()).toMatchObject({ phase: 'idle', display: response('newest') });
});

test('a new draft invalidates old work even when the text returns to the same query', async () => {
  const activity = createSearchActivity(), old = deferred();
  const older = activity.search(state, () => old.promise);
  activity.draft('wizard');
  activity.draft('cleric');
  old.resolve(response('obsolete'));
  await older;
  expect(activity.getSnapshot()).toMatchObject({ phase: 'waiting', display: null });
  await activity.search(state, async () => response('current'));
  expect(activity.getSnapshot().display).toMatchObject(response('current'));
});

test('current failures retain previous results and a subsequent request can recover', async () => {
  const activity = createSearchActivity();
  await activity.search(state, async () => response());
  await expect(activity.search(state, async () => { throw new Error('busy'); })).rejects.toThrow('busy');
  expect(activity.getSnapshot()).toMatchObject({ phase: 'error', display: response() });
  await activity.search(state, async () => response('recovered'));
  expect(activity.getSnapshot()).toMatchObject({ phase: 'idle', display: response('recovered') });
});

test('completed empty and explicitly paged results keep accurate visible paging', async () => {
  const activity = createSearchActivity();
  await activity.search({ searchTerm: '' }, async () => ({ results: [], totalResults: 0 }));
  expect(activity.getSnapshot().display).toMatchObject({ pagingStart: 0, pagingEnd: 0, totalPages: 1, current: 1 });
  await activity.search({ ...state, current: 2 }, async () => ({ ...response(), totalResults: 50 }));
  expect(activity.getSnapshot().display).toMatchObject({ pagingStart: 21, pagingEnd: 21, totalPages: 3 });
  await activity.search(state, async () => ({ ...response(), current: 3, pagingStart: 99, pagingEnd: 100, totalPages: 4 }));
  expect(activity.getSnapshot().display).toMatchObject({ current: 3, pagingStart: 99, pagingEnd: 100, totalPages: 4 });
});

test('the hook subscribes to its provider and also works without one', () => {
  function Probe() { return <span>{useSearchActivity().phase}</span>; }
  const activity = createSearchActivity();
  const view = render(<SearchActivityContext.Provider value={activity}><Probe /></SearchActivityContext.Provider>);
  act(() => activity.draft('cleric'));
  expect(screen.getByText('waiting')).toBeInTheDocument();
  view.rerender(<Probe />);
  expect(screen.getByText('idle')).toBeInTheDocument();
});
