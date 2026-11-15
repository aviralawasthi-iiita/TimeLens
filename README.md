# TimeLens

Design-time analysis of time rules in BPMN processes.

TimeLens reads a BPMN 2.0 model and a side file of time rules: task durations, gaps between the tasks, lags and deadlines. Some durations are **contingent**: the organization does not control them (for example, how long a customer takes to upload documents). TimeLens decides if every case can always meet its rules (**dynamic controllability**, DC). If not, it explains the conflict in business words and computes the cheapest repair with business levers.

```
 Event log (BPI 2017)          BPMN model + time rules (JSON)
        |                                 |
 [1] CALIBRATE  ---- duration ranges ---->|
                                    [2] MAP: BPMN -> STNU
                                    [3] CHECK: consistency, SC, DC
                     +--------------------+--------------------+
                     | DC                                      | NOT DC
              schedule, slack,                        [4] REPAIR: conflict ->
              critical path                               linear cut -> LP
                     +---------> [5] DECIDE + ALERT <----------+
              risk vs cost frontier, run-time alerts, replay on past cases
```

Steps 2 and 3 follow published methods (Vidal and Fargier 1999; Morris and Muscettola 2005). The contribution is the combination: duration ranges learned from a real log with a coverage level alpha as a risk knob, repair at minimum business cost with conflict-directed linear cuts (after CDRU by Yu, Fang and Williams, ICAPS 2014), a risk vs cost frontier, and run-time alert thresholds validated by replay.

## Install

Python 3.11 or later.

```bash
uv venv -p 3.12 .venv
uv pip install -p .venv -e ".[dev]"
source .venv/bin/activate
timelens --help
```

With plain pip: `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`.

Verification commands:

```bash
ruff check .
mypy src          # strict for timelens.stnu and timelens.analysis
pytest -q         # no network, fixed seeds
```

## Web UI

```bash
timelens ui                # opens http://localhost:8501 in the browser
timelens ui --port 8600
```

Start it from the project folder. Without a terminal, for example from a script, it starts headless and does not open the browser: open the URL it prints. The file pickers list the files in `examples/`, `config/`, `data/` and `results/`, and also accept an upload or a path on this computer. Use a path for large logs: the upload limit is 1 GB, and a path keeps the parquet cache next to the log.

| Page | Same as |
| --- | --- |
| Check rules | `timelens check`, with a chart of budget against demand and a worst-case schedule chart |
| Repair rules | `timelens repair`; saves the repaired rules to `results/` |
| Explore a log | `timelens discover-log` |
| Calibrate ranges | `timelens calibrate`; saves the calibrated rules to `results/` |
| Risk against cost | `timelens frontier` |
| Replay with alerts | `timelens alerts-eval` |
| Benchmark | `timelens bench` |

Every page uses the same library code as the CLI. Results have JSON, CSV or PNG downloads. The UI listens on `localhost` only, sends no usage statistics, and has no login: it is for one analyst on one computer. The rules and template pickers follow the folder of the chosen model until you pick a file yourself. After you save rules, the page links to the next step, which opens with those rules and the same model.

## Inputs

- **BPMN file.** Standard BPMN 2.0 XML with one process. Supported: one start event, one end event, intermediate events, tasks (all task types), parallel and exclusive gateways, sequence flows. The diagram part is ignored. Loops, other element types, and gateways that both split and join are rejected.
- **Rules file** (`*.rules.json`). Time rules keyed by BPMN element id. See `examples/loan_minimal/loan_minimal.rules.json`. Time values are `"90m"`, `"4h"`, `"2d"`, `"1w"` or plain integer minutes. Every task needs an entry.
- **Levers file** (`config/levers_*.yaml`). Business actions that change one time bound, with a cost per day and a maximum change.
- **Mapping file** (`config/mapping_bpi2017.yaml`). Maps log events to model timepoints.

Model a task done by people as `contingent`. Use `controllable` only when the organization truly fixes the duration.

## Commands

Exit codes: `0` success and DC, `1` success and NOT DC (or no repair), `2` input error, `3` internal error.

