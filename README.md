# NodeLoc Public Archiver

A conservative Python archiver for publicly accessible NodeLoc topics. It discovers topic URLs through the site's published sitemap and saves each topic's raw Discourse JSON plus checksummed metadata.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item config.example.yml config.yml
```

Adjust `max_topics` and the request delay in `config.yml`, then run:

```powershell
.venv\Scripts\python archive.py --config config.yml --max-topics 50 --seed 20261004
```

Archived data is written under `data/topics/<topic-id>/`.

Each JSON topic saves `topic-base.json` first. If `post_stream.stream` contains more IDs than the first response, the archiver fetches the missing public posts through `/t/{id}/posts.json?post_ids[]=...` in chunks. Restricted topics may return only a permission placeholder; those are marked `restricted=true` and are not treated as incomplete.

## Verification

```powershell
.venv\Scripts\python -m unittest -v test_archive.py
```

Sample verification output is stored in `data/sample-report.json`, with a readable summary in `SAMPLE_REPORT.md`.

## Concurrency

Set `concurrency` in `config.yml` (default 1). Each worker keeps the randomized 1-3 second delay; 429 responses are retried with `Retry-After` backoff. A value of 4 was tested without throttling on the public sitemap-discovered topics.

## Full-site background run

`run_full.ps1` runs repeated passes over every sitemap-discovered topic until all are archived, restarting automatically after an interrupted pass. It is resumable: completed and unchanged topics are skipped.

```powershell
Start-Process powershell -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","$PWD\run_full.ps1" -WindowStyle Hidden
```

Monitor and stop:

```powershell
Get-Content .\data\run.log -Tail 20
Get-Process -Id (Get-Content .\data\run.pid)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*archive.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Stop-Process -Id (Get-Content .\data\run.pid) -Force
```

## Responsible use

- Archives only public pages discovered through the published sitemap.
- Does not authenticate, bypass access controls, or call private endpoints.
- Uses bounded retries, configurable delays, and modest concurrency.
- Review the site's current terms and robots policy before large-scale use.
- Set `max_topics` conservatively during testing.
