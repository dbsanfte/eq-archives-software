import React, { useState } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import ReaderText, { MAX_FORMATTED_LENGTH } from './ReaderText';

const base = 'https://web.archive.org/web/20000101000000/http://example.org/a/page';
function Reader({ text, initial = '' }) { const [find, setFind] = useState(initial); return <ReaderText text={text} base={base} find={find} onFind={setFind} />; }
beforeEach(() => { Element.prototype.scrollIntoView = jest.fn(); });

test('reads formatted source and highlights literal matches without losing rapid input', async () => {
  const text = '# A guide\n\nAncient cyclops. Ancient cyclops.\n\n`[a+b]`';
  const { container } = render(<Reader text={text} initial="ancient cyclops" />);
  expect(screen.getByRole('heading', { name: 'A guide' })).toBeVisible();
  expect(screen.getByRole('status')).toHaveTextContent('1 of 2 matches');
  expect(container.querySelectorAll('mark')).toHaveLength(2);
  fireEvent.click(screen.getByRole('button', { name: 'Next match' }));
  expect(screen.getByRole('status')).toHaveTextContent('2 of 2');
  expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
  fireEvent.keyDown(screen.getByRole('searchbox'), { key: 'Enter' });
  expect(screen.getByRole('status')).toHaveTextContent('1 of 2');
  fireEvent.keyDown(screen.getByRole('searchbox'), { key: 'Enter', shiftKey: true });
  expect(screen.getByRole('status')).toHaveTextContent('2 of 2');
  fireEvent.click(screen.getByRole('button', { name: 'Previous match' }));
  expect(screen.getByRole('status')).toHaveTextContent('1 of 2');
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'nope' } });
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: '[a+b]' } });
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('1 of 1'));
  expect(screen.getByRole('searchbox')).toHaveValue('[a+b]');
  expect(container.querySelector('mark')).toHaveTextContent('[a+b]');
  fireEvent.click(screen.getByRole('checkbox', { name: 'Source text' }));
  expect(container.querySelector('pre')).toHaveTextContent('# A guide');
  expect(container.querySelector('pre').textContent).toBe(text);
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'missing' } });
  fireEvent.keyDown(screen.getByRole('searchbox'), { key: 'Enter' });
  fireEvent.keyDown(screen.getByRole('searchbox'), { key: 'Escape' });
  expect(screen.getByRole('status')).toHaveTextContent('No matches');
  expect(screen.getByRole('button', { name: 'Next match' })).toBeDisabled();
});

test('preserves safe links and replaces remote images with deliberate source links', () => {
  const { container } = render(<Reader text={'[Relative](../spell) [Bad](javascript:alert)\n\n![A picture](image.png) ![](other.png)\n\n<img src=x onerror="alert(1)">'} />);
  expect(screen.getByRole('link', { name: 'Relative' })).toHaveAttribute('href', 'https://web.archive.org/web/20000101000000/http://example.org/spell');
  expect(screen.getByRole('link', { name: 'Relative' })).toHaveAttribute('rel', 'noopener noreferrer');
  expect(screen.queryByRole('link', { name: 'Bad' })).not.toBeInTheDocument();
  expect(screen.getByRole('link', { name: '[Image: A picture]' })).toBeInTheDocument();
  expect(screen.getByRole('link', { name: '[Image: view original]' })).toBeInTheDocument();
  expect(container.querySelector('img')).toBeNull();
  expect(screen.getByRole('status')).toHaveTextContent('Search within the source text');
});

test('keeps the complete source when formatting or highlighting would be excessive', () => {
  const text = 'word '.repeat(MAX_FORMATTED_LENGTH / 5 + 1) + 'FINAL SOURCE LINE';
  const { container } = render(<Reader text={text} initial="word" />);
  expect(screen.getByText(/shown as complete source text/)).toBeVisible();
  expect(screen.getByText(/Highlighting the first 1,000/)).toBeVisible();
  expect(screen.getByRole('status')).toHaveTextContent('1 of 1000+ matches');
  expect(container.querySelectorAll('mark')).toHaveLength(1000);
  expect(container.querySelector('pre').textContent).toBe(text);
});
