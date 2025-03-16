import React, { useRef, useEffect, useState, useMemo } from "react";
import {
  ErrorBoundary,
  Facet,
  SearchProvider,
  SearchBox,
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
import { Box, Button, Collapse, CircularProgress } from "@mui/material";
import CustomResultView from "./views/result/CustomResultView";
import HeaderContent from "./views/HeaderContent"; 
import ArchiveStatusBar from "./views/ArchiveStatusBar";
import SearchParameters from "./views/search/SearchParameters"; 
import AdvancedSettings, { DEFAULT_KNN_PARAMS } from "./views/search/AdvancedSettings";
import SyntaxExamples from "./views/search/SyntaxExamples";
import { getSearchConfig } from "./search/Connector";
import DateRangeFacet from "./views/search/DateRangeFacet";
import { getDate } from "date-fns";

export default function App() {
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [showSyntax, setShowSyntax] = useState(false);
  const [knnParams, setKnnParams] = useState(DEFAULT_KNN_PARAMS);

  const knnParamsRef = useRef(knnParams);
  useEffect(() => {
    knnParamsRef.current = knnParams;
  }, [knnParams]);

  function handleParamChange(param, value) {
    setKnnParams(prev => ({ ...prev, [param]: value }));
  }

  const config = useMemo(() => getSearchConfig(knnParamsRef), [knnParamsRef]);

  return (
    <SearchProvider config={config}>
      <WithSearch 
        mapContextToProps={({ wasSearched, isLoading, executeSearch }) => ({ wasSearched, isLoading, executeSearch })}
      >
        {({ wasSearched, isLoading, executeSearch }) => (
          <div className="App" style={{ position: "relative" }}>
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
                  <SearchBox searchAsYouType={true} />
                  <Button
                    variant="contained"
                    style={{ marginTop: "1rem" }}
                    onClick={() => setShowAdvanced(!showAdvanced)}
                  >
                    Advanced...
                  </Button>
                  <Button
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
                      return <DateRangeFacet key={field} field={field} label={field} />;
                    })
                  }
                  {
                    getStandardFacetFields().map(field => {
                      // Use default Facet for standard fields
                      return <Facet key={field} field={field} label={field} />;
                    })
                  }
                </div>
              }
              bodyContent={
                <ErrorBoundary>                
                  <Results
                    titleField={getConfig().titleField}
                    urlField={getConfig().urlField}
                    thumbnailField={getConfig().thumbnailField}
                    shouldTrackClickThrough={true}
                    resultView={CustomResultView}
                  />
                </ErrorBoundary>
              }
              bodyHeader={
                <>
                  {wasSearched && <PagingInfo />}
                  {wasSearched && <ResultsPerPage />}
                </>
              }
              bodyFooter={<Paging />}
            />
          </div>
        )}
      </WithSearch>
    </SearchProvider>
  );
}