"""Quick script to review top matches from the output."""
import gzip
import json
import os
import sys

from config import OUTPUT_DIR

sys.stdout.reconfigure(encoding='utf-8')

results = []
output_dir = OUTPUT_DIR

for root, dirs, files in os.walk(output_dir):
    for f in files:
        if f.endswith(".jsonl.gz"):
            with gzip.open(os.path.join(root, f), "rt", encoding="utf-8") as fh:
                for line in fh:
                    results.append(json.loads(line.strip()))

# Sort by semantic score descending
results.sort(key=lambda x: -x["semantic_score"])

print(f"\nTotal matches: {len(results)}")
print(f"Languages: {sorted(set(r['language'] for r in results))}")
print(f"\n{'='*70}")
print("TOP 8 MATCHES BY SEMANTIC SCORE")
print(f"{'='*70}")

for r in results[:8]:
    print(f"\nScore: {r['semantic_score']:.3f} | Lang: {r['language']} | Keywords: {r['matched_keywords'][:3]}")
    print(f"URL: {r['url'][:80]}")
    print(f"Paragraph: {r['paragraph'][:250]}...")
    print("-" * 70)
