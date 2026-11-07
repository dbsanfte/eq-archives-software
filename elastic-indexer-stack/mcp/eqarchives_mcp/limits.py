"""Shared conservative analyzed-text accounting for browser and MCP queries."""
import re


class TextBudget:
    def __init__(self):
        self.chars = self.terms = self.clauses = 0

    def accept(self, value, limit=1000):
        if not isinstance(value, str) or not 0 < len(value) <= limit or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid analyzed text")
        value.encode("utf-8")
        # Bound standard analysis without an ES call: punctuation splits ASCII
        # words, long runs split at 255 characters, and other scripts may emit
        # a token per character. Preserve the caller's original query text.
        terms = sum((len(part) + 254) // 255 if part.isascii() else 1
                    for part in re.findall(r"[A-Za-z0-9]+|[^\x00-\x7f\s]", value))
        self.chars += len(value)
        self.terms += terms
        self.clauses += 1
        if terms > 100 or self.chars > 4000 or self.terms > 400 or self.clauses > 8:
            raise ValueError("Excessive analyzed text")
        return value
