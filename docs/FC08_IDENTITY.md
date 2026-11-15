# FC-08 cross-source identity and dedupe provenance

Status (2026-11-15): **code complete with fixtures**. `foia_archive/identity.py`,
migration 12 (`document_relations`), `python main.py analyze-identity`
(single-writer lock).

Relations are evidence between two records; **nothing is merged, deleted or
rewritten**, and every source association and content hash stays on its record.

| Relation | Meaning | Identity group? |
| --- | --- | --- |
| `url_variant` | different stored URLs normalizing to the same URL | yes |
| `identical_bytes` | archived binaries share SHA-256 | yes |
| `text_near_duplicate` | different bytes, word-5-shingle Jaccard ≥ 0.8 (MinHash/LSH candidates, exact Jaccard confirmed) | no — related version |
| `redaction_variant` | near-duplicate whose redaction-marker counts differ (`(b)(6)`, `[REDACTED]`, █) | no — related version |
| `third_party_copy` | FC-04 release crosslinked to an official record | no |

Texts under 20 words and LSH buckets over 500 members (boilerplate) are not
treated as evidence. Record pages show "Related public versions" separately;
`foia-document-relations-export/1` (`schemas/document-relation.v1.schema.json`,
`GET /api/v1/export/document-relations`, `export-dataset document-relations`)
publishes relations between FC-13 `record_id`s. Agency renames/reorganizations
and request-log tracking-ID joins remain follow-up work.
