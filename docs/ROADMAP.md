# Roadmap aggiornata dopo alpha45.9

## Stato validato

### Già completato

- alpha28: cost-aware planner iniziale.
- alpha29: scenario planner / astro scenario.
- alpha31: corpus benchmark.
- alpha33: HTTP pilot.
- alpha34: mirror robustness.
- alpha35: object-store pilot.
- alpha36: signed URL policy.
- alpha37–40: MinIO/S3/cost matrix.
- alpha41: anti-cherrypick / negative tests.
- alpha42: GitHub report.
- alpha43: comparison harness.
- alpha44.3: rsync checksum baseline validato.
- alpha45.8 fresh: zsync vs CLD2 same-run validato.
- alpha45.9: consolidation report completato.

## Risultato consolidato

CLD2 è promettente come **cost-aware distribution planner** sopra chunk/object/CAS/delta, non come compressore generico.

Forza reale:
- update di release;
- utenti già con cache/chunk precedenti;
- distribuzioni multi-file o contenuti con alto riuso;
- report FinOps/devtool/GitHub vetrina;
- supporto enterprise/CI-CD/packaging.

Limiti:
- cold start non è il caso forte;
- high entropy riduce vantaggio;
- heavy change quasi pari a zsync;
- dataset-dependent;
- benchmark locali non equivalgono a CDN reale.

## Prossimi step

### alpha46 — casync/desync baseline

Obiettivo:
- aggiungere baseline `casync` o `desync` dove installabile.
- confrontare contro CLD2 su stessi scenari:
  - normal
  - high-entropy
  - small-files
  - heavy-change

Claim boundary:
- se non installabile su Windows/WSL in modo pulito, fare probe/preflight e dichiarare limite.
- non forzare risultati.

### alpha47 — ostree baseline

Obiettivo:
- baseline content-addressed/update repository.
- molto rilevante perché concettualmente più vicino a CLD2 rispetto a zsync.

### alpha48 — DVC/lakeFS/Xet

Obiettivo:
- probabilmente non benchmark diretto completo, ma confronto architetturale + micro-probe se installabile.
- usarlo per posizionamento mercato/devtool.

### alpha49 — Docker/OCI layering

Obiettivo:
- confrontare layering/layer reuse vs CLD2 per distribuzione artefatti.
- importante per GitHub e mercato devops.

### alpha50 — GitHub/public pack

Obiettivo:
- README onesto.
- benchmark report.
- quickstart.
- examples.
- claim boundary.
- donation/sponsor non paywall obbligatorio.

## Monetizzazione consigliata

Non mettere paywall obbligatorio “1€/anno o 5€ forever” per scaricare da GitHub.

Meglio:
- open source / MIT-licensed con GitHub Sponsors;
- donazioni libere;
- supporto enterprise;
- integrazioni CI/CD;
- consulenza;
- managed packing/distribution;
- report FinOps custom.

