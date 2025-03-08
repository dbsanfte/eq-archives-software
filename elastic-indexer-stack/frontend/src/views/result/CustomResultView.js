import React from "react";
import LabelRow from "./LabelRow";
import TagRow from "./TagRow";
import ResultBody from "./ResultBody";
import ButtonRow from "./ButtonRow";

const CustomResultView = (context, onClickLink) => {
  const summary = context.result.llm_summary;
  let summaryHTML = "";
  if (summary?.raw) {
    summaryHTML = summary.raw;
  }
  
  const text_full = context.result.text_full;
  let textHTML = "";
  if (text_full?.snippet) {
    textHTML = text_full.snippet;
  } 

  return (
    <li className="sui-result">
      {/* Header with link to source URL */}
      <div className="sui-result__header">
        <a
          className="sui-result__title sui-result__title-link"
          onClick={onClickLink}
          href={context.result.url.raw}
          target="_blank"
          rel="noopener noreferrer"
        >
          {context.result.title.raw}
        </a>
      </div>
      {/* Label Row providing metadata labels for the result */}
      <LabelRow
        flavour={context.result.llm_content_flavour?.raw}
        mailingList={context.result.mailing_list_name?.raw}
        domain={context.result.domain_name?.raw}
        captureDate={context.result.capture_date?.raw?.split("T")[0]}
        guessedDate={context.result.llm_guessed_date?.raw}
      />
      {/* Tag Row providing llm_tags from metadata if any defined */}
      {context.result.llm_tags && <TagRow tags={context.result.llm_tags.raw} />}
      
      {/* Main result body with summary and text */}
      <ResultBody 
        result={context.result} 
        summaryHTML={summaryHTML} 
        textHTML={textHTML} 
      />
      
      {/* Button Row for further interactions */}
      <ButtonRow result={context.result} />
    </li>
  );
};

export default CustomResultView;