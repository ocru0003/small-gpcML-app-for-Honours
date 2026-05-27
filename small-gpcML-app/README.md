# gpcML Validator Web App

Small deploy-ready FastAPI app for:

- validating gpcML XML files against the schema
- extracting contact, column, and eluent data
- plotting raw and processed chromatogram data

## Runtime

- Python 3.11 or newer
- Dependencies from `requirements.txt`

## Local run

```bash
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Then open `http://127.0.0.1:8000`.

## Deploy

- app entrypoint: `main:app`
- production command: `python -m uvicorn main:app --host 0.0.0.0 --port $PORT`
- health check: `/healthz`
- `Procfile` included for platforms that support it

## Small upload package

Clean upload bundle that excludes test data, caches, notes, and local junk:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_deploy_package.ps1
```

Creates:

- `dist\web-upload\`
- `dist\gpcml-validator-web.zip`

Only the files needed for deployment are included.
