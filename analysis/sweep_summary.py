import sys, pandas as pd
d=pd.read_csv(sys.argv[1]+'/requests.csv'); t=pd.read_csv(sys.argv[1]+'/telemetry.csv')
d['t']=(d.ts_ms-d.ts_ms.min())/1000; d=d[d.t>10]; ok=d[d.outcome.isin(['ok','not_found'])]
print("%s: rps %.0f goodput %.0f p50 %.0f p95 %.0f p99 %.0f | busy %.1f act %.1f pend %.1f wait %.1f gc %.0f pg_active %.1f"%(sys.argv[1].split('/')[-1], len(d)/(d.t.max()-10), len(ok)/(d.t.max()-10), ok.latency_ms.quantile(.5), ok.latency_ms.quantile(.95), ok.latency_ms.quantile(.99), t.ds_tomcat_busy.mean(), t.ds_hikari_active.mean(), t.ds_hikari_pending.mean(), t.ds_acquire_wait_mean_ms.mean(), t.ds_gc_pause_1s_ms.mean(), t.pg_active.mean()))
