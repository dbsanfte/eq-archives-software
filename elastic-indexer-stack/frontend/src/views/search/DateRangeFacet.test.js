import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import DateRangeFacet from './DateRangeFacet';
import filterRegistry from '../../search/FilterRegistry';

let mockSearchState;
jest.mock('@elastic/react-search-ui', () => ({
  WithSearch: ({ children, mapContextToProps }) => children(mapContextToProps(mockSearchState))
}));
jest.mock('@mui/x-date-pickers/DatePicker', () => ({
  DatePicker: ({ label, value, onChange, slotProps }) => (
    <input aria-label={label} value={value ? value.toISOString().slice(0, 10) : ''}
      onChange={event => onChange(event.target.value ? new Date(event.target.value) : null)}
      onKeyDown={slotProps.textField.onKeyDown} />
  )
}));
jest.mock('@mui/x-date-pickers/LocalizationProvider', () => ({
  LocalizationProvider: ({ children }) => children
}));

const from = '1999-01-01T00:00:00.000Z';
const to = '2001-12-31T00:00:00.000Z';
const view = () => <DateRangeFacet field="capture_date" label="Capture date" />;
const saved = values => [{ field: 'capture_date', type: 'range', values }];
function chooseDates() {
  fireEvent.change(screen.getByLabelText('From Date'), { target: { value: from.slice(0, 10) } });
  fireEvent.change(screen.getByLabelText('To Date'), { target: { value: to.slice(0, 10) } });
}

beforeEach(() => {
  jest.useFakeTimers();
  filterRegistry.disableLogging();
  filterRegistry.clearAllFilters();
  window.__activeDateFilters.clear();
  mockSearchState = { filters: [], addFilter: jest.fn(), removeFilter: jest.fn() };
  jest.spyOn(console, 'log').mockImplementation(() => {});
});
afterEach(() => {
  jest.clearAllTimers();
  jest.useRealTimers();
  jest.restoreAllMocks();
});

test('requires a complete range, applies it to search, replaces duplicates, and clears it', () => {
  const { unmount } = render(view());
  expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Clear' })).toBeDisabled();
  fireEvent.keyDown(screen.getByLabelText('From Date'), { key: 'Enter' });
  fireEvent.keyDown(screen.getByLabelText('To Date'), { key: 'Enter' });
  chooseDates();
  fireEvent.keyDown(screen.getByLabelText('From Date'), { key: 'Tab' });
  fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
  expect(mockSearchState.addFilter).toHaveBeenCalledWith('capture_date',
    expect.objectContaining({ from, to, isDateField: true }), 'range');
  expect(filterRegistry.applyFilters({}).query.bool.filter).toEqual([
    { range: { capture_date: { gte: from, lte: to } } }
  ]);
  const query = filterRegistry.applyFilters({ query: { bool: { filter: [
    { term: { domain: 'example.org' } },
    { range: { capture_date: { gte: 'old' } } },
    { range: { other_date: { gte: from } } }
  ] } } });
  expect(query.query.bool.filter).toHaveLength(3);
  expect(query.query.bool.filter[0]).toEqual({ term: { domain: 'example.org' } });
  act(() => jest.advanceTimersByTime(100));
  fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
  expect(mockSearchState.removeFilter).toHaveBeenCalledWith('capture_date', null, 'range');
  expect(filterRegistry.getFilterCount()).toBe(0);
  expect(screen.getByLabelText('From Date')).toHaveValue('');
  act(() => jest.advanceTimersByTime(100));
  unmount();
  expect(window.__activeDateFilters.size).toBe(0);
});

test('restores a bookmarked filter, supports keyboard apply, and persists across remounts', () => {
  mockSearchState.filters = saved([{ from, to, name: 'Bookmarked range' }]);
  let mounted = render(view());
  expect(screen.getByLabelText('From Date')).toHaveValue('1999-01-01');
  expect(screen.getByLabelText('To Date')).toHaveValue('2001-12-31');
  expect(filterRegistry.getFilterCount()).toBe(1);
  fireEvent.keyDown(screen.getByLabelText('From Date'), { key: 'Enter' });
  fireEvent.keyDown(screen.getByLabelText('To Date'), { key: 'Enter' });
  fireEvent.keyDown(screen.getByLabelText('To Date'), { key: 'Tab' });
  expect(mockSearchState.removeFilter).toHaveBeenCalledWith('capture_date', null, 'range');
  expect(mockSearchState.addFilter).toHaveBeenCalledTimes(2);
  act(() => jest.advanceTimersByTime(100));
  mounted.unmount();
  expect(filterRegistry.getFilterCount()).toBe(1);
  mounted = render(view());
  expect(filterRegistry.getFilterCount()).toBe(1);
  mockSearchState.filters = [];
  mounted.rerender(view());
  expect(screen.getByLabelText('From Date')).toHaveValue('');
  expect(screen.getByLabelText('To Date')).toHaveValue('');
});

test.each([
  { from, name: 'Since 1999' },
  { to, name: 'Before 2002' },
  { name: 'Any time' },
  { from, to, name: 'Nested date', isNestedField: true }
])('handles partial and nested filter state without registering a standard range: %j', value => {
  mockSearchState.filters = saved([value]);
  render(view());
  expect(filterRegistry.getFilterCount()).toBe(0);
  if (!value.from || !value.to) expect(screen.getByText('Active filter: ' + value.name)).toBeInTheDocument();
});

test('ignores filters for other fields and empty range values', () => {
  mockSearchState.filters = [
    { field: 'domain', type: 'all', values: ['example.org'] },
    { field: 'capture_date', type: 'range', values: [{}] }
  ];
  render(view());
  expect(screen.queryByText(/Active filter:/)).not.toBeInTheDocument();
  expect(filterRegistry.getFilterCount()).toBe(0);
});
