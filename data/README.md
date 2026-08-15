# Data directory

Nothing in `raw/` or `processed/` is committed to git — the raw CSV alone is 208 MB.
This file explains how to obtain and verify it.

## Layout

| Path | Contents | Committed? |
|---|---|---|
| `raw/` | The unmodified source archive and its extracted contents | No |
| `processed/` | Intermediate artefacts produced by the pipeline (Parquet, chunk manifests) | No |

## Obtaining the dataset

**Source:** UCI Machine Learning Repository, dataset 791 — *MetroPT-3 Dataset*
DOI `10.24432/C5VW3R` · https://archive.ics.uci.edu/dataset/791/metropt+3+dataset

```bash
curl -L --retry 6 --retry-all-errors --retry-delay 5 \
     --speed-limit 1024 --speed-time 60 \
     -o data/raw/metropt3.zip \
     "https://archive.ics.uci.edu/static/public/791/metropt+3+dataset.zip"
```

The retry flags are not decoration. The endpoint sends no `Content-Length` and drops
connections mid-stream; a truncated download still looks like a valid ZIP file. See
`docs/DEV_LOG.md` DL-004 and DL-006.

Extract with `scripts/setup.ps1`, or manually — note that PowerShell 5.1 needs the
per-entry form, see DL-007.

## Expected contents and verification

| File | Size (bytes) | Notes |
|---|---|---|
| `metropt3.zip` | 218,381,995 | Stored uncompressed, so the archive is barely larger than the CSV |
| `MetroPT3(AirCompressor).csv` | 218,300,507 | 1,516,948 data rows + 1 header |
| `Data Description_Metro.pdf` | 81,208 | Dataset description from the authors |

Verified hashes of the copy this project was built against:

| Artefact | SHA-256 |
|---|---|
| `metropt3.zip` | `aab991a970e58210de853bb8078ce0e63abb4d9412fdc5c79792dae3d8e1721a` |
| `MetroPT3(AirCompressor).csv` | `db30ccb4ea402e3c8bf2c99db06e288d4f2a772f6928f9dbe26a920d69793e24` |

```powershell
Get-FileHash -Algorithm SHA256 data\raw\MetroPT3(AirCompressor).csv
```

A mismatch means either a truncated download or a newer publication from UCI. Either
way, re-run `scripts/profile_dataset.py` before trusting anything downstream — the
profile is what every threshold in the system is derived from.

## Configuration

The pipeline never hard-codes these paths. Set `METROPT_RAW_CSV` in `.env`, or pass
`--csv`. The default is `data/raw/MetroPT3(AirCompressor).csv` relative to the
repository root.

## Licence and citation

The dataset is redistributed by UCI under its own terms; it is **not** re-published in
this repository. Cite the two papers listed in `docs/DATA_DICTIONARY.md` §4 for any use
of this data.
