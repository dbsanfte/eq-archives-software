import React, { useState, useEffect, useRef } from "react";
import { Box, Typography } from "@mui/material";
import { getConfig } from "../config/config-helper";

export default function ArchiveStatusBar() {
  const buildSha = process.env.REACT_APP_GIT_SHA;
  const [docCount, setDocCount] = useState(0);
  // Start with a null value to indicate indexing rate is not ready
  const [indexRate, setIndexRate] = useState(null);
  // useRef to hold the previous document count between renders
  const previousCountRef = useRef(0);
  // Use a ref to store rate samples for the rolling average
  const rateSamplesRef = useRef([]);
  // To discard the very first sample result on page load
  const isFirstSampleRef = useRef(true);
  // Polling interval in milliseconds
  const POLL_INTERVAL = 5000; // 5 seconds
  const MAX_SAMPLES = 30 / (POLL_INTERVAL / 1000); // 6 samples for 30 seconds

  const { indexName } = getConfig();

  const fetchStats = () => {
    fetch("/elasticsearch/" + indexName + "/_count")
      .then((response) => response.json())
      .then((data) => {
        // Use count from the _count endpoint
        const count = data.count || 0;
        // Discard the very first result
        if (isFirstSampleRef.current) {
          isFirstSampleRef.current = false;
          previousCountRef.current = count;
          setDocCount(count);
          return;
        }
        const delta = count - previousCountRef.current;
        const currentRate = (delta / (POLL_INTERVAL / 1000)) * 60;
        // Update the rate sample array
        rateSamplesRef.current.push(currentRate);
        if (rateSamplesRef.current.length > MAX_SAMPLES) {
          rateSamplesRef.current.shift();
        }
        // Only calculate and set the rolling average if we have at least 2 samples
        if (rateSamplesRef.current.length >= 2) {
          const avgRate = rateSamplesRef.current.reduce((acc, rate) => acc + rate, 0) / rateSamplesRef.current.length;
          setIndexRate(avgRate);
        } else {
          setIndexRate(null);
        }
        // Update document count and previous count
        setDocCount(count);
        previousCountRef.current = count;
      })
      .catch((error) =>
        console.error("Error fetching archive stats:", error)
      );
  };

  useEffect(() => {
    // Initial fetch
    fetchStats();
    // Set up the polling interval
    const intervalId = setInterval(fetchStats, POLL_INTERVAL);
    return () => clearInterval(intervalId);
  }, []);

  return (
    <Box
      className="archive-status-bar"
      aria-label="Archive status"
    >
      <Typography className="archive-status-bar__count" variant="subtitle1">
        Documents Indexed: {docCount.toLocaleString()}
      </Typography>
      {indexRate !== null && indexRate > 0 && (
        <Typography className="archive-status-bar__rate" variant="subtitle1">
          Indexing Rate: {indexRate.toFixed(2)} docs/min
        </Typography>
      )}
      <Typography
        className="archive-status-bar__revision"
        variant="body2"
        title={buildSha || "Local development build"}
      >
        Build: {buildSha ? buildSha.slice(0, 7) : "development"}
      </Typography>
    </Box>
  );
}
