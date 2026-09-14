---
name: office-documents
description: Create and edit Word DOCX documents, preserve uploaded originals, and verify rendered layout.
---

Use word_document for ordinary DOCX creation and editing. Legacy .doc files, tracked-change workflows, text boxes and embedded objects are not fully supported; preserve originals and state concrete limits.

- Inspect the uploaded file with word_document(action="inspect",file_id=...) and file_read before editing.
- Create: action="create",name="report.docx",blocks=[{"type":"heading","level":0,"text":"Title"},{"type":"paragraph","text":"Body"},{"type":"table","rows":[["Item","Value"],["A","12"]]}]. Other block types: bullet, numbered, page_break, image (file_id of PNG/JPEG, width_cm).
- Edit: action="edit",file_id=...,name="report-revised.docx",replacements=[{"old":"old text","new":"new text","expected_count":1}]. Text replacements span runs in body paragraphs, tables, headers and footers; unmatched or ambiguous counts fail without publishing. blocks append content. Never silently rebuild an existing document from extracted plain text.
- Returned asset.id is the new file; parent_file_id records provenance. Download links are in asset.download_url. Never invent links or overwrite the source.
- For layout verification call office_render(file_id=new_id,request_id=stable_id,format="pdf"), then creative_job_status(wait_seconds=30). Inspect each PDF page with image_understand. Do not claim a visual review if the renderer/vision call failed.
- Complex edits outside the tool schema can use installed python-docx through bash, operating on a copy of the stored asset. Import the completed file with file_import. Do not discard unsupported structures without explaining the loss.
