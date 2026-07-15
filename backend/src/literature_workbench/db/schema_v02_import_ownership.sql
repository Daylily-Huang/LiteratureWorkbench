ALTER TABLE import_promotions ADD COLUMN expected_volume_id TEXT;
ALTER TABLE import_promotions ADD COLUMN expected_file_id TEXT;
ALTER TABLE import_promotions ADD COLUMN expected_size_bytes INTEGER;
ALTER TABLE import_promotions ADD COLUMN expected_hard_link_count INTEGER;
ALTER TABLE import_promotions ADD COLUMN moved_volume_id TEXT;
ALTER TABLE import_promotions ADD COLUMN moved_file_id TEXT;
ALTER TABLE import_promotions ADD COLUMN moved_size_bytes INTEGER;
ALTER TABLE import_promotions ADD COLUMN moved_hard_link_count INTEGER;
ALTER TABLE import_promotions ADD COLUMN ownership_provenance TEXT NOT NULL DEFAULT 'source_claim'
  CHECK (ownership_provenance IN ('source_claim','handle_move','crash_recovery'));
ALTER TABLE import_promotions ADD COLUMN cleanup_authorized INTEGER NOT NULL DEFAULT 0
  CHECK (
    cleanup_authorized IN (0,1) AND
    (
      cleanup_authorized = 0 OR
      (
        status IN ('moved','completed','failed') AND
        moved_volume_id IS NOT NULL AND moved_file_id IS NOT NULL AND
        moved_size_bytes IS NOT NULL AND moved_hard_link_count IS NOT NULL AND
        ownership_provenance IN ('handle_move','crash_recovery')
      )
    )
  );
