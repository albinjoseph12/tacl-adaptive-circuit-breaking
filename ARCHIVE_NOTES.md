# TACL replication package — archive notes (rebuilt 2026-11-18)
Contents: gateway and downstream services (`services/`), fault injectors, both load generators (`load/`: closed-loop Locust and open-loop `open_loop_gen.py`), telemetry, run scripts, `results/` (all 138 closed-loop and 114 open-loop runs with raw per-request logs and 100 ms timelines, plus `results/analysis/` derived tables), `analysis/` scripts (incl. `analyze_openloop.py`) and `paper/` (final manuscript source, PDF, DOCX).
Regenerate the manuscript text from the data: `python3 paper/render.py paper/paper_full.md.tmpl out.md` (0 unresolved placeholders expected).
Compared with the first archive, only `paper/` changed (final manuscript text); no result file or code changed.

Update (2026-11-19, submission version for Software: Practice and Experience): `paper/` holds the final manuscript. Figures 2-5 are the ones analysis/analyze.py generates from results/ (results/analysis/fig_*.png); the renderer's sensitivity range excludes the ablations; a new open-loop summary table is rendered from results/analysis/openloop_issue_cohort.csv.

Update (2026-11-20, open-loop re-run and clock-alignment fix):
- `openloop_rerun/`: a second, instrumented open-loop campaign (190 runs, 5 reps; GCP, same platform as the original).
  Contains its driver (`campaign_on_loadgen.sh`), VM setup scripts and logs, the instrumented generator
  (`open_loop_gen_instrumented.py`, which differs from `load/open_loop_gen.py` only by the `sent_ts_ms` column), an
  independent checking script, `README.md` and `RESULTS.md`.
- `results_openloop_rerun/analysis/`: its derived tables, written by `analysis/analyze_openloop.py results_openloop_rerun`.
  The raw per-request logs and telemetry of those 190 runs (about 2.2 GB) are in the separate archive
  `tacl_openloop_rerun_raw_2026-11-20.tar.gz`. Extract it next to this one so that `results_openloop_rerun/` holds both.
- `analysis/analyze_openloop.py` corrected. Each event window now uses its own clock:
  - flash crowd: the generator's clock;
  - faults: the server's clock.
  Earlier the two were mixed (details in `openloop_rerun/RESULTS.md`, section 1). The issue-cohort rate now divides
  by the number of reps present.
  `results/analysis/openloop_*` were regenerated with it. The previous tables are kept in `results/analysis/_pre_align/`.
- `paper/`: Section V-I, Table V, the abstract, and the discussion, conclusion and data-availability passages updated.
  The new render sources `rol*` read `results_openloop_rerun/analysis/`.
