# Golden Data Issues — `Loss Runs GTs.xlsx`

Findings from comparing the 92 golden rows against the five source PDFs in `samples/`.

## Bottom Line

- **`Loss Run_Report.pdf` has the most hard errors** — wrong claim IDs, wrong GUARD policy/claim IDs, claimant/adjuster swaps, and several bad dates/statuses.
- **FCCI LRs goldens systematically put Paid Medical into Indemnity Paid** for WCC medical-only claims (27 rows). Claim totals are still correct.
- **Excel numeric coercion** strips leading zeros on Chubb claim and occurrence IDs.
- **`Chubb_Loss-1.pdf` and `Loss-4.pdf` are mostly correct**; issues are formatting / inference, not wrong claim sets.

---

## Highest-priority corrections

1. `EE87100` → `E8F7100` (+ description bollard / IVD)
2. GUARD `NEWC003132` → `NEWC030132` (and claim IDs)
3. GUARD claimant names on both WC policies (many look like adjusters)
4. `NEWC997607-004` status/dates (should be closed; loss date `05/16`)
5. WCC medical → Indemnity Paid mapping on the FCCI LRs golden (decide if intentional)
6. Excel number formatting stripping leading zeros on Chubb claim IDs

---

## 1. `Loss Run_Report.pdf` (26 rows)

### Travelers claim `EE87100`

| Field | Golden | PDF |
| --- | --- | --- |
| Claim ID | `EE87100` | `E8F7100` |
| Loss Description | `CONCRETE DOOR JAMB` / `(TVD)` | `CONCRETE BOLLARD` / `(IVD)` |

Other fields for this row (insured, policy `8E146593`, dates, paid `40939.40`) match.

### GUARD policy `NEWC003132` (4 rows)

| Issue | Golden | PDF |
| --- | --- | --- |
| Policy Number | `NEWC003132` | `NEWC030132` |
| Claim IDs | `NEWC003132-00x` | `NEWC030132-00x` |

Claimant errors (names appear swapped with adjusters / wrong):

| Claim | Golden Claimant | PDF (approx.) |
| --- | --- | --- |
| `-001` | `Kreutzer, John` | Claimant ≈ Swepson/Tangie; adjuster ≈ John Kreuzer/Kreutzer |
| `-002` | `Hernandez, Pedro` | Solo/Solio, Michael |
| `-004` | `Drew, Maya` | Drew, Kayla |

Expense paid vs reserved on `-003` / `-004`:

| Field | Golden | PDF |
| --- | --- | --- |
| Expense Paid | `125` | `0` |
| Expense Reserved / Outstanding | `0` | `125` |

### GUARD policy `NEWC997607` (5 rows)

Claimants appear largely wrong (several look like adjuster names):

| Claim | Golden Claimant | PDF (approx.) |
| --- | --- | --- |
| `-001` | `Hacking, Dan` | Stark, Keaghon (GT may have used adjuster Dan …) |
| `-002` | `Myers, Katherine` | Kelly, James |
| `-004` | `Warren, Kimberly` | Sublett, James |
| `-005` | `Anderson, Andrea` | Kelley, James (GT may have used adjuster Andrea Anzalone) |

Date / status errors:

| Claim | Golden | PDF |
| --- | --- | --- |
| `-001` Accident | `09/04/2018` | `09/14/2018` |
| `-001` Reported | `09/04/2018` | ~`10/10/2018` |
| `-004` Accident | `05/26/2019` | `05/16/2019` |
| `-004` Status | `OPEN` (no closed date) | `CLOSED` (~`10/28/2019`) |
| `-005` Accident | `06/13/2019` | Loss `06/07/2019` (GT used received date) |

Financial notes:

| Claim | Issue |
| --- | --- |
| `-002` | Indemnity Paid `2865.42` vs PDF ~`2856.47`; Paid Medical ~`1985.21` not reflected in indemnity |
| `-003` | Reserves look OK (`IndR=7592.5`, `ExpR≈6288`); Expense Paid may be `1211.77` vs PDF `1211.68` |

