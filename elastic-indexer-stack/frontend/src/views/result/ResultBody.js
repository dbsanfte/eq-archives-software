import React from "react";
import PropTypes from "prop-types";
import "./ResultBody.css";

function ResultBody({ summaryHTML, textHTML }) {
  // Older records carry a processing placeholder instead of a summary.
  const hasSummary = summaryHTML && summaryHTML.trim() !== "[ Still awaiting LLM Enrichment... ]";

  return (
    <div className="sui-result__body">
      <div className="sui-result__details">
        {textHTML && (
          <section className="result-text-snippet">
            <h3>From the archive</h3>
            <div dangerouslySetInnerHTML={{ __html: textHTML }} />
          </section>
        )}
        {hasSummary && (
          <details className="result-summary result-summary--collapsed">
            <summary>AI-generated summary</summary>
            <div dangerouslySetInnerHTML={{ __html: summaryHTML }} />
          </details>
        )}
        {!hasSummary && !textHTML && (
          <p className="archive-result-placeholder">Open the full text to explore this record.</p>
        )}
      </div>
    </div>
  );
}

ResultBody.propTypes = {
  summaryHTML: PropTypes.string.isRequired,
  textHTML: PropTypes.string.isRequired
};

export default ResultBody;
