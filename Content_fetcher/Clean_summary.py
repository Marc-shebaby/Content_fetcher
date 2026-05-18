import pandas as pd
import re
from ftfy import fix_text

INPUT_FILE = "firecrawl_results_cleaned.csv"
OUTPUT_FILE = "firecrawl_results_cleaned.csv"
COLUMN = "summary"

def clean_mojibake(text):
    if pd.isna(text):
        return text

    text = str(text)

    # Fix mojibake like â€™, Â­, etc.
    text = fix_text(text)

    # Remove soft hyphens, including cases left behind after decoding
    text = text.replace("\u00ad", "")

    # Normalize non-breaking spaces
    text = text.replace("\xa0", " ")

    # Collapse repeated whitespace
    text = re.sub(r"\s+", " ", text).strip()

    return text

df = pd.read_csv(INPUT_FILE)

if COLUMN not in df.columns:
    raise ValueError(f"Column '{COLUMN}' not found. Available columns: {list(df.columns)}")

df[COLUMN] = df[COLUMN].apply(clean_mojibake)

df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8")

print(f"Cleaned CSV saved to {OUTPUT_FILE}")