| Command | What it does |
| --- | --- |
| `timelens check MODEL.bpmn RULES.json [--json OUT] [--force-schedule]` | Consistency, strong and dynamic controllability. Worst-case schedule, slack and critical path if DC. If not DC, one conflict for every rule that fails in each scenario. |
| `timelens repair MODEL.bpmn RULES.json LEVERS.yaml [--json OUT] [--rules-out OUT] [--margin TIME]` | Cheapest lever changes that make the model DC. `--rules-out` writes the repaired rules. `--margin 4h` keeps at least 4 h of spare time on every deadline and lag maximum. |
| `timelens discover-log LOG` | Every activity with its lifecycle values and counts. Run it before you write a mapping. |
| `timelens calibrate MODEL.bpmn LOG MAPPING.yaml TEMPLATE.json --alpha 0.95 --out RULES.json [--deadline-days N]` | Duration ranges from the first 70 % of cases. Joint coverage on the last 30 %. Sets the deadline from the start event to the end event. |
| `timelens frontier MODEL.bpmn LOG MAPPING.yaml TEMPLATE.json LEVERS.yaml [--deadline-days N]` | Risk vs cost for alpha 0.80 to 0.99. Writes `results/frontier.csv` and `results/frontier.png`. |
| `timelens alerts-eval MODEL.bpmn RULES.json LOG MAPPING.yaml` | Replays the test cases with run-time alerts and a static baseline. Writes `results/alerts_eval.json` and `.md`. Pass repaired (DC) rules. |
| `timelens bench [--sizes 10,25,50] [--models 20] [--seed 7]` | Timing benchmark on random structured processes. Writes `results/bench.csv` and `results/bench.png`. |
| `timelens ui [--port 8501]` | Starts the web UI (see above). |

`LOG` is a `.xes` or `.xes.gz` file (read with pm4py), a `.csv` file with XES column names (`case:concept:name`, `concept:name`, `lifecycle:transition`, `time:timestamp` in ISO 8601), or a `.parquet` cache. XES and CSV logs are cached as parquet next to the file (`x.csv` -> `x.csv.parquet`).

## Example: `loan_minimal`

Upload documents (customer, 1 to 5 days), KYC check (staff, 1 to 2 days), approval (exactly 1 day), decision within 7 days.

```
$ timelens check examples/loan_minimal/loan_minimal.bpmn examples/loan_minimal/loan_minimal.rules.json
Model: loan_minimal  (XOR outcome known at: start)
Consistent:               yes
Strongly controllable:    no
Dynamically controllable: NOT_DC

Conflict in scenario 'default':
Not dynamically controllable.
Budget:  Decision within 7 days of application ........... 7 d
Demand:  Upload documents (customer), maximum ............ 5 d
         KYC check, maximum .............................. 2 d
         Approval, minimum ............................... 1 d
         Total demand .................................... 8 d
Shortfall: 1 d
```

```
$ timelens repair examples/loan_minimal/loan_minimal.bpmn examples/loan_minimal/loan_minimal.rules.json config/levers_loan.yaml
Repair: REPAIRED  (LP rounds: 2)
  Add one KYC officer [Task_KYC.max]: 2 d -> 1 d, cost 20,000 INR
  Total cost: 20,000 INR (assumption)
After repair:
Dynamically controllable: DC
  KYC check               start             5 d           5 d           0 m
Critical path: Task_Upload -> Task_KYC -> Task_Approval
```

Without the KYC lever, the repair sends reminders to the customer (upload maximum minus 1 d, 30,000 INR). Without any lever, the result is `NO_REPAIR`.

`loan_full` adds parallel credit scoring and fraud checks and an XOR choice between auto approval and manual review (`xor_outcome_known_at: "split"`). It is NOT DC with worst-case shortfalls of 1 d 7 h (auto approval) and 2 d 6 h (manual review). The repair with the three loan levers makes it DC: KYC maximum minus 1 d and upload maximum minus 1 d 6 h, 57,500 INR.

## Example: `claim_full`

An insurance claim with every kind of time rule (an original example, all values are assumptions). Register the claim, send an acknowledgement, then three parallel branches: the customer sends documents and staff check them, a policy coverage check, and a fraud screening. A claim-size choice follows (known at registration, `xor_outcome_known_at: "start"`): small claims get a fast-track assessment, large claims a site survey and a committee review. Then the decision and the payment.

| Rule | Type |
| --- | --- |
| Acknowledge within 1 day of receipt | deadline to a task end |
| Decision within 14 days of receipt | deadline to an intermediate event |
| Claim closed within 30 days of receipt | deadline to the end event |
| Payment starts 1 to 2 days after the decision | lag with min and max |
| Paid within 5 days of the decision | lag to a task end |
| Documents checked only after fraud screening | lag across parallel branches |
| Check starts within 1 day of the documents; committee 4 h to 2 d after the survey | flow gaps |

