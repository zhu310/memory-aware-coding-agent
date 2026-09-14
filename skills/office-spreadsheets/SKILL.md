---
name: office-spreadsheets
description: Create and edit Excel XLSX workbooks, formulas, formatting and charts; verify values and preserve originals.
---

Use excel_workbook for XLSX creation, inspection and copy-on-edit. Macro-enabled XLSM, legacy XLS, complex pivots and specialized embedded objects require a separate compatibility workflow.

Inspect first: action="inspect",file_id=...,sheet="Sheet",cell_range="A1:F30". Formula expressions and cached_value are separate. Never infer a computed value from an uncached expression.

Create/edit takes operations, each with type and sheet (existing sheet name). New workbooks start with Sheet.
- rename_sheet: {type:"rename_sheet",sheet:"Sheet",title:"Budget"}
- add_sheet: {type:"add_sheet",sheet:"Notes"}
- write: {type:"write",sheet:"Budget",start:"A1",rows:[["Item","Amount"],["A",120],["Total","=SUM(B2:B2)"]]}. null clears a cell. Strings beginning = are intentionally formulas; plain user data must not be accidentally converted to formulas.
- format: {type:"format",sheet:"Budget",range:"A1:B1",bold:true,fill:"DDE5DB"}. Optional number_format,color,wrap.
- freeze: {type:"freeze",sheet:"Budget",cell:"A2"}; filter/merge require range.
- column_width: {type:"column_width",sheet:"Budget",column:"A",width:25}.
- chart: {type:"chart",sheet:"Budget",range:"A1:B4",chart_type:"bar",title:"Budget",anchor:"D2"}; line also supported. First column categories, first row headers, remaining columns values.

Write formulas with English function names and comma separators. Editing preserves ordinary cells/styles/charts; advanced Excel features need specialist review. Output is a new asset.

openpyxl does NOT calculate formulas. Call office_render(file_id=output_id,request_id=stable_id,format="xlsx") to create a recalculated copy. Wait creative_job_status; inspect that copy and check cached values, including errors. LibreOffice compatibility is not equivalent to native Excel for every function. For visual inspection render to PDF and inspect each page with image_understand. Large sheets need explicit print areas/pagination; use installed openpyxl on a copy for advanced print setup, then file_import.
