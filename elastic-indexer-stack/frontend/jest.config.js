module.exports = {
  testEnvironment: 'jsdom',
  setupFilesAfterEnv: ['<rootDir>/src/setupTests.js'],
  moduleNameMapper: {
    '\\.(css|less|scss|sass)$': 'identity-obj-proxy'
  },
  transform: {
    '^.+\\.(js|jsx|ts|tsx)$': 'babel-jest'
  },
  transformIgnorePatterns: [
    'node_modules/(?!(trim-lines|devlop|react-syntax-highlighter|react-markdown|remark-.*|rehype-.*|mdast-.*|micromark.*|unist-.*|unified|bail|is-plain-obj|trough|vfile.*|hast-.*|property-information|space-separated-tokens|comma-separated-tokens|decode-named-character-reference|character-entities|escape-string-regexp|markdown-table|estree-.*|html-.*|zwitch|ccount|longest-streak)/)'
  ],
  collectCoverage: true,
  coverageReporters: ['text', 'lcov'],
  coverageDirectory: 'coverage',
  coverageThreshold: {
    global: {
      branches: 90,
      functions: 90,
      lines: 90,
      statements: 90
    }
  }
};