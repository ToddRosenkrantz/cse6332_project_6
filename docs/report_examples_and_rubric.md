# Report Examples and Grading Rubric

This page shows **what a good executive summary and abstract look like** and **how they will be graded**.

> **The example uses a fictitious topic, "Z", that is not one of the avenues** in [research_projects.md](research_projects.md). Its findings and numbers are invented. It demonstrates *form*: structure, tone and level of detail. Your documents must be about your own avenue (A–E) and your own two runs.

## The two documents

| | Executive summary | Abstract |
|---|---|---|
| **Reader** | A manager or decision-maker who may read nothing else | A technical reader deciding whether to read your paper |
| **Opens with** | The answer, and what it means for the business | The context and the research question |
| **Language** | Plain; capacity, cost and risk; no jargon | Precise; the setting changed, how it was measured, quantitative results |
| **Length** | **150–250 words** (about half a page): a short opening paragraph, 2–4 bullets, 1 recommendation | **150–300 words, at most 3 paragraphs**, like a published article's abstract |
| **Numbers** | A few headline figures, rounded | Specific results with the conditions they were measured under |
| **Ends with** | A clear recommendation | The conclusion and why it matters |

---

## Example: Topic Z (fictitious), how often should the pipeline be monitored?

**Question:** Does collecting monitoring metrics every second, instead of the default every five seconds, slow the pipeline down, and is it worth it?

**Experiment:** one scenario run twice: default (metrics every 5 s) and changed (metrics every 1 s). Everything else stayed the same.

### Example executive summary

Collecting monitoring data five times more often is **not worth it**: it reduced how much data the pipeline could handle by about **11%** and grew monitoring storage fivefold, without revealing anything the default setting missed. We tested it because faster monitoring is often assumed to be free and to catch problems sooner.

- **Capacity:** the pipeline kept up with about 2,850 events per second with 1-second monitoring, against 3,200 with the default 5-second monitoring. On the same hardware, about one ninth of the capacity went to monitoring alone.
- **Storage cost:** monitoring data grew from about 240 MB to 1.2 GB per day, so a month of history needs about 36 GB instead of 7 GB.
- **Speed for users:** a typical event still reached storage in about 0.4 seconds with either setting, so faster monitoring did not make the pipeline more responsive.
- **Visibility:** both settings showed the same slowdown when the load exceeded capacity; the extra detail added no new insight.

**Recommendation:** keep the default 5-second monitoring in production, and switch to 1-second monitoring only temporarily while investigating a specific problem, accepting the lower capacity for that period.

### Example abstract

Observability is essential for operating distributed data pipelines, but collecting metrics uses the same CPU, memory and network resources as the workload being observed. We asked whether increasing the metric collection frequency changes the sustainable throughput of a Kafka → Spark Structured Streaming → S3 pipeline monitored by Prometheus.

We ran the same step-load experiment (offered load stepped from 400 to 4,000 events per second per topic, 60 seconds per step) twice on one machine: with the default 5-second scrape interval, and with a 1-second interval. A step was considered sustained when the Kafka backlog did not grow. We compared sustainable throughput, end-to-end latency percentiles, per-container CPU usage, and monitoring storage growth.

The 1-second interval reduced sustainable throughput by 11% (from 3,200 to 2,850 events per second), raised Prometheus CPU usage from 0.1 to 0.3 cores, and increased monitoring storage fivefold, while median end-to-end latency was unchanged at about 0.4 seconds. Both configurations detected the same saturation point. We conclude that high-frequency monitoring carries a measurable capacity and storage cost without improving diagnosis for this workload, so moderate scrape intervals are the better default.

### Why these examples work

- **The executive summary answers the question in its first sentence** ("not worth it"), translates results into consequences (capacity, storage cost, visibility), and ends with a decision.
- **The abstract uses three paragraphs,** like many published abstracts: (1) context and question, (2) what was changed and how it was measured, (3) results with numbers and the conclusion.
- **Both tell the same story** from the same two runs, at two levels of detail for two different readers.
- **Neither over-claims.** The conclusion is limited to "this workload" and one machine.
- **Both are within the length limits:** the executive summary is about 190 words and the abstract about 185 words, in 3 paragraphs.

---

## Grading rubric (100 points)

The full rubric, with the scored items, partial-credit levels and the grader checklist, is **[rubric.md](../rubric.md)**. In summary:

| Deliverable | Criterion | Points |
|---|---|---:|
| **Executive summary** | Answer in the first sentence (10); 2–4 quantified impact bullets (15); a clear recommendation with its condition or trade-off (10); concise, plain language (5) | 40 |
| **Abstract** | Context and research question (8); method: scenario, the one setting changed and its two values, load steps, measures (12); specific results for both runs (12); a supported conclusion, no filler (8) | 40 |
| **Experiment and evidence** | Hypothesis with its reason, written before the runs (5); exactly one setting changed, full-length runs (60 s steps, not `--quick`), runs labelled (8); results ZIP with both `results/` folders, every number traceable (7) | 20 |
| **Total** | | **100** |

**Required length** (enforced both ways: too short is penalized the same as too long):
- Executive summary: **150–250 words**.
- Abstract: **150–300 words, at most 3 paragraphs**.

**Deductions:**
- **−5 per document** outside its required length.
- **−10:** a reported number cannot be found in your `results/` folders.
- **−10:** numbers copied from these examples or from another student.

**Submission:** in **Canvas** by **December 9, 2026 at 11:59:59 PM**. Email submissions are not accepted. Late work loses **10% per day**, starting at 12:00:00 AM on December 10, 2026.
