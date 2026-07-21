#!/usr/bin/env python3
"""
export_csv.py — Build one CSV of EMIS-code-wise total enrolment
=================================================================

Reads every district JSON in data/ (written by fetch.py), sums each
school's class-wise male/female enrolment, and writes a single CSV:

    data/enrolment_summary.csv

Apps Script (in the Google Sheet) fetches this file's raw GitHub URL
daily and replaces the "waqas testing" tab with its contents.
"""

import json
import os
import glob
import csv

DATA_DIR = "data"
OUTPUT_FILE = os.path.join(DATA_DIR, "enrolment_summary.csv")

HEADER = [
    "District",
    "Tehsil",
    "Markaz",
    "School ID",
    "EMIS Code",
    "School Name",
    "Total Male Enrolment",
    "Total Female Enrolment",
    "Total Enrolment",
    "Scraped At",
]


def sum_grades(school):
    male_total = 0
    female_total = 0
    for grade in school.get("grades", []):
        male_total += int(grade.get("male_students", 0) or 0)
        female_total += int(grade.get("female_students", 0) or 0)
    return male_total, female_total


def build_rows():
    rows = []
    district_files = sorted(glob.glob(os.path.join(DATA_DIR, "*.json")))

    for filepath in district_files:
        if os.path.basename(filepath) in ("index.json",):
            continue

        with open(filepath, "r", encoding="utf-8") as f:
            payload = json.load(f)

        scraped_at = payload.get("scraped_at", "")

        for school in payload.get("schools", []):
            male_total, female_total = sum_grades(school)
            rows.append([
                school.get("district", ""),
                school.get("tehsil", ""),
                school.get("markaz", ""),
                school.get("school_id", ""),
                school.get("emis_code", ""),
                school.get("school_name", ""),
                male_total,
                female_total,
                male_total + female_total,
                scraped_at,
            ])
    return rows


def main():
    rows = build_rows()
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows(rows)
    print(f"Wrote {len(rows)} rows to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
