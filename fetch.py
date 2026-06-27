=================================================================
  SIS PESRP Scraper — TEST MODE (first 50 schools)
=================================================================
[Network] Requesting CSRF token from server...
[Network] CSRF Token received: 00b10572e3...
[Network] Requesting Districts list...
[Success] Found 40 Districts.

Phase 1a: Mapping Tehsils and Markazs sequentially...
  -> ATTOCK: Found 6 tehsils
  -> BAHAWALNAGAR: Found 5 tehsils
  -> BAHAWALPUR: Found 6 tehsils
  -> BHAKKAR: Found 4 tehsils
  -> CHAKWAL: Found 3 tehsils
  -> CHINIOT: Found 3 tehsils
  -> D.G. KHAN: Found 4 tehsils
  -> FAISALABAD: Found 6 tehsils
  -> GUJRANWALA: Found 4 tehsils
  -> GUJRAT: Found 3 tehsils
  -> HAFIZABAD: Found 2 tehsils
  -> JHANG: Found 4 tehsils
  -> JHELUM: Found 4 tehsils
  -> KASUR: Found 4 tehsils
  -> KHANEWAL: Found 4 tehsils
  -> KHUSHAB: Found 4 tehsils
  -> LAHORE: Found 5 tehsils
  -> LAYYAH: Found 3 tehsils
  -> LODHRAN: Found 3 tehsils
  -> MANDI BAHA UD DIN: Found 3 tehsils
  -> MIANWALI: Found 3 tehsils
  -> MULTAN: Found 4 tehsils
  -> MUZAFFARGARH: Found 3 tehsils
  -> NANKANA SAHIB: Found 3 tehsils
  -> NAROWAL: Found 3 tehsils
  -> OKARA: Found 3 tehsils
  -> PAKPATTAN: Found 2 tehsils
  -> RAHIMYAR KHAN: Found 4 tehsils
  -> RAJANPUR: Found 3 tehsils
  -> RAWALPINDI: Found 5 tehsils
  -> SAHIWAL: Found 2 tehsils
  -> SARGODHA: Found 7 tehsils
  -> SHEIKHUPURA: Found 5 tehsils
  -> SIALKOT: Found 4 tehsils
  -> T.T.SINGH: Found 4 tehsils
  -> VEHARI: Found 3 tehsils
  -> KOT ADU: Found 2 tehsils
  -> MURREE: Found 2 tehsils
  -> TALAGANG: Found 2 tehsils
  -> WAZIRABAD: Found 1 tehsils

[Success] Mapped exactly 3410 Markazs.

Phase 1b: Fetching school lists across 3410 Markazs concurrently...
  -> Processed 200 / 3410 Markazs...
  -> Processed 400 / 3410 Markazs...
  -> Processed 600 / 3410 Markazs...
  -> Processed 800 / 3410 Markazs...
  -> Processed 1000 / 3410 Markazs...
  -> Processed 1200 / 3410 Markazs...
  -> Processed 1400 / 3410 Markazs...
  -> Processed 1600 / 3410 Markazs...
  -> Processed 1800 / 3410 Markazs...
  -> Processed 2000 / 3410 Markazs...
  -> Processed 2200 / 3410 Markazs...
  -> Processed 2400 / 3410 Markazs...
  -> Processed 2600 / 3410 Markazs...
  -> Processed 2800 / 3410 Markazs...
  -> Processed 3000 / 3410 Markazs...
  -> Processed 3200 / 3410 Markazs...
  -> Processed 3400 / 3410 Markazs...

Phase 1 Complete! Discovered exactly 38150 schools.

[TEST MODE] Limiting to first 50 of 38150 schools.

Phase 2: Fetching enrollment data concurrently...

=================================================================
[DEBUG] Raw grade bar response (first school):
{
  "male": [
    28,
    24,
    17,
    12,
    0,
    0,
    0,
    0,
    0,
    0
  ],
  "female": [
    28,
    31,
    34,
    33,
    35,
    27,
    22,
    24,
    30,
    17
  ],
  "other": [
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0
  ]
}
=================================================================

  -> Fetched data for 10 / 50 schools...
  -> Fetched data for 20 / 50 schools...
  -> Fetched data for 30 / 50 schools...
  -> Fetched data for 40 / 50 schools...
  -> Fetched data for 50 / 50 schools...

📊 Sanity check:
   50/50 schools have non-zero grade data
   0/50 schools have ECE students
   0/50 schools have Nursery students

📋 Sample — GGES (MC) MEHAR PURA (ID: 38796)
   Total: 362  Boys: 81  Girls: 281
   Grade       1: boys=28  girls=28
   Grade       2: boys=24  girls=31
   Grade       3: boys=17  girls=34
   Grade       4: boys=12  girls=33
   Grade       5: boys=0  girls=35
   Grade       6: boys=0  girls=27
   Grade       7: boys=0  girls=22
   Grade       8: boys=0  girls=24
   Grade       9: boys=0  girls=30
   Grade      10: boys=0  girls=17

✅ Finished in 2.7 minutes!
   → schools.csv  (rows: 50)
   → data.json

✅ Grade data looks good — remove [:50] for the full run!
no students data of Ece and nursery fethced.checked from website