import React from "react";
import PropTypes from "prop-types";
import "./ResultBody.css";

function ResultBody({ result, summaryHTML, textHTML }) {
  return (
    <div className="sui-result__body">
      <div className="sui-result__image">
        <img src={result.thumbnail.raw} alt="" />
      </div>
      <div className="sui-result__details">
        <section className="result-summary">
          <h3>Summary</h3>
          <div dangerouslySetInnerHTML={{ __html: summaryHTML }} />
        </section>
        <section className="result-full-text">
          <h3>Text Snippet</h3>
          <div dangerouslySetInnerHTML={{ __html: textHTML }} />
        </section>
      </div>
    </div>
  );
}

ResultBody.propTypes = {
  result: PropTypes.object.isRequired,
  summaryHTML: PropTypes.string.isRequired,
  textHTML: PropTypes.string.isRequired,
};

export default ResultBody;