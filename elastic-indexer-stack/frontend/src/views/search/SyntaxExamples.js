import React from "react";
import { Box, Typography, List, ListItem, Link } from "@mui/material";
import SyntaxHighlighter from "react-syntax-highlighter";
import { materialDark } from "react-syntax-highlighter/dist/esm/styles/prism";

function SyntaxExamples({}) {
  const examples = [
    {
      title: "Free-text semantic search:",
      content: `Soloing as a cleric is too hard
Verant is the best game company ever
Necros are overpowered`
    },
    {
      title: "Doc must contain this exact match:",
      content: `"Match this phrase exactly"`
    },
    {
      title: "Doc must contain all these matches:",
      content: `"Match docs with" "these three" "text phrases only"`
    },
    {
      title: "Doc can contain any of these matches:",
      content: `"Match docs with" OR "any of these three" OR "text phrases"`
    },
    {
      title: "Excluding unwanted matches:",
      content: `"Match docs with this phrase" NOT "this unwanted phrase"`
    },
    {
      title: "Grouping terms with parentheses:",
      content: `( "Match docs with this phrase" OR "this other phrase" ) AND "this phrase too"`
    },
    {
      title: "Domain filtering:",
      content: `domain_name:("eq.castersrealm.com") 
domain_name:("eq.castersrealm.com" OR "eq.crgaming.net") 
domain_name:(NOT "eq.castersrealm.com")`
    },
    {
      title: "Date range filtering:",
      content: `llm_guessed_date:[1999-01-01 TO 2001-12-31] 
capture_date:[1999-01-01 TO 1999-12-31]`
    },
    {
      title: "Elasticsearch Syntax Reserved Characters:",
      content: `+ - = && || > < ! ( ) { } [ ] ^ " ~ * ? : \\ /`
    }
  ];

  return (
    <Box
      p={2}
      sx={{ border: "1px solid #ccc", borderRadius: "4px", marginBottom: "1rem" }}
    >
      <Typography variant="h4" gutterBottom>
        Syntax Examples
      </Typography>
      <List>
        {examples.map((example) => (
          <ListItem key={example.title} alignItems="flex-start" sx={{ display: "block", mb: 2 }}>
            <Typography variant="h6">{example.title}</Typography>
            <SyntaxHighlighter
              language="json"
              style={materialDark}
              customStyle={{ backgroundColor: "black", padding: "0.5rem", borderRadius: "4px", marginTop: "0.5rem" }}
            >
              {example.content}
            </SyntaxHighlighter>
          </ListItem>
        ))}
        <ListItem key="elastic-docs" alignItems="flex-start" sx={{ display: "block", mb: 2 }}>
          <Typography variant="h6" gutterBottom>
            Elasticsearch Query String Syntax Reference:
          </Typography>
          <Typography variant="body1" sx={{ whiteSpace: "pre-wrap", mt: 1, fontFamily: "monospace", marginTop: "0.5rem" }}>
            <Link href="https://www.elastic.co/guide/en/elasticsearch/reference/current/query-dsl-query-string-query.html#query-string-syntax" target="_blank" rel="noopener">
              https://www.elastic.co/guide/en/elasticsearch/reference/current/query-dsl-query-string-query.html#query-string-syntax
            </Link>
          </Typography>
        </ListItem>
      </List>
    </Box>
  );
}

SyntaxExamples.propTypes = {};

export default SyntaxExamples;
