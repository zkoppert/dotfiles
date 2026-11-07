---
name: spreadsheet-report-generation
description: Use when creating or modifying Excel workbooks, spreadsheet reports, generated reports, report hyperlinks, or report templates.
---

# Spreadsheet and report generation

Read and follow this skill before creating or modifying spreadsheet or generated report artifacts.

## Excel & Report Generation
- Use **openpyxl** for Excel file creation in Python
- Excel can only support one hyperlink per cell; use a separate References sheet for multiple links
- For display in cells, use clean text (e.g., `github#418801`) without raw URLs
- Match existing format/templates when extending reports; don't invent new layouts without asking

<!-- This boundary keeps the migrated baseline block byte-verifiable. -->
