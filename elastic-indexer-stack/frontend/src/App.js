import React, { useRef, useEffect, useState, useMemo } from "react";
import {
  ErrorBoundary,
  Facet,
  SearchProvider,
  Results,
  PagingInfo,
  ResultsPerPage,
  Paging,
  Sorting,
  withSearch
} from "@elastic/react-search-ui";
import { Layout } from "@elastic/react-search-ui-views";
import "@elastic/react-search-ui-views/lib/styles/styles.css";
import {
  getConfig,
  getStandardFacetFields,
  getDatePickerFacetFields,
  buildSortOptionsFromConfig
} from "./config/config-helper";
import { Box, Button, Checkbox, FormControlLabel, Collapse, CssBaseline, ThemeProvider } from "@mui/material";
import { KeyboardArrowUp } from "@mui/icons-material";
import CustomResultView from "./views/result/CustomResultView";
import HeaderContent from "./views/HeaderContent"; 
import ArchiveStatusBar from "./views/ArchiveStatusBar";
import SearchParameters from "./views/search/SearchParameters"; 
import AdvancedSettings, { DEFAULT_KNN_PARAMS } from "./views/search/AdvancedSettings";
import SyntaxExamples from "./views/search/SyntaxExamples";
import { getSearchConfig } from "./search/Connector";
import DateRangeFacet from "./views/search/DateRangeFacet";
import EnhancedSearchBox from "./views/search/EnhancedSearchBox";
import SearchError from "./views/search/SearchError";
import SearchProgress from "./views/search/SearchProgress";
import { createSearchActivity, SearchActivityContext, useSearchActivity } from './search/SearchActivity';
import archiveTheme from "./theme";
import { CapturePaging, CaptureSummary } from "./views/search/CapturePaging";
import DocumentReader from "./views/reader/DocumentReader";
import { ReaderSearchContext, searchPhrase } from "./views/reader/reader-utils";
import fieldLabels from "./config/field-labels";
import RecentSites from "./views/sites/RecentSites";
import Explore from "./views/explore/Explore";
import SiteSearchScope, { scopedDomains, websiteScope } from "./views/search/SiteSearchScope";
import "./views/ArchiveTheme.css";

// Search UI's render-prop WithSearch creates a new component type whenever its
// parent renders. Keep this subscription stable so opening panels or changing
// options cannot unmount the input and cancel a pending automatic search.
const SearchState = withSearch(({
  wasSearched, isLoading, error, searchTerm, setSearchTerm, setCurrent,
  totalResults, pagingStart, pagingEnd, rawResponse, resultSearchTerm, filters, removeFilter, results,
  current, totalPages, resultsPerPage
}) => ({
  wasSearched, isLoading, error,
  executeSearch: () => setSearchTerm(searchTerm, { shouldClearFilters: false }),
  searchTerm, setSearchTerm, setCurrent, totalResults, pagingStart, pagingEnd,
  rawResponse, resultSearchTerm, filters, removeFilter, results, current, totalPages, resultsPerPage
}))(({ children, ...state }) => {
  const { phase, display } = useSearchActivity();
  const pending = ['waiting', 'preparing', 'searching', 'reranking'].includes(phase);
  return children({ ...state, ...display,
    isLoading: pending || (phase === 'idle' && !display && !state.error && state.isLoading),
    searchPhase: phase, hasPreviousResults: Boolean(display?.results.length),
    results: display?.results || [], wasSearched: Boolean(display) || (phase === 'idle' && state.wasSearched)
  });
});