```
$ timelens check examples/claim_full/claim_full.bpmn examples/claim_full/claim_full.rules.json
Conflict in scenario 'Claim size?=Site survey by loss adjuster':
Budget:  Decision within 14 days of receipt .............. 14 d
Demand:  ... Total demand ................................ 19 d 8 h 5 m
Shortfall: 5 d 8 h 5 m
```

Small claims are safe (12 d 4 h 5 m in the worst case). With `config/levers_claim.yaml` the cheapest repair is an extra loss adjuster (survey maximum -2 d), an extra committee sitting (-2 d) and document reminders (-1 d 9 h), 131,250 INR. Extending the decision deadline costs more per day, so the repair does not use it.

## Example: `order_fulfilment`

A cross-border web shop order (an original example, all values are assumptions). It has 16 tasks, 10 gateways and 4 XOR scenarios, and the gateways nest three levels deep. After the card authorisation, three parallel branches run: create the invoice, review the order for fraud, and get the goods. In the goods branch an XOR choice decides between picking items in stock and a backorder. A backorder runs a parallel block: the supplier ships the missing items while the system tells the customer about the delay. After packing and payment capture, a destination choice decides between a domestic courier and an international shipment, which first prepares the customs declaration and the export screening in parallel. Stock and destination are known when the order is placed (`xor_outcome_known_at: "start"`).

| Rule | Type | Applies to |
| --- | --- | --- |
| Tell the customer about a backorder within 4 hours | deadline to a task end | backorders only |
| Start picking within 2 hours of the authorisation | lag with max | items in stock only |
| Pack only after the fraud review ends | lag across parallel branches | all orders |
| Capture the payment within 7 days of authorisation | lag with max (the authorisation expires) | all orders |
| Deliver domestic orders within 10 days | deadline to a task end | domestic only |
| Deliver international orders within 18 days | deadline to a task end | international only |
| Close the order within 30 days | deadline to the end event | all orders |
| Carrier collects 4 h to 2 d after the export papers; review request 2 to 3 days after delivery | flow gaps | international only; all orders |

A rule that names an element of an XOR branch applies only in the scenarios that take that branch.

```
$ timelens check examples/order_fulfilment/order_fulfilment.bpmn \
    examples/order_fulfilment/order_fulfilment.rules.json
Dynamically controllable: NOT_DC
Conflict in scenario 'Items in stock?=Backorder from supplier, Destination?=Print domestic label':
Budget:  Capture the payment within 7 days of authorisation (the authorisation expires) ... 7 d
Demand:  Supplier ships missing items (supplier), maximum ... 6 d
         Inspect supplier goods (warehouse), maximum ..... 1 d
         Pack parcel (warehouse), maximum ................ 2 h
         Total demand .................................... 7 d 2 h
Shortfall: 2 h
```

Orders with items in stock meet every rule. A backorder misses three rules in the worst case: the payment window by 2 h, the domestic delivery deadline by 3 h 31 m and the international delivery deadline by 2 d 7 h 30 m. The check lists all three: after each conflict it sets aside the rule that gave the budget and checks again, so every failing rule gets its own shortfall. With `config/levers_order.yaml` the cheapest repair takes 4 LP rounds and costs 13,000 INR: a priority customs broker (customs maximum -1 d), an extra inspection shift (-20 h) and express supplier shipping (-12 h). These three changes fix all three rules. After the repair, international backorders reach the customer 30 minutes before the deadline in the worst case. With `--margin 1h` every deadline and lag keeps at least 1 h of spare time: express supplier shipping grows to -13 h and the total to 13,333 INR.

## BPI Challenge 2017 workflow