### Chubb `N0097982A` rows

- Claim IDs lost leading zeros (`052805` → `52805`, `008250` → `8250`, etc.) — Excel numeric coercion.
- Occurrence IDs present in PDF (`JY13J0528055`, etc.) but blank in GT.
- `*` and `$16; ($16)` style amounts match Chubb’s A/Z recovery notation — OK as literal captures.

---

## 2. `LRs_Application_CAU CPP MAR PKG WCO Loss Runs.PDF` (51 rows)

- **Row set is complete** — all 51 claim IDs match the PDF.
- Claim totals and expense paid look good for the checked rows.

### Systematic financial mapping (27 WCC medical-only rows)

GT puts PDF **Paid Medical** into **Indemnity Paid**, while PDF **Paid Loss (Indemnity) = $0**.

Example `C00317085-01`:

| Field | Golden | PDF |
| --- | --- | --- |
| Indemnity Paid | `975.39` | Paid Loss (Indemnity) `0.00` |
| (not captured separately) | — | Paid Medical `975.39` |
| Expense Paid | `71.27` | `71.27` |
| Claim Total | `1046.66` | `1046.66` |

Same pattern on Diggs `-01`, Curet, Fig, Heinel, Lawless `-01`, Bare, Debenedett `-01`, Marichal medical rows, Vitale `-01`, and other WCC MO claims.

> Schema text says indemnity can include medical, so this may be intentional — but it does **not** match the PDF’s “Paid Loss (Indemnity)” column.

### Other

- Carrier `FCCI` is not in the text layer (likely logo-only).

---

## 3. `Loss_2.pdf` (12 rows)

### CRC / page-1 claim `CLM30293`

| Field | Golden | PDF |
| --- | --- | --- |
| Valuation Date | `12/11/2020` | Loss Run Dated `10/31/2020` (`12/11` is print date) |
| Carrier | `CRC Group shown as broker; carrier/provider ` | Truncated / incomplete |

Claim ID, occurrence `4164658`, expense `3200.68`, and description otherwise match.

### Velocity row (policy `793`)

| Field | Golden | PDF |
| --- | --- | --- |
| Policy Number | `793` | `0000793` |
| Expense Paid | `3705.58` | Split: Expense Paid `$20.30` + Unallocated Expense Paid `$3,685.28` (total incurred `$3,705.58`) |

### Swiss Re rows

- LOB `Global Loss Run` is the report title, not a line of business.
- Valuation Date blank; PDF has Report Run Date `11-Dec-2020`.

### Status enrichments

Several rows use `C (Closed)` where the PDF only has `C`.

---

## 4. `Loss-4.pdf` (1 row)

Mostly correct. Flags:

- Carrier `TRAVELERS` not in text (logo inference only).
- Status `C (Closed)` vs PDF `C`.

---

## 5. `Chubb_Loss-1.pdf` (2 rows)

Mostly correct. Flags:

| Field | Golden | PDF |
| --- | --- | --- |
| Claim IDs | `40512146091` / `40515077700` | `040512146091` / `040515077700` |
| Occurrence IDs | `2` / `3` | `000002` / `000003` |

Insured, valuation date, policy term, LOB, financials, and descriptions match.

---

## Scope covered

| Source PDF | Golden rows | Verdict |
| --- | --- | --- |
| `LRs_Application_CAU CPP MAR PKG WCO Loss Runs.PDF` | 51 | Complete row set; medical→indemnity mapping issue |
| `Loss Run_Report.pdf` | 26 | Multiple hard errors (IDs, names, dates, status) |
| `Loss_2.pdf` | 12 | Valuation / policy / expense-split / LOB issues |
| `Chubb_Loss-1.pdf` | 2 | Leading-zero formatting only |
| `Loss-4.pdf` | 1 | Carrier inference + status enrichment |
| **Total** | **92** | |
