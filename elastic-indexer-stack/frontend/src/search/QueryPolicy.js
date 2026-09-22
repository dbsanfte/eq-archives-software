const RESERVED_CHARS = [
  '"', '<', '>', '=', '*', '^', '[', ']', '(', ')', '{',
  '}', '!', '+', '-', '&&', '|', ':', '~', '?', '\\', '/',
  "AND", "OR", "NOT", "TO"
];

export const usesQuerySyntax = query => RESERVED_CHARS.some(char => query.includes(char));

export const shouldUseSemanticSearch = (query, enabled, vectorFields, nestedVectorFields) =>
  Boolean(query?.trim() && enabled && !usesQuerySyntax(query) &&
    (vectorFields?.length || nestedVectorFields?.length));
