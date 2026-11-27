import React from 'react';
import fs from 'fs';
import path from 'path';
import { fireEvent, render, screen } from '@testing-library/react';
import ResultHeading from './ResultHeading';
import siteIcons from './site-icons.json';

it('ships one entry for each ranked site and a local fingerprinted asset for every archived logo', () => {
  expect(Object.keys(siteIcons)).toHaveLength(100);
  for (const { logo, monogram } of Object.values(siteIcons)) {
    expect(monogram).toMatch(/^[A-Z0-9]{2,3}$/);
    if (!logo) continue;
    expect(logo).toMatch(/^[a-z0-9.-]+\.[a-f0-9]{12}\.webp$/);
    expect(fs.existsSync(path.join(process.cwd(), 'public/static/site-icons', logo))).toBe(true);
  }
});

it('uses the same decorative icon for equivalent site addresses without changing the title', () => {
  const { rerender } = render(<ResultHeading domain="WWW.EQATLAS.COM:80"><a href="/read/123">Norrath maps</a></ResultHeading>);
  const heading = screen.getByRole('heading', { name: 'Norrath maps' });
  expect(heading.querySelector('img')).toHaveAttribute('src', `/static/site-icons/${siteIcons['eqatlas.com'].logo}`);
  expect(heading.querySelector('img')).toHaveAttribute('alt', '');
  fireEvent.error(heading.querySelector('img'));
  expect(heading).toHaveTextContent('EQ');
  expect(heading.querySelector('img')).toBeNull();

  rerender(<ResultHeading domain="p075.ezboard.com"><a href="/read/123">Norrath maps</a></ResultHeading>);
  expect(heading.querySelector('img')).toHaveAttribute('src', `/static/site-icons/${siteIcons['ezboard.com'].logo}`);
  rerender(<ResultHeading domain="unknown.example"><a href="/read/123">Norrath maps</a></ResultHeading>);
  expect(heading.querySelector('.archive-result-site-icon')).toBeNull();
});

it('uses the curated monogram when an archived site has no verified logo', () => {
  render(<ResultHeading domain="eqx.simplenet.com"><a href="/read/123">EverQuest Express archive</a></ResultHeading>);
  const heading = screen.getByRole('heading', { name: 'EverQuest Express archive' });
  expect(heading.querySelector('.archive-result-site-icon')).toHaveTextContent('EX');
  expect(heading.querySelector('img')).toBeNull();
});
