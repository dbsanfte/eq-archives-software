import React from "react";
import "./ArchiveHeader.css";

export default function HeaderContent() {
  return (
    <header className="archive-masthead">
      <div className="archive-masthead__top">
        <a className="archive-brand" href="/" aria-label="EQ Archives home">
          <img src="/images/archive-mark.svg" width="36" height="36" alt="" />
          <span>EQ Archives</span>
        </a>
        <nav className="archive-resource-links" aria-label="Archive resources">
          <a href="/chatgpt.html">ChatGPT</a>
          <a
            href="https://www.youtube.com/watch?v=DWXsCpAwKU4"
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Watch the search guide on YouTube (opens in a new tab)"
          >
            <svg viewBox="0 0 20 20" width="16" height="16" fill="none" aria-hidden="true" focusable="false">
              <circle cx="10" cy="10" r="7.25" stroke="currentColor" strokeWidth="1.3" />
              <path d="m8.25 6.75 5 3.25-5 3.25z" fill="currentColor" />
            </svg>
            <span><span className="archive-resource-links__optional">Search </span>Guide</span>
          </a>
          <a
            href="https://discord.com/channels/@me/312315372021743616"
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Contact Dolalin on P99 Discord (opens in a new tab)"
          >
            Contact
            <svg viewBox="0 0 20 20" width="14" height="14" fill="none" aria-hidden="true" focusable="false">
              <path d="M5 15 15 5M5 5h10v10" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </a>
        </nav>
      </div>
      <div className="archive-introduction">
        <div className="archive-introduction__copy">
          <p className="archive-eyebrow">The EverQuest collection</p>
          <h1>Rediscover early EverQuest.</h1>
          <p className="archive-introduction__description">
            Explore the websites, mailing lists, and conversations that tell the
            story of Norrath.
          </p>
        </div>
        <img
          className="archive-introduction__map"
          src="/images/archive-map.svg"
          width="440"
          height="220"
          alt=""
          aria-hidden="true"
        />
      </div>
    </header>
  );
}
