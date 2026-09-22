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
  WithSearch
} from "@elastic/react-search-ui";
import { Layout } from "@elastic/react-search-ui-views";
import "@elastic/react-search-ui-views/lib/styles/styles.css";
import {
  getConfig,
  getStandardFacetFields,
  getDatePickerFacetFields,
  buildSortOptionsFromConfig
} from "./config/config-helper";
import { Box, Button, Checkbox, FormControlLabel, Collapse, CircularProgress, CssBaseline, ThemeProvider } from "@mui/material";
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
import archiveTheme from "./theme";
import { CapturePaging, CaptureSummary } from "./views/search/CapturePaging";
import DocumentReader from "./views/reader/DocumentReader";
import { ReaderSearchContext, searchPhrase } from "./views/reader/reader-utils";
import fieldLabels from "./config/field-labels";
import "./views/ArchiveTheme.css";

function SearchApp() {
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
      // Override these settings to prevent automatic searches
      searchQuery: {
        ...(baseConfig.searchQuery || {}),
        searchAsYouType: false // This disables automatic searches
      }
    };
  }, [knnParamsRef]);

  return (
      <SearchProvider config={config}>
        <WithSearch
          mapContextToProps={({ wasSearched, isLoading, executeSearch, setCurrent, totalResults, pagingStart, pagingEnd, rawResponse, resultSearchTerm }) => ({ wasSearched, isLoading, executeSearch, setCurrent, totalResults, pagingStart, pagingEnd, rawResponse, resultSearchTerm })}
        >
          {({ wasSearched, isLoading, executeSearch, setCurrent, resultSearchTerm, ...searchState }) => (
            <div className="App" style={{ position: "relative" }} data-testid="app-container">
              {isLoading && (
                <Box
                  sx={{
                    position: "absolute",
                    top: "50%",
                    left: "50%",
                    transform: "translate(-50%, -50%)",
                    zIndex: 999
                  }}
                >
                  <CircularProgress size={80} />
                </Box>
              )}
              <Layout
                header={
                  <>
                    <ArchiveStatusBar />
                    <HeaderContent />
                    <EnhancedSearchBox searchAsYouType={true} enableSemanticSearch={knnParams.enableSemanticSearch} /> {/* We'll handle automatic searches ourselves */}
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
                  <ErrorBoundary>
                    <ReaderSearchContext.Provider value={searchPhrase(resultSearchTerm)}>
                      <Results
                      titleField={getConfig().titleField}
                      urlField={getConfig().urlField}
                      thumbnailField={getConfig().thumbnailField}
                      shouldTrackClickThrough={true}
                      resultView={CustomResultView}
                      />
                    </ReaderSearchContext.Provider>
                  </ErrorBoundary>
                }
                bodyHeader={
                  <>
                    {wasSearched && <div className="archive-group-controls">
                      <FormControlLabel label="Group repeated captures" control={<Checkbox checked={knnParams.groupCaptures} onChange={event => {
                        const next = { ...knnParamsRef.current, groupCaptures: event.target.checked };
                        knnParamsRef.current = next;
                        setKnnParams(next);
                        setCurrent(1);
                      }} />} />
                      {knnParams.groupCaptures ? <CaptureSummary {...searchState} /> : <PagingInfo />}
                    </div>}
                    {wasSearched && <ResultsPerPage />}
                  </>
                }
                bodyFooter={
                  <div>
                    <Paging view={knnParams.groupCaptures ? CapturePaging : undefined} isLoading={isLoading} />
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
        </WithSearch>
      </SearchProvider>
  );
}

export default function App() {
  return <ThemeProvider theme={archiveTheme}><CssBaseline />
    {/^\/document\/?$/.test(window.location.pathname) ? <DocumentReader /> : <SearchApp />}
  </ThemeProvider>;
}