function SearchApp() {
  const [activity] = useState(createSearchActivity);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [showSyntax, setShowSyntax] = useState(false);
  const [knnParams, setKnnParams] = useState({ ...DEFAULT_KNN_PARAMS, groupCaptures: true });

  const knnParamsRef = useRef(knnParams);
  useEffect(() => {
    knnParamsRef.current = knnParams;
  }, [knnParams]);

  function handleParamChange(param, value) {
    setKnnParams(prev => ({ ...prev, [param]: value }));
  }

  function scrollToTop() {
    window.scrollTo({
      top: 0,
      behavior: 'smooth'
    });
  }

  // Create config with searchAsYouType disabled to prevent auto-searches, 
  // as we override this behaviour in EnhancedSearchBox
  const config = useMemo(() => {
    const baseConfig = getSearchConfig(knnParamsRef);
    return {
      ...baseConfig,
      onSearch: (state, queryConfig, next) => activity.search(state,
        prepared => (baseConfig.onSearch || next)(prepared, queryConfig)),
      // Override these settings to prevent automatic searches
      searchQuery: {
        ...(baseConfig.searchQuery || {}),
        searchAsYouType: false // This disables automatic searches
      }
    };
  }, [knnParamsRef, activity]);

  return (
      <SearchActivityContext.Provider value={activity}><SearchProvider config={config}>
        <SearchState>
          {({ wasSearched, isLoading, error, executeSearch, setCurrent, resultSearchTerm, filters, removeFilter, searchPhase, hasPreviousResults, ...searchState }) => (
            <div className="App" style={{ position: "relative" }} data-testid="app-container">
              <Layout
                header={
                  <>
                    <ArchiveStatusBar />
                    <HeaderContent compact={scopedDomains(filters).length > 0 || websiteScope(filters)} />
                    <SiteSearchScope filters={filters} removeFilter={removeFilter} />
                    <EnhancedSearchBox searchAsYouType={true} enableSemanticSearch={knnParams.enableSemanticSearch} /> {/* We'll handle automatic searches ourselves */}
                    <SearchProgress phase={searchPhase} isLoading={isLoading} hasResults={hasPreviousResults} />
                    <Button
                      className="archive-search-option"
                      variant="contained"
                      style={{ marginTop: "1rem" }}
                      onClick={() => setShowAdvanced(!showAdvanced)}
                    >
                      Advanced...
                    </Button>
                    <Button
                      className="archive-search-option"
                      variant="contained"
                      style={{ marginTop: "1rem", marginLeft: "1rem" }}
                      onClick={() => setShowSyntax(!showSyntax)}
                    >
                      Search Syntax...
                    </Button>
                    <Collapse in={showAdvanced}>
                      <Box sx={{ marginTop: "1rem", marginBottom: "1rem" }}>
                        <SearchParameters
                          values={knnParams}
                          onChange={handleParamChange}
                          onSearch={executeSearch}
                        />
                      </Box>
                      <AdvancedSettings values={knnParams} onChange={handleParamChange} />
                    </Collapse>
                    <Collapse in={showSyntax}>
                      <Box sx={{ marginTop: "1rem", marginBottom: "1rem" }}>
                        <SyntaxExamples />
                      </Box>
                    </Collapse>
                  </>
                }
                sideContent={
                  <div>
                    {
                      wasSearched && (
                        <Sorting
                          label="Sort by"
                          sortOptions={buildSortOptionsFromConfig()}
                        />
                      )
                    }
                    {
                      getDatePickerFacetFields().map(field => {
                        // Use custom DateRangeFacet for historical date fields
                        return <DateRangeFacet key={field} field={field} label={fieldLabels[field] || field} />;
                      })
                    }
                    {
                      getStandardFacetFields().map(field => {
                        // Use default Facet for standard fields
                        return <Facet key={field} field={field} label={fieldLabels[field] || field} isFilterable={true} />;
                      })
                    }
                  </div>
                }
                bodyContent={
                  <ErrorBoundary view={SearchError} error={isLoading ? '' : error} onRetry={executeSearch}>
                  <section aria-label="Search results" aria-busy={isLoading}>
                    <ReaderSearchContext.Provider value={searchPhrase(resultSearchTerm)}>
                      <Results
                        results={searchState.results}
                        titleField={getConfig().titleField}
                        urlField={getConfig().urlField}
                        thumbnailField={getConfig().thumbnailField}
                        shouldTrackClickThrough={true}
                        resultView={CustomResultView}
                      />
                    </ReaderSearchContext.Provider>
                  </section>
                  </ErrorBoundary>
                }
                bodyHeader={
                  <>
                    {wasSearched && <div className="archive-group-controls">
                      <FormControlLabel label="Group captures and discussions" control={<Checkbox checked={knnParams.groupCaptures} onChange={event => {
                        const next = { ...knnParamsRef.current, groupCaptures: event.target.checked };
                        knnParamsRef.current = next;
                        setKnnParams(next);
                        setCurrent(1);
                      }} />} />
                      {(!isLoading || searchState.pagingEnd > 0) && (knnParams.groupCaptures ? <CaptureSummary {...searchState} /> : <PagingInfo
                        totalResults={searchState.totalResults} pagingStart={searchState.pagingStart} pagingEnd={searchState.pagingEnd} />)}
                    </div>}
                    {wasSearched && <ResultsPerPage />}
                  </>
                }
                bodyFooter={
                  <div>
                    <Paging view={knnParams.groupCaptures ? CapturePaging : undefined}
                      current={searchState.current} totalPages={searchState.totalPages} isLoading={isLoading} />
                    {wasSearched && (
                      <Box sx={{ display: 'flex', justifyContent: 'center', marginTop: '1rem' }}>
                        <Button
                          variant="outlined"
                          onClick={scrollToTop}
                          startIcon={<KeyboardArrowUp />}
                          sx={{ textTransform: 'none' }}
                        >
                          Back to Top
                        </Button>
                      </Box>
                    )}
                  </div>
                }
              />
            </div>
          )}
        </SearchState>
      </SearchProvider></SearchActivityContext.Provider>
  );
}

export default function App() {
  return <ThemeProvider theme={archiveTheme}><CssBaseline />
    {/^\/document\/?$/.test(window.location.pathname) ? <DocumentReader /> :
      /^\/sites\/?$/.test(window.location.pathname) ? <RecentSites /> :
        /^\/explore\/?$/.test(window.location.pathname) ? <Explore /> : <SearchApp />}
  </ThemeProvider>;
}