Data: BPI Challenge 2017, loan applications of a Dutch financial institute, DOI [10.4121/uuid:5f3067df-f10b-45da-b98b-86ae4c7a310b](https://doi.org/10.4121/uuid:5f3067df-f10b-45da-b98b-86ae4c7a310b). Put `BPI Challenge 2017.xes.gz`, or a CSV export of it, in `data/` (not in git).

The results below use `data/bpi_2017_cleaned.csv`, a CSV copy from Kaggle. It has the same size as the official application log: 1,202,267 events, 31,509 cases, 26 activities, 1 January 2016 to 1 February 2017.

```bash
LOG="data/bpi_2017_cleaned.csv"     # or "data/BPI Challenge 2017.xes.gz"
timelens discover-log "$LOG"                       # check the activity names first
timelens calibrate examples/bpi2017/loan_bpi.bpmn "$LOG" config/mapping_bpi2017.yaml \
    examples/bpi2017/loan_bpi.rules.template.json --alpha 0.95 --out results/bpi.rules.json
timelens frontier examples/bpi2017/loan_bpi.bpmn "$LOG" config/mapping_bpi2017.yaml \
    examples/bpi2017/loan_bpi.rules.template.json config/levers_bpi2017.yaml
timelens repair examples/bpi2017/loan_bpi.bpmn results/bpi.rules.json \
    config/levers_bpi2017.yaml --rules-out results/bpi.repaired.json
timelens alerts-eval examples/bpi2017/loan_bpi.bpmn results/bpi.repaired.json \
    "$LOG" config/mapping_bpi2017.yaml
timelens bench
```

### Results on BPI 2017

All costs and lever limits are assumptions. Deadline: 36 d, the 90th percentile of training case durations (35 d 19 h 38 m) rounded up. No user choice was given.

Calibration at alpha 0.95 (22,056 training cases):

| Task | Samples | Dropped | Range |
| --- | --- | --- | --- |
| Complete application (staff) | 13,246 | 8,810 | 3 m to 3 d 12 h 5 m |
| Customer returns offer | 15,085 | 6,971 | 2 d 18 h 38 m to 28 d 3 h 9 m |
| Validate application (staff) | 7,951 | 14,105 | 0 m to 21 d 3 h 38 m |

Joint coverage on the test set: 0.878 (1,765 cases with every mapped event), so the risk is 12.2 %.

Risk vs cost frontier (`results/frontier.csv`, `results/frontier.png`):

| alpha | Risk | Verdict | Minimum repair cost |
| --- | --- | --- | --- |
| 0.80 | 60.3 % | DC | 0 |
| 0.85 | 49.0 % | DC | 0 |
| 0.90 | 36.9 % | NOT DC | 112,500 INR |
| 0.95 | 12.2 % | NOT DC | 519,583 INR |
| 0.99 | 2.7 % | NOT DC | no repair within the lever limits |

The repair at alpha 0.95 uses all four levers: completion maximum -2 d, customer return maximum -10 d, validation maximum -2 d, deadline +2 d 19 h.

Alert replay on the repaired alpha 0.95 rules (`results/alerts_eval.md`). It uses 6,554 test cases (301 late). A case needs only a case start and a case end; a missing task event stays pending. 98 cases have no end. 2,801 cases do not fit the model order and are counted apart: in the log, "Validate application" starts seconds before `O_Returned`, while the model puts validation after the customer return.

| Method | Precision | Recall | F1 | Median lead |
| --- | --- | --- | --- | --- |
| TimeLens latest safe start | 0.049 | 0.914 | 0.094 | 38 d 19 h |
| Static: no decision by deadline - 3 d | 0.695 | 1.000 | 0.820 | 3 d |

**TimeLens alerts do not beat the static rule on this model.** The minimum-cost repair leaves almost no slack (the latest safe start of "Complete application" is minutes after case creation), and the monitor assumes every pending contingent duration takes its repaired maximum, which is tighter than history. So 5,558 of 6,554 cases alert, most of them at once. Waits are not the cause: between the mapped events the log has almost none (`O_Sent` and the end of "Complete application" share a second), and a contingent wait from creation to the start of completion lowered precision to 0.042. Keeping slack in the repair (`repair --margin`, not yet measured on this log), or a monitor that uses observed duration quantiles instead of maxima, are open options.

Calibration splits cases by start time: the first 70 % train, the last 30 % test. Only contingent tasks are calibrated; controllable ranges stay as the template sets them. A task with fewer than 30 samples is not calibrated (warning). A mapped task that is not in the template, or a model task without a mapping in `alerts-eval`, is an input error. Range at coverage alpha: floor of the `(1 - alpha) / 2` quantile to ceil of the `1 - (1 - alpha) / 2` quantile. Risk is `1 - empirical joint coverage` on the test set (no independence formula).

## Assumptions

Every report states its assumptions in its header: date, inputs, alpha, deadline choice and the assumption note.

- **Costs.** All costs and lever limits are illustrative assumptions, not real bank data. They live only in `config/levers_*.yaml`. The BPI levers reuse the loan rates (staff 20,000, customer reminders 30,000, deadline extension 50,000 INR per day). Their `max_change` values are also assumptions.
- **BPI deadline.** The log has no official deadline. With `--deadline-days N` the user chooses it. Otherwise TimeLens uses the 90th percentile of training case durations, rounded up to whole days, and writes that reason into the rules `notes` and every report.
- **BPI mapping.** `config/mapping_bpi2017.yaml` uses the activity names of the build guide. They must be checked with `discover-log`. `calibrate` stops with an error if a mapped name or lifecycle value is not in the log. For `activity_any` lists, one present name is enough. Missing alternatives give a warning.
- **Benchmark generator.** One upper-bound lag per 10 tasks, with a bound of 1.0 to 1.5 times the worst-case gap. The deadline is 0.8 to 1.2 times the worst-case makespan.

## Limits

- Design-time analysis plus a replay of past cases. No live integration with a workflow engine.
- Acyclic models only. No loops.
- Elapsed calendar time. No working-day calendars.
- No resource constraints. Staff capacity appears only through the cost of an "add staff" lever.
- Tasks and intermediate events need exactly one incoming and one outgoing flow. Use gateways to split and join.
- With `xor_outcome_known_at: "split"`, per-scenario DC is necessary and union DC is sufficient. In between the verdict is `UNKNOWN_XOR`. The same rule applies to strong controllability. The CSTNU Tool oracle is not integrated.
- More than 256 XOR scenarios: only the union check runs.
- Provenance (conflict explanation and repair) is for networks up to 200 timepoints. Larger networks get verdicts and a deletion-filter list of the user constraints that keep them NOT DC, but no repair.
- Repair changes one parameter per lever and assumes a linear cost.
- The reported conflict is the most negative cycle that Floyd-Warshall closes first, not always the global worst cycle.
- `alerts-eval` supports only models without XOR choices: the replay uses one network for every case.
- The monitor watches task start timepoints. It assumes every contingent duration not yet observed takes its maximum, so it is conservative. If the repaired ranges are tighter than history, many past cases alert early.
- Calibrated ranges come from one bank and one year (2016). They may not transfer to other organizations.

## Notes on the build guide

These points differ from, or add to, `TimeLens_Build_Guide.md`:

1. Floyd-Warshall stops at the first negative diagonal entry of any node, not only the pivot. Otherwise a later pivot can double a negative cycle and its linear form.
2. The AllMax pass of the DC engine runs on the raw edge matrix, not on the closed one. The verdict is the same. The conflict found first is then the worst-case cycle, which gives the expected `loan_full` shortfalls.
3. Conflict text leaves out terms with value 0 (like `const.zero`). `loan_minimal` keeps the 4 h gap after KYC from Section 5.3, whose minimum is 0.
4. The deletion filter removes constraints, not timepoints, so the network never becomes small enough for provenance. TimeLens reports the kept constraints only.
5. `repair --rules-out` and the rules `notes` field are additions, so that `alerts-eval` can use repaired rules and reports can carry the deadline choice.
6. In the replay, a contingent link whose end is observed uses the observed time instead of its maximum.
7. Not built (OPTIONAL in the guide): the Morris 2014 O(n^3) engine, the UPPAAL export, and the CSTNU Tool oracle.

## Layout

```
config/            levers and log mapping (assumptions)
examples/          loan_minimal, loan_full, claim_full, order_fulfilment, bpi2017 template
src/timelens/
  model/           BPMN parser, rules schema, process graph and XOR scenarios
  stnu/            STNU, BPMN -> STNU mapping, STN tools, SC, DC
  analysis/        verdicts, schedule, conflicts, repair, frontier, alerts
  mining/          event log loading, mapping, calibration
  bench/           random process generator and timing benchmark
  report/          report text and plots
  ui/              Streamlit web UI: pages and file pickers
  workflow.py      steps shared by the CLI and the UI
tests/             pytest and hypothesis tests, slow reference DC engine
```

## References

1. M. Ocampo-Pineda, R. Posenato, F. Zerbato. TimeAwareBPMN-js. SoftwareX 17, 2022.
2. R. Posenato, M. Franceschetti, C. Combi, J. Eder. Introducing Agile Controllability in Temporal Business Processes. BPMDS 2024.
3. C. Dechsupa, W. Vatanawood, A. Thongtak. Timed-automata networks for timed business process models. Information Sciences 710, 2025.
4. R. Dechter, I. Meiri, J. Pearl. Temporal Constraint Networks. Artificial Intelligence 49, 1991.
5. T. Vidal, H. Fargier. Handling contingency in temporal constraint networks. JETAI, 1999.
6. P. Morris, N. Muscettola. Temporal Dynamic Controllability Revisited. AAAI 2005.
7. P. Yu, C. Fang, B. Williams. Resolving Uncontrollable Conditional Temporal Problems Using Continuous Relaxations. ICAPS 2014.
8. B. van Dongen. BPI Challenge 2017. 4TU.ResearchData, 2017.
