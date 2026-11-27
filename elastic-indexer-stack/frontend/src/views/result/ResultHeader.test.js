import React from 'react';
import fs from 'fs';
import path from 'path';
import { fireEvent, render, screen } from '@testing-library/react';
import ResultHeader from './ResultHeader';
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

it('gives the archived logo the single thumbnail slot and keeps the title accessible', () => {
  const title = <a href="/read/123">Norrath maps</a>;
  const thumbnail = '/thumbnails/website.webp';
  const { rerender } = render(<ResultHeader domain="WWW.EQATLAS.COM:80" thumbnail={thumbnail}>{title}</ResultHeader>);
  const heading = screen.getByRole('heading', { name: 'Norrath maps' });
  const header = heading.parentElement;
  expect(header.querySelectorAll('.archive-result-thumbnail')).toHaveLength(1);
  expect(heading.querySelector('img')).toBeNull();
  expect(header.querySelector('img')).toHaveAttribute('src', `/static/site-icons/${siteIcons['eqatlas.com'].logo}`);
  expect(header.querySelector('img')).toHaveAttribute('alt', '');
  fireEvent.error(header.querySelector('img'));
  expect(header.querySelector('.archive-result-thumbnail')).toHaveTextContent('EQ');
  expect(header.querySelector('img')).toBeNull();

  rerender(<ResultHeader domain="p075.ezboard.com" thumbnail={thumbnail}>{title}</ResultHeader>);
  expect(header.querySelector('img')).toHaveAttribute('src', `/static/site-icons/${siteIcons['ezboard.com'].logo}`);
  rerender(<ResultHeader domain="unknown.example" thumbnail={thumbnail}>{title}</ResultHeader>);
  expect(header.querySelector('.archive-result-thumbnail')).toHaveAttribute('src', thumbnail);
  rerender(<ResultHeader domain="unknown.example">{title}</ResultHeader>);
  expect(header.querySelector('.archive-result-thumbnail')).toBeNull();
});

it('uses the curated monogram when an archived site has no verified logo', () => {
  const { container } = render(<ResultHeader domain="eqx.simplenet.com" thumbnail="/thumbnails/website.webp">
    <a href="/read/123">EverQuest Express archive</a>
  </ResultHeader>);
  expect(container.querySelector('.archive-result-thumbnail')).toHaveTextContent('EX');
  expect(container.querySelector('img')).toBeNull();
});
