import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import DateRangeFacet from './DateRangeFacet';

let mockSearchState;
jest.mock('@elastic/react-search-ui', () => ({
  withSearch: mapContextToProps => Component => props => <Component {...mapContextToProps(mockSearchState)} {...props} />
}));
jest.mock('@mui/x-date-pickers/DatePicker', () => ({
  DatePicker: ({ label, value, onChange, slotProps }) => (
    <input aria-label={label} value={value && !isNaN(value) ? value.toISOString().slice(0, 10) : ''}
      onChange={event => onChange(event.target.value ? new Date(event.target.value + 'T00:00:00') : null)}
      onKeyDown={slotProps.textField.onKeyDown} />
  )
}));
jest.mock('@mui/x-date-pickers/LocalizationProvider', () => ({
  LocalizationProvider: ({ children }) => children
}));

const from = '1999-01-01T00:00:00.000Z';
const to = '2001-12-31T23:59:59.999Z';
const view = () => <DateRangeFacet field="capture_date" label="Capture date" />;
const saved = values => [{ field: 'capture_date', type: 'range', values }];
const change = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
function chooseDates() {
  change('From Date', '1999-01-01');
  change('To Date', '2001-12-31');
}
beforeEach(() => {
  mockSearchState = { filters: [], setFilter: jest.fn(), removeFilter: jest.fn() };
});

test('applies a complete inclusive UTC range atomically, with keyboard support', () => {
  render(view());
  expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Clear' })).toBeDisabled();
  fireEvent.keyDown(screen.getByLabelText('From Date'), { key: 'Enter' });
  expect(mockSearchState.setFilter).not.toHaveBeenCalled();
  chooseDates();
  fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
  fireEvent.keyDown(screen.getByLabelText('From Date'), { key: 'Tab' });
  fireEvent.keyDown(screen.getByLabelText('From Date'), { key: 'Enter' });
  fireEvent.keyDown(screen.getByLabelText('To Date'), { key: 'Enter' });
  expect(mockSearchState.setFilter).toHaveBeenCalledTimes(3);
  expect(mockSearchState.setFilter).toHaveBeenLastCalledWith('capture_date', {
    from, to, name: '01/01/1999 – 31/12/2001', isDateField: true
  }, 'range');
  expect(mockSearchState.removeFilter).not.toHaveBeenCalled();
});

test('restores an applied range on remount and clears controls with its search filter', () => {
  mockSearchState.filters = saved([{ from, to, name: 'Bookmarked range' }]);
  let mounted = render(view());
  expect(screen.getByLabelText('From Date')).toHaveValue('1999-01-01');
  expect(screen.getByLabelText('To Date')).toHaveValue('2001-12-31');
  mounted.unmount();
  mounted = render(view());
  expect(screen.getByLabelText('From Date')).toHaveValue('1999-01-01');
  fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
  expect(mockSearchState.removeFilter).toHaveBeenCalledWith('capture_date', null, 'range');
  expect(screen.getByLabelText('From Date')).toHaveValue('');
  expect(screen.getByLabelText('To Date')).toHaveValue('');
  mockSearchState.filters = [];
  mounted.rerender(view());
  expect(screen.queryByText(/Active filter:/)).not.toBeInTheDocument();
});

test('does not replace edited dates when unchanged applied filters are returned as new objects', () => {
  mockSearchState.filters = saved([{ from, to, name: 'Bookmarked range' }]);
  const mounted = render(view());
  change('From Date', '2000-01-01');
  mockSearchState.filters = JSON.parse(JSON.stringify(mockSearchState.filters));
  mounted.rerender(view());
  expect(screen.getByLabelText('From Date')).toHaveValue('2000-01-01');
  expect(screen.getByText('Active filter: Bookmarked range')).toBeInTheDocument();
});

test('restores and removes dates through browser history after starting unfiltered', () => {
  const mounted = render(view());
  mockSearchState.filters = saved([{ from, to, name: 'Bookmarked range' }]);
  mounted.rerender(view());
  expect(screen.getByLabelText('From Date')).toHaveValue('1999-01-01');
  expect(screen.getByLabelText('To Date')).toHaveValue('2001-12-31');
  mockSearchState.filters = saved([{ from: '2000-01-01T00:00:00.000Z', to, name: 'Updated range' }]);
  mounted.rerender(view());
  expect(screen.getByLabelText('From Date')).toHaveValue('2000-01-01');
  mockSearchState.filters = [];
  mounted.rerender(view());
  expect(screen.getByLabelText('From Date')).toHaveValue('');
  expect(screen.getByLabelText('To Date')).toHaveValue('');
});

test('keeps incomplete drafts through result updates and unrelated filters', () => {
  const mounted = render(view());
  change('From Date', '2000-01-01');
  mockSearchState.filters = [{ field: 'domain_name', type: 'all', values: ['example.org'] }];
  mounted.rerender(view());
  expect(screen.getByLabelText('From Date')).toHaveValue('2000-01-01');
  expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
  expect(screen.getByLabelText('From Date')).toHaveValue('');
});

test.each([
  ['not a date', '2000-01-01'], ['2000-01-01', 'not a date'],
  ['2001-01-01', '2000-01-01'], ['', '2000-01-01'], ['2000-01-01', ''],
  ['1899-01-01', '2000-01-01'], ['2000-01-01', '2100-01-01']
])('does not apply invalid, partial or reversed dates: %s – %s', (start, end) => {
  render(view());
  change('From Date', start);
  change('To Date', end);
  expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled();
  fireEvent.keyDown(screen.getByLabelText('To Date'), { key: 'Enter' });
  expect(mockSearchState.setFilter).not.toHaveBeenCalled();
});

test('allows a single-day range and includes the very end of that day', () => {
  render(view());
  change('From Date', '2000-02-29');
  change('To Date', '2000-02-29');
  fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
  expect(mockSearchState.setFilter).toHaveBeenCalledWith('capture_date', expect.objectContaining({
    from: '2000-02-29T00:00:00.000Z', to: '2000-02-29T23:59:59.999Z'
  }), 'range');
});

test.each([{ from, name: 'Since 1999' }, { to, name: 'Before 2002' },
  { from: 'invalid', to: 123, name: 'Malformed bookmark' }])('safely restores partial or invalid bookmarked dates: %j', value => {
  mockSearchState.filters = saved([value]);
  render(view());
  expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled();
  expect(screen.getByText('Active filter: ' + value.name)).toBeInTheDocument();
});
