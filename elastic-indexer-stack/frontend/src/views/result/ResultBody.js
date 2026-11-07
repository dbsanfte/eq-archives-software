import React from "react";
import PropTypes from "prop-types";
import "./ResultBody.css";

// Elasticsearch supplies escaped text with em markers. Render only those markers
// as elements; even malformed or legacy snippets can never introduce source HTML.
function Snippet({ html }) {
  html = Array.isArray(html) ? html.join(' ') : html;
  const decode = value => {
    const node = document.createElement('textarea');
    node.innerHTML = value.replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return node.value;
  };
  return html.split(/(<em>.*?<\/em>)/gis).map((part, index) =>
    /^<em>/i.test(part) ? <em key={index}>{decode(part.slice(4, -5))}</em> : decode(part));
}

function ResultBody({ summaryHTML, textHTML }) {
  // Older records carry a processing placeholder instead of a summary.
  const hasSummary = summaryHTML && summaryHTML.trim() !== "[ Still awaiting LLM Enrichment... ]";

  return (
    <div className="sui-result__body">
      <div className="sui-result__details">
        {textHTML && (
          <section className="result-text-snippet">
            <h3>From the archive</h3>
            <div><Snippet html={textHTML} /></div>
          </section>
        )}
        {hasSummary && (
          <details className="result-summary result-summary--collapsed">
            <summary>AI-generated summary</summary>
            <div>{summaryHTML}</div>
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
  textHTML: PropTypes.oneOfType([PropTypes.string, PropTypes.arrayOf(PropTypes.string)]).isRequired
};

export default ResultBody;
