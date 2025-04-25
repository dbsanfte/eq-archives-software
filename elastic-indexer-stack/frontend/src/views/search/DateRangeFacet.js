import React, { useState, useEffect, useRef } from 'react';
import PropTypes from 'prop-types';
import { Box, Button, Typography } from '@mui/material';
import { DatePicker } from '@mui/x-date-pickers/DatePicker';
import { AdapterDateFns } from '@mui/x-date-pickers/AdapterDateFns';
import { LocalizationProvider } from '@mui/x-date-pickers/LocalizationProvider';
import { WithSearch } from '@elastic/react-search-ui';

function DateRangeFacetView({ label, field, filters, addFilter, removeFilter, searchkit }) {
  const [fromDate, setFromDate] = useState(null);
  const [toDate, setToDate] = useState(null);
  // Use ref instead of state to avoid triggering effects
  const operationInProgress = useRef(false); // Renamed to be more generic
  // Track if a filter is currently applied via postProcessRequest
  const [hasActiveDirectFilter, setHasActiveDirectFilter] = useState(false);
  
  // Find existing filter for this field
  const existingFilter = filters.find(filter => filter.field === field && filter.type === "range");
  
  // Check if there's an actual filter value (not just an empty array)
  const hasActiveFilter = existingFilter?.values?.length > 0 && 
                          existingFilter.values.some(v => Object.keys(v).length > 0);

  // Initialize dates from existing filter when filter changes
  useEffect(() => {
    // Skip the effect if we're actively applying or clearing the filter
    if (operationInProgress.current) {
      return;
    }

    if (existingFilter?.values?.length > 0) {
      const filterValue = existingFilter.values[0];
      
      if (filterValue.from) {
        setFromDate(new Date(filterValue.from));
      }
      
      if (filterValue.to) {
        setToDate(new Date(filterValue.to));
      }
    } else {
      // Reset dates when filter is removed
      setFromDate(null);
      setToDate(null);
    }
  }, [existingFilter]);

  const handleApplyFilter = () => {
    // Only apply filter if both dates are selected
    if (fromDate && toDate) {
      // Set flag to prevent useEffect from interfering
      operationInProgress.current = true;
      
      // Format dates as ISO strings for Elasticsearch
      const fromIso = fromDate.toISOString();
      const toIso = toDate.toISOString();
      
      // Create a range filter value for UI state tracking
      const rangeValue = {
        from: fromIso,
        to: toIso,
        name: `${fromDate.toLocaleDateString()} - ${toDate.toLocaleDateString()}`
      };
      
      // First, remove any existing filter for this field
      if (existingFilter) {
        removeFilter(field, null, "range");
      }
      
      // Apply filter to the original field for UI state tracking
      addFilter(field, rangeValue, "range");
      
      // Set flag to indicate we have an active direct filter
      setHasActiveDirectFilter(true);
      
      // Reset the operation flag after a delay
      setTimeout(() => {
        operationInProgress.current = false;
      }, 100);
    }
  };

  const handleClearFilter = () => {
    // Set the operation flag to prevent useEffect from restoring dates
    operationInProgress.current = true;
    
    // Clear local state first
    setFromDate(null);
    setToDate(null);
    
    // Remove UI filter
    removeFilter(field, null, "range");
    
    // Clear our direct filter flag
    setHasActiveDirectFilter(false);
    
    // Reset searchkit.postProcessRequest to remove our custom filter
    if (searchkit?.postProcessRequest) {
      const originalPostProcess = searchkit.postProcessRequest;
      searchkit.postProcessRequest = (body, state, queryConfig) => {
        // If post_filter exists, remove our filter
        if (body.post_filter?.bool?.must) {
          body.post_filter.bool.must = body.post_filter.bool.must.filter(
            filter => !(filter.range && filter.range[field])
          );
          
          // Clean up empty must arrays
          if (body.post_filter.bool.must.length === 0) {
            delete body.post_filter.bool.must;
          }
          
          // Clean up empty bool objects
          if (Object.keys(body.post_filter.bool).length === 0) {
            delete body.post_filter.bool;
          }
          
          // Clean up empty post_filter objects
          if (Object.keys(body.post_filter).length === 0) {
            delete body.post_filter;
          }
        }
        
        // Call the original post process if it exists
        if (originalPostProcess) {
          return originalPostProcess(body, state, queryConfig);
        }
        
        return body;
      };
    }
    
    // Reset the operation flag after a delay
    setTimeout(() => {
      operationInProgress.current = false;
    }, 100);
  };

  // Calculate if we should show the active filter box based on UI filter or direct filter
  const showActiveFilterBox = hasActiveFilter || hasActiveDirectFilter;

  return (
    <Box sx={{ mb: 3, mt: "32px" }}>
      <Typography 
        fontSize="12px"
        sx={{ mb: 1 }}
        className="sui-facet__title"
      >
        {label}
      </Typography>
      <LocalizationProvider dateAdapter={AdapterDateFns}>
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, mb: 2 }}>
          <DatePicker
            label="From Date"
            value={fromDate}
            onChange={(newValue) => setFromDate(newValue)}
            openTo="day"
            format="dd/MM/yyyy"
            slotProps={{ 
              textField: { 
                size: 'small', 
                fullWidth: true, 
                inputProps: { style: { fontSize: '13px' } },
                InputLabelProps: { style: { fontSize: '13px', zIndex: 0 } },
                placeholder: "DD/MM/YYYY",
                onKeyDown: (event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault();
                    if (fromDate && toDate) {
                      handleApplyFilter();
                    }
                  }
                }
              } 
            }}
            componentsProps={{
              popper: {
                sx: {
                  zIndex: 1050
                }
              }
            }}
          />
          <DatePicker
            label="To Date"
            value={toDate}
            onChange={(newValue) => setToDate(newValue)}
            openTo="day"
            format="dd/MM/yyyy"
            slotProps={{ 
              textField: { 
                size: 'small', 
                fullWidth: true, 
                inputProps: { style: { fontSize: '13px' } },
                InputLabelProps: { style: { fontSize: '13px', zIndex: 0 } },
                placeholder: "DD/MM/YYYY",
                onKeyDown: (event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault();
                    if (fromDate && toDate) {
                      handleApplyFilter();
                    }
                  }
                }
              } 
            }}
            minDate={fromDate}
          />
        </Box>
      </LocalizationProvider>
      
      <Box sx={{ display: 'flex', gap: 1 }}>
        <Button 
          variant="contained" 
          color="primary" 
          size="small"
          disabled={!fromDate || !toDate}
          onClick={handleApplyFilter}
        >
          Apply
        </Button>
        <Button 
          variant="outlined" 
          size="small"
          onClick={handleClearFilter}
          disabled={!existingFilter && !fromDate && !toDate && !hasActiveDirectFilter}
        >
          Clear
        </Button>
      </Box>

      {showActiveFilterBox && (
        <Box sx={{ mt: 1, bgcolor: '#f0f7ff', p: 1, borderRadius: 1 }}>
          <Typography variant="caption">
            Active filter: {fromDate && toDate ? 
              `${fromDate.toLocaleDateString()} - ${toDate.toLocaleDateString()}` :
              existingFilter?.values?.map(v => v.name).join(', ')}
          </Typography>
        </Box>
      )}
    </Box>
  );
}

DateRangeFacetView.propTypes = {
  label: PropTypes.string.isRequired,
  field: PropTypes.string.isRequired,
  filters: PropTypes.array.isRequired,
  addFilter: PropTypes.func.isRequired,
  removeFilter: PropTypes.func.isRequired,
  searchkit: PropTypes.object
};

// Connect component to Search UI's context
const DateRangeFacet = ({ field, label }) => (
  <WithSearch
    mapContextToProps={({ filters, addFilter, removeFilter, driver }) => ({
      filters,
      addFilter,
      removeFilter,
      searchkit: driver && driver.apiConnector
    })}
  >
    {props => <DateRangeFacetView field={field} label={label} {...props} />}
  </WithSearch>
);

DateRangeFacet.propTypes = {
  field: PropTypes.string.isRequired,
  label: PropTypes.string.isRequired
};

export default DateRangeFacet;