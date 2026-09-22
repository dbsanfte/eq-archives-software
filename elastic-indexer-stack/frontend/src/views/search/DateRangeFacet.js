import React, { useState, useEffect } from 'react';
import PropTypes from 'prop-types';
import { Box, Button, Typography } from '@mui/material';
import { DatePicker } from '@mui/x-date-pickers/DatePicker';
import { AdapterDateFns } from '@mui/x-date-pickers/AdapterDateFns';
import { LocalizationProvider } from '@mui/x-date-pickers/LocalizationProvider';
import { withSearch } from '@elastic/react-search-ui';
import { format, isValid, parseISO } from 'date-fns';

// Filter bounds describe UTC calendar days. Pickers display those calendar dates
// in the browser's timezone without shifting a day when a saved URL is opened.
function pickerDate(bound) {
  if (typeof bound !== 'string') return null;
  const date = parseISO(bound.slice(0, 10));
  return isValid(date) ? date : null;
}

function selectableDate(date) {
  return isValid(date) && date.getFullYear() >= 1900 && date.getFullYear() <= 2099;
}

function DateRangeFacetView({ label, field, filters, setFilter, removeFilter }) {
  const applied = filters.find(filter => filter.field === field && filter.type === 'range')?.values[0];
  const from = applied?.from;
  const to = applied?.to;
  const [fromDate, setFromDate] = useState(() => pickerDate(from));
  const [toDate, setToDate] = useState(() => pickerDate(to));

  // Only a changed applied range (Apply, Clear, or history navigation) replaces
  // draft input. Results and equivalent filter objects must leave editing alone.
  useEffect(() => {
    setFromDate(pickerDate(from));
    setToDate(pickerDate(to));
  }, [from, to]);

  const validDates = selectableDate(fromDate) && selectableDate(toDate);
  const reversed = validDates && fromDate > toDate;
  const canApply = validDates && !reversed;
  const apply = () => {
    if (!canApply) return;
    setFilter(field, {
      from: `${format(fromDate, 'yyyy-MM-dd')}T00:00:00.000Z`,
      to: `${format(toDate, 'yyyy-MM-dd')}T23:59:59.999Z`,
      name: `${format(fromDate, 'dd/MM/yyyy')} – ${format(toDate, 'dd/MM/yyyy')}`,
      isDateField: true
    }, 'range');
  };
  const onKeyDown = event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      apply();
    }
  };
  const textField = {
    size: 'small', fullWidth: true, placeholder: 'DD/MM/YYYY', onKeyDown,
    sx: { '& .MuiInputBase-input': { fontSize: '13px' }, '& .MuiInputLabel-root': { fontSize: '13px' } }
  };

  return (
    <Box className="archive-date-facet" sx={{ mb: 3, mt: '32px' }}>
      <Typography fontSize="12px" sx={{ mb: 1 }} className="sui-facet__title">{label}</Typography>
      <LocalizationProvider dateAdapter={AdapterDateFns}>
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          <DatePicker label="From Date" value={fromDate} onChange={setFromDate}
            format="dd/MM/yyyy" slotProps={{ textField }} />
          <DatePicker label="To Date" value={toDate} onChange={setToDate}
            minDate={isValid(fromDate) ? fromDate : undefined} format="dd/MM/yyyy"
            slotProps={{ textField: { ...textField, ...(reversed && {
              error: true, helperText: 'Choose a date on or after From Date.'
            }) } }} />
          <Typography variant="caption" color="text.secondary">Includes both dates (UTC).</Typography>
          <Box sx={{ display: 'flex', gap: 1 }}>
            <Button variant="contained" size="small" onClick={apply} disabled={!canApply}>Apply</Button>
            <Button variant="outlined" size="small" disabled={!applied && !fromDate && !toDate}
              onClick={() => {
                setFromDate(null);
                setToDate(null);
                removeFilter(field, null, 'range');
              }}>Clear</Button>
          </Box>
          {applied && <Box sx={{ bgcolor: 'var(--archive-sage)', p: 1, borderRadius: 1 }}>
            <Typography variant="caption">Active filter: {applied.name}</Typography>
          </Box>}
        </Box>
      </LocalizationProvider>
    </Box>
  );
}

DateRangeFacetView.propTypes = {
  label: PropTypes.string.isRequired,
  field: PropTypes.string.isRequired,
  filters: PropTypes.array.isRequired,
  setFilter: PropTypes.func.isRequired,
  removeFilter: PropTypes.func.isRequired
};

// Construct the connected component once. WithSearch's render-prop wrapper
// creates a new component type whenever its parent renders, losing picker drafts.
export default withSearch(({ filters, setFilter, removeFilter }) => ({ filters, setFilter, removeFilter }))(DateRangeFacetView);
