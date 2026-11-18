# Project 6 Grading Rubric (100 points)

**CSE 6332: Performance research on the Kafka → Spark Structured Streaming → S3 pipeline**

Students pick one research avenue (A–E), run its `bench` scenario twice (default settings and one setting changed), and submit three things: an **executive summary**, an **abstract**, and a **results ZIP** holding both `results/` folders. This is the authoritative grading rubric. It splits each criterion (40 / 40 / 20) into scored items so every grader awards points the same way. [docs/report_examples_and_rubric.md](docs/report_examples_and_rubric.md) summarizes it and shows a worked example of both documents.

## Summary

| Deliverable | Criterion | Points |
|---|---|---:|
| Executive summary | Answer first, quantified impact, recommendation, plain language | 40 |
| Abstract | Context, method, quantified results, supported conclusion | 40 |
| Results ZIP and experiment | Hypothesis, one-setting design, traceable evidence | 20 |
| **Total** | | **100** |
| Deductions | Length out of range (−5 per document); untraceable or copied numbers (−10 each); late (−10% per day) | |

## 1. Executive summary (40 points)

Reader: a manager who reads nothing else. **Required length: 150–250 words** (about half a page); see Deductions.

| Item | Pts | Full credit | Partial credit | No credit |
|---|---:|---|---|---|
| 1a. Answer in the first sentence | 10 | The first sentence states the finding and what it means (for example "not worth it", "doubles capacity") | Finding stated, but only after background (6) | No clear finding; describes activities ("we ran tests") (0–2) |
| 1b. Quantified impact bullets | 15 | 2–4 bullets, each with a rounded headline number tied to capacity, cost or risk | Numbers present but not tied to impact, or only one bullet (9) | No numbers, or bullets restate the method (0–5) |
| 1c. Recommendation | 10 | One clear, actionable recommendation that follows from the numbers, with its condition or trade-off | Recommendation present but vague or weakly justified (6) | Missing (0) |
| 1d. Concise, plain language | 5 | No jargon or filler; every sentence carries a finding, a number or the recommendation | Some jargon or padding (3) | Flowery or technical prose that buries the recommendation, or a copy of the abstract (0) |

## 2. Abstract (40 points)

Reader: a technical reader deciding whether to read further. **Required length: 150–300 words, at most 3 paragraphs**; see Deductions.

| Item | Pts | Full credit | Partial credit | No credit |
|---|---:|---|---|---|
| 2a. Context and research question | 8 | Why the topic matters in streaming or cloud systems, and the specific question asked | Question present, context thin (5) | No question stated (0–2) |
| 2b. Method | 12 | Names the scenario, the one setting changed and its two values, the load steps, and the measures used (for example sustainable throughput, p95/p99 latency, files per step) | Setting named but measurement or conditions unclear (7) | Method missing (0–4) |
| 2c. Results | 12 | Specific numbers for both runs, with the conditions they were measured under | Some results unquantified, or only the changed run reported (7) | No numbers (0–4) |
| 2d. Conclusion and concision | 8 | Conclusion follows from the data, is limited to this workload and machine, says why it matters; no filler | Over-claims, or some padding (5) | No conclusion, or reads like an introduction (0–2) |

## 3. Experiment and evidence (20 points)

| Item | Pts | Full credit | Partial credit | No credit |
|---|---:|---|---|---|
| 3a. Hypothesis | 5 | A prediction and its reason, written before the runs (stated in the abstract or as one line at the top of the submission) | Hypothesis present but no reasoning (3) | Missing (0) |
| 3b. One-setting design | 8 | Exactly one setting differs between the default and changed runs; both are full-length runs (60 s steps, not `--quick`); runs labelled | Correct design, minor issue such as a `--quick` run or unclear labels (5) | More than one setting changed, or no default run (0–2) |
| 3c. Results ZIP and traceability | 7 | ZIP holds both `results/<date>_<scenario>_<label>/` folders, and every reported number is in their `report.md`, `steps.csv` or `audit.json` | A file is missing, or a few numbers are hard to locate (4) | Folders missing (0) |

## Deductions

This project is about being concise, succinct and accurate. No journal editor spends publishing space on fluff, and no executive wades through prose to reach the recommendation. Length limits are therefore enforced in both directions.

- **−5 per document:** executive summary outside 150–250 words, or abstract outside 150–300 words or longer than 3 paragraphs. Too short is penalized the same as too long.
- **−10:** a reported number cannot be found in the submitted `results/` folders.
- **−10:** numbers copied from the Topic Z example or from another student.

## Submission and late policy

- **Due:** December 9, 2026 at 11:59:59 PM, submitted in **Canvas**. Email submissions are not accepted.
- **Late penalty:** 10% per day, starting at 12:00:00 AM on December 10, 2026.

## Grader checklist

1. Diff the two `config.json` files: exactly one setting should differ (3b).
2. In each `report.md`, check the steps row shows 60 s steps, not 30 s from `--quick` (3b).
3. Spot-check three numbers from each document against `report.md` or `steps.csv` (3c and deductions).
4. Check data accounting (PASS/FAIL) in `report.md` if the student makes any claim about loss or duplicates.
5. Count words in both documents and paragraphs in the abstract (length deductions).
