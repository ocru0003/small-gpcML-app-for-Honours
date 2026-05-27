from typing import Optional

from fastapi import FastAPI, UploadFile, File, Request, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import time, secrets
from html import escape
from io import BytesIO
from pathlib import Path

from lxml import etree

from validators.extract import extract_from_many
from validators.rules import run_custom_rules
from plotter import build_session_from_gpcml, render_plot_pdf, render_plot_png  # adjust import path if needed


app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
XSD_PATH = BASE_DIR / "schema.xsd"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Load schema once
with open(XSD_PATH, "rb") as f:
    schema_root = etree.XML(f.read())
    schema = etree.XMLSchema(schema_root)


# ---------------------------
# Pages
# ---------------------------

@app.get("/", response_class=HTMLResponse)
def homepage(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "result": None}
    )


@app.get("/extract", response_class=HTMLResponse)
def extractor_page(request: Request):
    return templates.TemplateResponse(
        "extract.html",
        {"request": request, "tables": None, "errors": None},
    )


@app.get("/rawplot", response_class=HTMLResponse)
def rawplot_page(request: Request):
    return templates.TemplateResponse("rawplot.html", {"request": request})


@app.get("/information", response_class=HTMLResponse)
def information_page(request: Request):
    # Content is edited in templates/info_content.html (no backend restart required after edit)
    return templates.TemplateResponse("information.html", {"request": request})


@app.get("/healthz")
def healthcheck():
    return {"status": "ok"}


# ---------------------------
# Validator
# ---------------------------

@app.post("/validate", response_class=HTMLResponse)
async def validate(request: Request, xml_file: UploadFile = File(...)):
    xml_bytes = await xml_file.read()

    try:
        # Parse using etree.parse so line numbers are tracked properly
        doc = etree.parse(BytesIO(xml_bytes))
        root = doc.getroot()

        # Phase 1: XSD validation (no exceptions needed)
        is_valid = schema.validate(doc)

        if not is_valid:
            # Pull schema errors WITH line/column info
            errors = []
            for err in schema.error_log:
                errors.append(f"Line {err.line}, Col {err.column}: {err.message}")

            return templates.TemplateResponse(
                "index.html",
                {"request": request, "result": {"valid": False, "message": "\n".join(errors)}},
            )

        # Phase 2: your custom rules (these won't have schema line numbers automatically)
        custom_errors = run_custom_rules(root)
        if custom_errors:
            return templates.TemplateResponse(
                "index.html",
                {"request": request, "result": {"valid": False, "message": "\n".join(custom_errors)}},
            )

        return templates.TemplateResponse(
            "index.html",
            {"request": request, "result": {"valid": True, "message": "XML is valid."}},
        )

    except etree.XMLSyntaxError as e:
        # Pure XML well-formedness errors (these DO have line/column)
        errors = [f"Line {err.line}, Col {err.column}: {err.message}" for err in e.error_log]
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "result": {"valid": False, "message": "\n".join(errors)}},
        )


# ---------------------------
# Extractor
# ---------------------------

@app.post("/extract", response_class=HTMLResponse)
async def extractor_run(request: Request, xml_files: list[UploadFile] = File(...)):
    files = []
    for f in xml_files:
        files.append((f.filename, await f.read()))

    contacts_df, columns_df, eluent_df, errors = extract_from_many(files)

    def rows_to_html(rows: list[dict]) -> str:
        if not rows:
            return "<div class='empty'>No rows found.</div>"
        columns = list(rows[0].keys())
        head = "".join(f"<th>{escape(str(col))}</th>" for col in columns)
        body_rows = []
        for row in rows:
            cells = "".join(
                f"<td>{escape('' if row.get(col) is None else str(row.get(col)))}</td>"
                for col in columns
            )
            body_rows.append(f"<tr>{cells}</tr>")
        return (
            '<table class="table">'
            f"<thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body_rows)}</tbody>"
            "</table>"
        )

    tables = {
        "contacts": rows_to_html(contacts_df),
        "columns": rows_to_html(columns_df),
        "eluent": rows_to_html(eluent_df),
    }

    return templates.TemplateResponse(
        "extract.html",
        {
            "request": request,
            "tables": tables,
            "errors": errors,
        },
    )


# ---------------------------
# RawPlot (server-side plotting)
# ---------------------------

_PLOT_CACHE = {}  # token -> {"session": PlotSession, "t": unix_time}
_PLOT_TTL_S = 60 * 30  # 30 minutes

def _cache_gc():
    now = time.time()
    dead = [k for k, v in _PLOT_CACHE.items() if now - v["t"] > _PLOT_TTL_S]
    for k in dead:
        _PLOT_CACHE.pop(k, None)

@app.post("/rawplot/upload")
async def rawplot_upload(
    file: Optional[UploadFile] = File(None),
    xml_file: Optional[UploadFile] = File(None),
):
    upload = file or xml_file
    if upload is None:
        raise HTTPException(status_code=400, detail="No XML file was uploaded.")

    try:
        xml_bytes = await upload.read()
        session = build_session_from_gpcml(xml_bytes)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    token = secrets.token_urlsafe(16)
    _PLOT_CACHE[token] = {"session": session, "t": time.time()}
    _cache_gc()

    return {
        "token": token,
        "summary": session.summary,
        "available_plots": session.available_plots,
        "warnings": session.warnings,
    }

@app.get("/rawplot/plot/{token}/{kind}.png")
def rawplot_plot(token: str, kind: str):
    item = _PLOT_CACHE.get(token)
    if not item:
        raise HTTPException(status_code=404, detail="Unknown/expired plot token. Re-upload the XML.")
    item["t"] = time.time()

    try:
        png = render_plot_png(item["session"], kind)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return Response(content=png, media_type="image/svg+xml")


@app.get("/rawplot/plots/{token}.pdf")
def rawplot_pdf(token: str):
    item = _PLOT_CACHE.get(token)
    if not item:
        raise HTTPException(status_code=404, detail="Unknown/expired plot token. Re-upload the XML.")
    item["t"] = time.time()

    try:
        pdf = render_plot_pdf(item["session"])
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    headers = {
        "Content-Disposition": f'attachment; filename="rawplot-report-{token}.pdf"'
    }
    return Response(content=pdf, media_type="application/pdf", headers=headers)
