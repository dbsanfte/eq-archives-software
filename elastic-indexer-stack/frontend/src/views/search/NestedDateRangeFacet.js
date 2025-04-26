import React, { useState, useEffect, useRef } from 'react';
import PropTypes from 'prop-types';
import { Box, Button, Typography } from '@mui/material';
import { DatePicker } from '@mui/x-date-pickers/DatePicker';
import { AdapterDateFns } from '@mui/x-date-pickers/AdapterDateFns';
import { LocalizationProvider } from '@mui/x-date-pickers/LocalizationProvider';
import { WithSearch } from '@elastic/react-search-ui';
import filterRegistry from '../../search/FilterRegistry';

// Create a persistent registry to track active filters across remounts
if (!window.__activeNestedDateFilters) {
  window.__activeNestedDateFilters = new Set();
}

function NestedDateRangeFacetView({ label, field, filters, addFilter, removeFilter }) {
  const [fromDate, setFromDate] = useState(null);
  const [toDate, setToDate] = useState(null);
  const operationInProgress = useRef(false);
  const [hasActiveDirectFilter, setHasActiveDirectFilter] = useState(false);
  const filterId = `nested-date-range-${field}`;
  const filterActive = useRef(false);
  const initialUrlRestoreRef = useRef(true);
  
  // Find existing filter for this field
  const existingFilter = filters.find(filter => filter.field === field && filter.type === "range");
  
  // Check if there's an actual filter value
  const hasActiveFilter = existingFilter?.values?.length > 0 && 
                          existingFilter.values.some(v => Object.keys(v).length > 0);

  // Special initialization for page load - detect URL parameters and restore filters
  useEffect(() => {
    if (initialUrlRestoreRef.current && existingFilter?.values?.length > 0) {
      const filterValue = existingFilter.values[0];
      
      // Check if this is a nested field filter (has our marker)
      if (filterValue.isNestedField === true) {
        console.log(`[${field}] Restoring filter from URL parameters: ${filterId}`);
        
        if (filterValue.from && filterValue.to) {
          // Re-register the filter
          registerFilterWithValues(
            new Date(filterValue.from), 
            new Date(filterValue.to),
            false // Don't mark as operation in progress
          );
          
          // Update state with the values from URL
          setFromDate(new Date(filterValue.from));
          setToDate(new Date(filterValue.to));
          setHasActiveDirectFilter(true);
          filterActive.current = true;
          
          // Add to our active filters registry
          window.__activeNestedDateFilters.add(filterId);
        }
      }
      
      // Mark initialization as complete
      initialUrlRestoreRef.current = false;
    }
  }, []);

  // Check if we should re-register our filter (if it was active before component remount)
  useEffect(() => {
    // Skip if we're handling a page load from URL parameters
    if (!initialUrlRestoreRef.current && window.__activeNestedDateFilters.has(filterId) && existingFilter?.values?.length > 0) {
      console.log(`[${field}] Re-registering persisted filter after remount: ${filterId}`);
      const filterValue = existingFilter.values[0];
      
      if (filterValue.from && filterValue.to) {
        // Re-register the filter with the previous values
        registerFilterWithValues(
          new Date(filterValue.from), 
          new Date(filterValue.to),
          false // Don't mark as operation in progress
        );
        
        // Update state with the existing values
        if (filterValue.from) {
          setFromDate(new Date(filterValue.from));
        }
        
        if (filterValue.to) {
          setToDate(new Date(filterValue.to));
        }
        
        setHasActiveDirectFilter(true);
        filterActive.current = true;
      }
    }
  }, []);

  // Initialize dates from existing filter when filter changes
  useEffect(() => {
    if (operationInProgress.current || initialUrlRestoreRef.current) {
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
      setFromDate(null);
      setToDate(null);
    }
  }, [existingFilter]);

  // Helper function to register a filter with given date values
  const registerFilterWithValues = (fromDateValue, toDateValue, markInProgress = true) => {
    if (markInProgress) {
      operationInProgress.current = true;
    }
    
    // Format dates as ISO strings for Elasticsearch
    const fromIso = fromDateValue.toISOString();
    const toIso = toDateValue.toISOString();
    
    // Mark this filter as active in our persistent registry
    window.__activeNestedDateFilters.add(filterId);
    
    // Register the nested filter with our FilterRegistry
    console.log(`[${field}] Registering filter with ID: ${filterId}`);
    filterRegistry.registerFilter(filterId, (body) => {
      if (!body.query) {
        body.query = { bool: {} };
      }
      
      if (!body.query.bool) {
        body.query.bool = {};
      }
      
      if (!body.query.bool.filter) {
        body.query.bool.filter = [];
      }
      
      // Remove any existing direct range filter for this field to avoid duplicates
      body.query.bool.filter = body.query.bool.filter.filter(
        filter => !(
          (filter.range && filter.range[field]) || 
          (filter.nested && filter.nested.path === field)
        )
      );
      
      // Also remove any existing filter inside nested bool queries
      body.query.bool.filter = body.query.bool.filter.filter(
        filter => !(
          filter.bool && 
          filter.bool.filter && 
          filter.bool.filter.some(f => f.range && f.range[field])
        )
      );
      
      // Add properly structured nested query
      const nestedQuery = {
        nested: {
          path: field,
          query: {
            range: {
              [`${field}.date`]: {
                gte: fromIso,
                lte: toIso
              }
            }
          }
        }
      };
      
      body.query.bool.filter.push(nestedQuery);
      return body;
    });
    
    console.log(`Current filter count: ${filterRegistry.getFilterCount()}`);
    filterRegistry.listFilters();
    
    // Set filter as active
    filterActive.current = true;
  };

  const handleApplyFilter = () => {
    if (fromDate && toDate) {
      // Create a range filter value for UI state tracking only
      // We'll use a special placeholder value to indicate this is a nested filter
      const rangeValue = {
        from: fromDate.toISOString(),
        to: toDate.toISOString(),
        name: `${fromDate.toLocaleDateString()} - ${toDate.toLocaleDateString()}`,
        isNestedField: true // Important marker for URL state restoration
      };
      
      // Remove any existing filter
      if (existingFilter) {
        removeFilter(field, null, "range");
      }
      
      // Apply filter to the original field for UI state tracking ONLY
      // The actual query will be built by the filter registry
      addFilter(field, rangeValue, "range");
      
      // Register the filter
      registerFilterWithValues(fromDate, toDate);
      
      setHasActiveDirectFilter(true);
      
      setTimeout(() => {
        operationInProgress.current = false;
      }, 100);
    }
  };

  const handleClearFilter = () => {
    operationInProgress.current = true;
    
    setFromDate(null);
    setToDate(null);
    
    removeFilter(field, null, "range");
    setHasActiveDirectFilter(false);
    
    // Remove our filter from the registry and from the persistent set
    filterRegistry.removeFilter(filterId);
    window.__activeNestedDateFilters.delete(filterId);
    filterActive.current = false;
    
    setTimeout(() => {
      operationInProgress.current = false;
    }, 100);
  };

  // Clean up filter when component unmounts - but only if not an active filter
  useEffect(() => {
    return () => {
      console.log(`[${field}] Component unmounting, filter active: ${filterActive.current}`);
      // Only clean up if this filter isn't active
      if (!filterActive.current) {
        console.log(`[${field}] Removing filter: ${filterId} during unmount`);
        filterRegistry.removeFilter(filterId);
        window.__activeNestedDateFilters.delete(filterId);
      } else {
        console.log(`[${field}] Preserving active filter: ${filterId} during unmount`);
      }
    };
  }, [filterId, field]);

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

NestedDateRangeFacetView.propTypes = {
  label: PropTypes.string.isRequired,
  field: PropTypes.string.isRequired,
  filters: PropTypes.array.isRequired,
  addFilter: PropTypes.func.isRequired,
  removeFilter: PropTypes.func.isRequired
};

// Connect component to Search UI's context
const NestedDateRangeFacet = ({ field, label }) => (
  <WithSearch
    mapContextToProps={({ filters, addFilter, removeFilter }) => ({
      filters,
      addFilter,
      removeFilter
    })}
  >
    {props => <NestedDateRangeFacetView field={field} label={label} {...props} />}
  </WithSearch>
);

NestedDateRangeFacet.propTypes = {
  field: PropTypes.string.isRequired,
  label: PropTypes.string.isRequired
};

export default NestedDateRangeFacet;