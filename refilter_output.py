import gzip
import json
import os
import logging
import sqlite3
from pathlib import Path
from tqdm import tqdm
from collections import Counter

# Import the updated matcher and config
from matcher import HybridMatcher
from config import OUTPUT_DIR, SEMANTIC_THRESHOLD, MIN_NARRATIVE_INDICATORS, DB_PATH

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)-7s | %(message)s')
logger = logging.getLogger(__name__)

def refilter():
    logger.info("Initializing HybridMatcher with new thresholds...")
    logger.info(f"Thresholds: Semantic >= {SEMANTIC_THRESHOLD}, Narrative >= {MIN_NARRATIVE_INDICATORS}")
    matcher = HybridMatcher(threshold=SEMANTIC_THRESHOLD)
    
    # Track new match counts per source file
    # Key is the encoded filename (stem), value is the new total match count
    new_match_counts = Counter()
    
    # First, we'll reset all matches_found to 0 for completed files in the DB
    # (We will then populate them with the new counts)
    logger.info("Resetting match counts in database...")
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE processing_state SET matches_found = 0 WHERE status = 'completed'")
    conn.commit()

    # Iterate through all language folders in output
    if not OUTPUT_DIR.exists():
        logger.warning(f"Output directory {OUTPUT_DIR} does not exist.")
        return

    all_files = list(OUTPUT_DIR.glob("**/*.jsonl.gz"))
    logger.info(f"Found {len(all_files)} JSONL files to process.")

    for file_path in tqdm(all_files, desc="Refiltering files"):
        records = []
        
        # Read existing records
        try:
            with gzip.open(file_path, "rt", encoding="utf-8") as f:
                for line in f:
                    records.append(json.loads(line))
        except Exception as e:
            logger.error(f"Failed to read {file_path}: {e}")
            continue
        
        if not records:
            # Delete empty file if found
            try:
                os.remove(file_path)
            except: pass
            continue
            
        # Re-filter records
        passing_records = []
        for record in records:
            text = record["paragraph"]
            
            # Check semantic threshold (score from record)
            if record["semantic_score"] < SEMANTIC_THRESHOLD:
                continue
            
            # Check narrative indicators (re-calculate with NEW logic)
            indicators = matcher.narrative_filter.count_indicators(text)
            if indicators >= MIN_NARRATIVE_INDICATORS:
                passing_records.append(record)
        
        # Update our counter for the source file
        # The filename stem (without .jsonl.gz) is the encoded wet_path
        source_stem = file_path.name
        if source_stem.endswith(".jsonl.gz"):
            source_stem = source_stem[:-9]
        
        new_match_counts[source_stem] += len(passing_records)
        
        # Overwrite file with passing records (or delete if none left)
        if passing_records:
            try:
                with gzip.open(file_path, "wt", encoding="utf-8") as f:
                    for record in passing_records:
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error(f"Failed to write {file_path}: {e}")
        else:
            # If no records passed, delete the file
            try:
                os.remove(file_path)
            except Exception as e:
                logger.error(f"Failed to delete empty file {file_path}: {e}")

    # Now update the database with the new counts
    logger.info("Updating database with new match counts...")
    
    # We need a mapping from encoded stem to original file_path
    # Since the encoding is: wet_path.replace("/", "_").replace("\\", "_")
    # and stripping .gz, we can try to find the file_path in the DB.
    
    update_data = []
    for stem, count in tqdm(new_match_counts.items(), desc="Preparing DB updates"):
        if count == 0: continue
        
        # We search for the file_path that matches this stem
        # This is a bit slow if we do it for every file, so let's try a bulk approach or a smart query.
        # Actually, let's just find them.
        update_data.append((count, stem))

    if update_data:
        # SQLite REPLACE(REPLACE(file_path, '/', '_'), '\', '_') trick
        # We handle the .gz stripping by checking both possibilities
        conn.execute("BEGIN TRANSACTION")
        try:
            for count, stem in tqdm(update_data, desc="Applying DB updates"):
                # Matches either the path as-is (with underscores) or after replacing slashes
                # We also need to handle the .gz extension that was stripped in some versions
                query = """
                    UPDATE processing_state 
                    SET matches_found = ? 
                    WHERE 
                        REPLACE(REPLACE(file_path, '/', '_'), '\\', '_') = ? 
                        OR REPLACE(REPLACE(REPLACE(file_path, '/', '_'), '\\', '_'), '.gz', '') = ?
                """
                conn.execute(query, (count, stem, stem))
            conn.commit()
            logger.info(f"Updated {len(update_data)} records in progress.db")
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to update database: {e}")
    
    # Clean up empty directories
    for lang_dir in OUTPUT_DIR.iterdir():
        if lang_dir.is_dir() and not any(lang_dir.iterdir()):
            try:
                lang_dir.rmdir()
                logger.info(f"Removed empty directory: {lang_dir.name}")
            except: pass

    conn.close()
    logger.info("Refiltering complete.")

if __name__ == "__main__":
    refilter()
