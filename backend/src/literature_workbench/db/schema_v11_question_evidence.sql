ALTER TABLE document_relations ADD COLUMN question TEXT NOT NULL DEFAULT '';
ALTER TABLE document_relations ADD COLUMN difference_basis TEXT NOT NULL DEFAULT 'unspecified' CHECK(difference_basis IN ('unspecified','claim','method','sample','context'));
ALTER TABLE document_relations ADD COLUMN comparison_note TEXT NOT NULL DEFAULT '';
