"""
gpcML plotter with OOP 
input: gpcML XML file
output: - PNG plots of raw, baseline-corrected, W(logM), wM, and nM graphs
"""

from __future__ import annotations

import base64
import csv
import html
import io
import math
import zlib
from dataclasses import dataclass, field
from typing import Optional, Dict

import lxml.etree as ET


MAX_LOGM_ABS_HARD = 50.0 #used to stop absurd numbers from appearing, will throw an error
LOGM_CLIP_MIN = -2.0 #same as above
LOGM_CLIP_MAX = 12.0 #same as above


Row = dict[str, float]

# defines helper funcitons to reduce code repetition and improve readability, such as 
# polynomial evaluation, trapezoidal integration, and axis tick generation. These functions 
# are used by the main classes to process the data and generate plots.

def _to_float(value: object) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _polyval(coeffs_desc: list[float], x: float) -> float:
    total = 0.0
    power = 1.0
    for coeff in coeffs_desc:
        total += coeff * power
        power *= x
    return total


def _polyder(coeffs_desc: list[float]) -> list[float]:
    return [coeff * idx for idx, coeff in enumerate(coeffs_desc)][1:]


def _trapezoid(y: list[float], x: list[float]) -> float:
    if len(y) < 2 or len(x) < 2:
        return 0.0
    return sum((x[i] - x[i - 1]) * (y[i] + y[i - 1]) / 2.0 for i in range(1, len(y)))


def _trapezoid_abs(y: list[float], x: list[float]) -> float:
    if len(y) < 2 or len(x) < 2:
        return 0.0
    return sum(abs(x[i] - x[i - 1]) * (y[i] + y[i - 1]) / 2.0 for i in range(1, len(y)))


def _normalize_peak(values: list[float]) -> list[float]:
    peak = max((abs(v) for v in values), default=0.0)
    if peak <= 0:
        return [0.0 for _ in values]
    return [100.0 * v / peak for v in values]


def _nice_step(raw_step: float) -> float:
    if raw_step <= 0 or not math.isfinite(raw_step):
        return 1.0
    exponent = math.floor(math.log10(raw_step))
    fraction = raw_step / (10.0 ** exponent)
    for nice in (1.0, 2.0, 2.5, 5.0, 10.0):
        if fraction <= nice:
            return nice * (10.0 ** exponent)
    return 10.0 ** (exponent + 1)


def _nice_axis_ticks(y_min: float, y_max: float, target_count: int = 5) -> tuple[float, float, list[float]]:
    if y_min == y_max:
        pad = abs(y_min) * 0.1 or 1.0
        y_min -= pad
        y_max += pad
    step = _nice_step((y_max - y_min) / max(target_count - 1, 1))
    axis_min = math.floor(y_min / step) * step
    axis_max = math.ceil(y_max / step) * step
    if axis_min == axis_max:
        axis_max = axis_min + step
    ticks = []
    tick = axis_min
    limit = axis_max + step * 0.5
    while tick <= limit and len(ticks) < 12:
        ticks.append(0.0 if abs(tick) < step * 1e-10 else tick)
        tick += step
    return axis_min, axis_max, ticks


def _format_tick(value: float) -> str:
    if abs(value) >= 1000 or (0 < abs(value) < 0.001):
        return f"{value:.3g}"
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _pow10_or_inf(value: float) -> float:
    try:
        return 10.0 ** value
    except OverflowError:
        return math.inf


def _all_finite(values: list[float]) -> bool:
    return all(math.isfinite(value) for value in values)


def _log_x_ticks(x_min: float, x_max: float) -> tuple[list[float], list[float]]:
    major = [float(exp) for exp in range(math.ceil(x_min), math.floor(x_max) + 1)]
    minor = []
    for exp in range(math.floor(x_min), math.ceil(x_max) + 1):
        for multiplier in range(2, 10):
            tick = exp + math.log10(multiplier)
            if x_min <= tick <= x_max:
                minor.append(tick)
    return major, minor


def _parse_csv_rows(decoded: str) -> list[Row]:
    sample = decoded[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(decoded), dialect)
    rows = [row for row in reader if row]
    if not rows:
        raise ValueError("Raw data CSV is empty.")

    first_numeric = [_to_float(cell) for cell in rows[0][:2]]
    has_header = len(first_numeric) < 2 or any(value is None for value in first_numeric)
    data_rows = rows[1:] if has_header else rows

    out: list[Row] = []
    for row in data_rows:
        if len(row) < 2:
            continue
        time_min = _to_float(row[0])
        ri_signal = _to_float(row[1])
        if time_min is None or ri_signal is None:
            continue
        out.append({"time_min": time_min, "ri_signal": ri_signal})

    if not out:
        raise ValueError("Raw data CSV did not contain numeric time/intensity columns.")
    return out


class GpcMLParser:
    """
    extracts raw data, calib coeffs, flow rate and integraion valies from a gpcML XML file, uses lxml for data extracting.
    """
    def __init__(self):
        self.tree: Optional[ET._ElementTree] = None
        self.raw_rows: Optional[list[Row]] = None
        self.calibration_data: list[float] = []
        self.integration_data: list[list[float]] = [[0.0, 0.0], [0.0, 0.0]]
        self.flow_rate: Optional[float] = None

    # these help with the fact that different gpcML files may have slightly different structures, such as missing elements or attributes, or using different local names. 
    # The methods try multiple approaches to find the required data and raise informative errors if they cannot be found.
    def load_bytes(self, xml_bytes: bytes) -> None:
        parser = ET.XMLParser(recover=True, huge_tree=True)
        root = ET.fromstring(xml_bytes, parser=parser)
        self.tree = ET.ElementTree(root)

    def _string_xpath(self, xp: str) -> str:
        assert self.tree is not None
        return str(self.tree.xpath(f"string({xp})") or "").strip()

    def _find_first_text_by_localname(self, localnames: tuple[str, ...]) -> str:
        assert self.tree is not None
        root = self.tree.getroot()
        for ln in localnames:
            el = root.find(f".//{{*}}{ln}")
            if el is not None and (el.text or "").strip():
                return (el.text or "").strip()
    #fallback method if the above fails, this is more expensive but can handle more variations in the XML structure
        for ln in localnames:
            found = root.xpath(f".//*[local-name()='{ln}'][1]")
            if found:
                txt = (found[0].text or "").strip()
                if txt:
                    return txt
        return ""

    def _find_first_attr_by_localname(self, localnames: tuple[str, ...], attr_name: str) -> str:
        assert self.tree is not None
        root = self.tree.getroot()
        for ln in localnames:
            found = root.xpath(f".//*[local-name()='{ln}'][1]/@{attr_name}")
            if found:
                value = str(found[0]).strip()
                if value:
                    return value
        return ""

    def extract_raw_data(self) -> list[Row]:
        # parses base64 encoded data into a csv of time vs intensity values
        assert self.tree is not None
        raw_text = self._string_xpath("/gpcML/RunTimeData/RawData")
        if not raw_text:
            raw_text = self._find_first_text_by_localname(("rawData", "RawData"))
        if not raw_text:
            raise ValueError("Could not find <rawData> / <RawData> element text in XML.")

        decoded = base64.b64decode(raw_text).decode("utf-8", errors="replace")
        self.raw_rows = _parse_csv_rows(decoded)
        return self.raw_rows

    def extract_calibration_data(self) -> list[float]:
        # parses for calibration data and saves as a list of floats
        assert self.tree is not None
        coeff_children = self.tree.xpath("/gpcML/CalibrationData/CoeffList/*")
        if not coeff_children:
            coeff_children = self.tree.xpath(".//*[local-name()='CalibrationData']/*[local-name()='CoeffList']/*")
        named_degrees = {
            "constant": 0,
            "linearcoeff": 1,
            "quadraticcoeff": 2,
            "cubiccoeff": 3,
            "quarticcoeff": 4,
            "quinticcoeff": 5,
        }
        named_coeffs: dict[int, float] = {}
        for el in coeff_children:
            localname = ET.QName(el).localname.lower()
            if localname in named_degrees:
                named_coeffs[named_degrees[localname]] = float(el.text)

        if named_coeffs:
            max_degree = max(named_coeffs)
            self.calibration_data = [named_coeffs.get(degree, 0.0) for degree in range(max_degree + 1)]
        else:
            self.calibration_data = [float(el.text) for el in coeff_children] if coeff_children else []
        return self.calibration_data

    def extract_flowrate(self) -> float:
        # parses for flowrate and saves as a float
        s = self._string_xpath("/gpcML/RunTimeData/InstrumentConfiguration/Pump/@FlowRate")
        if not s:
            s = self._find_first_attr_by_localname(("Pump",), "FlowRate")
        if not s:
            raise ValueError("Could not find Pump/@FlowRate in XML.")
        self.flow_rate = float(s)
        return self.flow_rate

    def extract_integration_data(self) -> list[list[float]]:
        # parses for integration values and saves as a list of lists of floats, 
        # the first sublist is the baseline integration window and the second sublist is the peak integration window
        def _f(xp: str, localnames: tuple[str, ...], attr_name: str) -> float:
            s = self._string_xpath(xp) or self._find_first_attr_by_localname(localnames, attr_name)
            if not s:
                raise ValueError(f"Missing integration xpath: {xp}")
            return float(s)

        b0 = _f("/gpcML/RunTimeData/IntegrationValues/Baseline/@StartVolume", ("Baseline",), "StartVolume")
        b1 = _f("/gpcML/RunTimeData/IntegrationValues/Baseline/@EndVolume", ("Baseline",), "EndVolume")
        p0 = _f("/gpcML/RunTimeData/IntegrationValues/Peaks/@StartVolume", ("Peaks",), "StartVolume")
        p1 = _f("/gpcML/RunTimeData/IntegrationValues/Peaks/@EndVolume", ("Peaks",), "EndVolume")
        self.integration_data = [[b0, b1], [p0, p1]]
        return self.integration_data


class DataProcessor:
    # where all the calculations take place
    def __init__(self, raw_rows: list[Row], calib_desc: list[float],
                 integration: list[list[float]], flow_rate: Optional[float]):
        self.raw_rows_full = [row.copy() for row in raw_rows]
        self.raw_rows = [row.copy() for row in raw_rows]
        self.calib_desc = [float(c) for c in calib_desc]
        self.integration = integration
        self.flow_rate = None if flow_rate is None else float(flow_rate)

        self.volume_rows: Optional[list[Row]] = None
        self.baseline_corrected_rows: Optional[list[Row]] = None
        self.logm_rows: Optional[list[Row]] = None
        self.wlogm_rows: Optional[list[Row]] = None
        self.nm_rows: Optional[list[Row]] = None
        self.wm_rows: Optional[list[Row]] = None
        self._excel_signal_max: float = 0.0 # saves maximum signal for use in various calculations

    @staticmethod
    def _snap_to_nearest(values: list[float], target: float) -> float:
        return min(values, key=lambda value: abs(value - target)) # snaps integration to nearest actual volume point

    def convert_time_to_volume(self) -> list[Row]:
        # converts time to volume using flow rate
        if self.flow_rate is None or self.flow_rate <= 0 or not math.isfinite(self.flow_rate):
            raise ValueError("A positive Pump/@FlowRate is required to convert retention time to volume.")
        rows = sorted(
            (
                {
                    "time_min": row["time_min"],
                    "ri_signal": row["ri_signal"],
                    "volume_mL": row["time_min"] * self.flow_rate,
                }
                for row in self.raw_rows
            ),
            key=lambda row: row["time_min"],
        )
        self.volume_rows = rows
        return rows

    def baseline_correct(self) -> list[Row]:
        # baseline correction used is just subtracting the minimal value from signal in the peak window
        if not self.volume_rows:
            raise ValueError("X-axis data has not been computed.")
        p0, p1 = self.integration[1]

        full = sorted(self.volume_rows, key=lambda row: row["volume_mL"])
        vs = [row["volume_mL"] for row in full] # list of all volumes for snapping
        pmin_s = self._snap_to_nearest(vs, min(p0, p1))
        pmax_s = self._snap_to_nearest(vs, max(p0, p1))
        peak = [row.copy() for row in full if pmin_s <= row["volume_mL"] <= pmax_s] # list of all rows in the peak window, copy to avoid modifying original data
        if not peak:
            raise ValueError("Integration window did not contain any raw data points.")
        min_signal = min(row["ri_signal"] for row in peak) # min signal in peak window
        self._excel_signal_max = max(row["ri_signal"] for row in peak)
        for row in peak:
            row["ri_signal"] = row["ri_signal"] - min_signal # baseline correction

        self.baseline_corrected_rows = peak
        self.volume_rows = peak
        self.raw_rows = [{"time_min": row["time_min"], "ri_signal": row["ri_signal"]} for row in peak] # update raw rows to match baseline corrected data for plotting
        return peak

    def convert_volume_to_logM(self) -> list[Row]:
        # converts volume to logM using the calibration coefficients, uses polynomial evaluation, also computes M from logM for later use
        if not self.calib_desc:
            raise ValueError("No calibration coefficients found; cannot compute logM plots.")
        if not self.volume_rows:
            raise ValueError("X-axis data has not been computed.")

        volumes = [row["volume_mL"] for row in self.volume_rows] 
        logm = [_polyval(self.calib_desc, volume) for volume in volumes] # compute logM values from volumes using polynomial evaluation
        if any(not math.isfinite(value) for value in logm):
            raise ValueError("Calibration produced non-finite logM values.")
        if any(abs(value) > MAX_LOGM_ABS_HARD for value in logm):
            raise ValueError("Calibration produced logM values outside the supported plotting range.")

        rows = []
        for row, value in zip(self.volume_rows, logm): # create new rows with logM and M values, copy other data from volume rows
            out = row.copy()
            out["logM"] = value
            out["M"] = _pow10_or_inf(value)
            rows.append(out)
        self.logm_rows = rows
        return rows

    def compute_wlogM(self) -> list[Row]:
        # computes W(logM) values using the derivative of the calibration curve and the signal, 
        # normalizes by the integrated corrected signal area,
        # uses polynomial derivative for slope calculation
        if not self.logm_rows:
            raise ValueError("logM data has not been computed.")
        dcoeff = _polyder(self.calib_desc) # derivative of calibration coefficients for slope calculation
        volumes = [row["volume_mL"] for row in self.logm_rows]
        signals = [max(row["ri_signal"], 0.0) for row in self.logm_rows]
        signal_area = _trapezoid_abs(signals, volumes)
        signal_denom = signal_area if signal_area > 1e-300 else 1e-300
        rows = []
        for row in self.logm_rows: # create new rows with wlogM values, copy other data from logM rows
            dlogm_dx = _polyval(dcoeff, row["volume_mL"]) if dcoeff else 0.0
            slope = abs(dlogm_dx) # Dont care abt the direction of the slope, therefore we take the absolute value of the slope.
            if slope <= 1e-300:
                raise ValueError("Calibration derivative is zero in the integration window; cannot compute W(logM).")
            out = row.copy()
            out["wlogM"] = max(row["ri_signal"], 0.0) / (slope * signal_denom)
            rows.append(out)

        self.wlogm_rows = rows
        return rows

    def compute_nM(self) -> list[Row]:
        # calculates nM values from wM and M values
        if not self.wm_rows:
            raise ValueError("wM data has not been computed.")
        rows = [row.copy() for row in self.wm_rows]
        for row in rows:
            row["nM"] = row["wM"] / max(row["M"], 1e-300)
        self.nm_rows = rows
        return rows

    def compute_wM(self) -> list[Row]:
        #calculates wM values from wlogM and M values. logM is base 10, so dM/dlogM = M * ln(10).
        if not self.wlogm_rows:
            raise ValueError("W(logM) data has not been computed.")
        rows = [row.copy() for row in self.wlogm_rows]
        for row in rows:
            row["wM"] = row["wlogM"] / max(row["M"] * math.log(10.0), 1e-300)
        self.wm_rows = rows
        return rows

    def compute_summary(self) -> dict:
        if not self.nm_rows:
            return {}
        rows = sorted((row for row in self.nm_rows if row["M"] > 0), key=lambda row: row["M"])
        ms = [row["M"] for row in rows]
        sum0 = _trapezoid([row["nM"] for row in rows], ms)
        sum1 = _trapezoid([row["nM"] * row["M"] for row in rows], ms)
        sum2 = _trapezoid([row["nM"] * row["M"] ** 2 for row in rows], ms)
        if abs(sum0) <= 1e-300 or abs(sum1) <= 1e-300:
            return {}
        mn = sum1 / sum0
        mw = sum2 / sum1
        pdi = mw / mn
        if not _all_finite([mn, mw, pdi]):
            return {}
        return {"Mn": mn, "Mw": mw, "PDI": pdi}


class SvgPlotter:
    #size and margins for the SVG plot
    width = 1100
    height = 620
    left = 82
    right = 28
    top = 54
    bottom = 76

    def __init__(self, title: str, xlabel: str, ylabel: str, x_log: bool = False):
        self.title = title
        self.xlabel = xlabel
        self.ylabel = ylabel
        self.x_log = x_log

    def render(self, rows: list[Row], x_key: str, y_key: str, peak_to_100: bool = False) -> bytes:
        # renders the plot as an SVG image, for uploading to the web app, uses basic SVG elements like lines, text, and paths to create the plot, 
        # also handles axis scaling, tick generation, and optional peak normalization
        points = [(row[x_key], row[y_key]) for row in rows if math.isfinite(row[x_key]) and math.isfinite(row[y_key])]
        if peak_to_100:
            ys = _normalize_peak([point[1] for point in points])
            points = [(point[0], y) for point, y in zip(points, ys)]
        if self.x_log:
            points = [(math.log10(x), y) for x, y in points if x > 0]
        if len(points) < 2:
            raise ValueError("Not enough points to render plot.")

        xs, ys = [p[0] for p in points], [p[1] for p in points]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        if x_min == x_max:
            x_min -= 1.0
            x_max += 1.0
        y_min, y_max, y_ticks = _nice_axis_ticks(y_min, y_max)

        plot_w = self.width - self.left - self.right
        plot_h = self.height - self.top - self.bottom

        def sx(x: float) -> float:
            # converts data x values to SVG x coordinates because SVG has its own coordinate system where (0,0) is the top-left corner and x increases to the right,
            #  so we need to scale and translate our data points to fit within the plot area defined by the margins
            return self.left + ((x - x_min) / (x_max - x_min)) * plot_w

        def sy(y: float) -> float:
            # same as above but for y values, also inverts y axis because SVG y increases downwards but we want higher data values to appear higher on the plot
            return self.top + (1.0 - ((y - y_min) / (y_max - y_min))) * plot_h

        path = " ".join(("M" if idx == 0 else "L") + f"{sx(x):.2f},{sy(y):.2f}" for idx, (x, y) in enumerate(points)) # creates the SVG path data string for the line plot, using "M" for the first point and "L" for subsequent points to create a continuous line
        if self.x_log:
            x_ticks, x_minor_ticks = _log_x_ticks(x_min, x_max)
            if not x_ticks:
                x_ticks = [x_min + (x_max - x_min) * i / 4 for i in range(5)]
        else:
            x_ticks, x_minor_ticks = [x_min + (x_max - x_min) * i / 4 for i in range(5)], []

        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.width} {self.height}" role="img">',
            '<rect width="100%" height="100%" fill="#ffffff"/>',
            f'<text x="{self.width / 2}" y="30" text-anchor="middle" font-family="Arial" font-size="22" fill="#172033">{html.escape(self.title)}</text>',
            f'<line x1="{self.left}" y1="{self.top + plot_h}" x2="{self.left + plot_w}" y2="{self.top + plot_h}" stroke="#1f2937" stroke-width="1.5"/>',
            f'<line x1="{self.left}" y1="{self.top}" x2="{self.left}" y2="{self.top + plot_h}" stroke="#1f2937" stroke-width="1.5"/>',
        ]
        for tick in x_minor_ticks:
            x = sx(tick)
            parts.append(f'<line x1="{x:.2f}" y1="{self.top + plot_h - 8}" x2="{x:.2f}" y2="{self.top + plot_h}" stroke="#94a3b8" stroke-width="1"/>')
        for tick in x_ticks:
            label = f"10^{int(tick)}" if self.x_log and abs(tick - round(tick)) < 1e-9 else f"{tick:.3g}"
            x = sx(tick)
            parts.append(f'<line x1="{x:.2f}" y1="{self.top}" x2="{x:.2f}" y2="{self.top + plot_h}" stroke="#e5e7eb"/>')
            parts.append(f'<text x="{x:.2f}" y="{self.top + plot_h + 28}" text-anchor="middle" font-family="Arial" font-size="13" fill="#4b5563">{label}</text>')
        for tick in y_ticks:
            y = sy(tick)
            parts.append(f'<line x1="{self.left}" y1="{y:.2f}" x2="{self.left + plot_w}" y2="{y:.2f}" stroke="#e5e7eb"/>')
            parts.append(f'<text x="{self.left - 12}" y="{y + 4:.2f}" text-anchor="end" font-family="Arial" font-size="13" fill="#4b5563">{_format_tick(tick)}</text>')

        parts.extend([
            f'<path d="{path}" fill="none" stroke="#2563eb" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>',
            f'<text x="{self.left + plot_w / 2}" y="{self.height - 24}" text-anchor="middle" font-family="Arial" font-size="15" fill="#172033">{html.escape(self.xlabel)}</text>',
            f'<text x="24" y="{self.top + plot_h / 2}" transform="rotate(-90 24 {self.top + plot_h / 2})" text-anchor="middle" font-family="Arial" font-size="15" fill="#172033">{html.escape(self.ylabel)}</text>',
            "</svg>",
        ])
        return "".join(parts).encode("utf-8")


@dataclass
class PlotSession:
    processor: DataProcessor
    summary: Dict[str, float] = field(default_factory=dict)
    available_plots: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def build_session_from_gpcml(xml_bytes: bytes) -> PlotSession:
    parser = GpcMLParser()
    parser.load_bytes(xml_bytes)
    raw_rows = parser.extract_raw_data()
    warnings: list[str] = []

    try:
        calib = parser.extract_calibration_data()
    except Exception as exc:
        calib = []
        warnings.append(f"Calibration data could not be read: {exc}")
    try:
        flow = parser.extract_flowrate()
    except Exception as exc:
        flow = None
        warnings.append(f"Flow rate could not be read: {exc}")
    try:
        inter = parser.extract_integration_data()
    except Exception as exc:
        inter = [[0.0, 0.0], [0.0, 0.0]]
        warnings.append(f"Integration values could not be read: {exc}")

    proc = DataProcessor(raw_rows, calib, inter, flow)
    available_plots = ["raw"]
    summary: Dict[str, float] = {}

    usable_flow = flow is not None and flow > 0 and math.isfinite(flow)

    if inter != [[0.0, 0.0], [0.0, 0.0]] and usable_flow:
        proc.convert_time_to_volume()
        proc.baseline_correct()
        available_plots.append("baseline")
        if calib:
            try:
                proc.convert_volume_to_logM()
                proc.compute_wlogM()
                proc.compute_wM()
                proc.compute_nM()
                summary = proc.compute_summary()
                available_plots.extend(["wlogM", "wM", "nM"])
                if not summary:
                    warnings.append("Mn/Mw/PDI summary could not be computed from finite values.")
            except Exception as exc:
                warnings.append(f"Molecular-weight plots could not be computed: {exc}")
        else:
            warnings.append("Calibration coefficients were not found, so only raw and baseline plots are available.")
    elif inter != [[0.0, 0.0], [0.0, 0.0]]:
        warnings.append("Only the raw plot is available because the XML is missing a usable pump flow rate.")
    else:
        warnings.append("Only the raw plot is available because the XML is missing integration metadata.")

    return PlotSession(processor=proc, summary=summary, available_plots=available_plots, warnings=warnings)


def _plot_config(session: PlotSession, kind: str) -> tuple[SvgPlotter, list[Row], str, str, bool]:
    proc = session.processor
    if kind == "raw":
        return SvgPlotter("Raw Data (Intensity vs Time)", "Time (min)", "RI signal"), proc.raw_rows_full, "time_min", "ri_signal", False
    if kind == "baseline":
        return SvgPlotter("Baseline Corrected (Peak Window)", "Volume (mL)", "Corrected RI signal"), proc.baseline_corrected_rows or [], "volume_mL", "ri_signal", False
    if kind == "wlogM":
        return SvgPlotter("W(logM) vs M", "M (g/mol)", "W(logM)", x_log=True), proc.wlogm_rows or [], "M", "wlogM", False
    if kind == "wM":
        return SvgPlotter("wM vs M", "M (g/mol)", "wM", x_log=True), proc.wm_rows or [], "M", "wM", False
    if kind == "nM":
        return SvgPlotter("nM vs M", "M (g/mol)", "nM", x_log=True), proc.nm_rows or [], "M", "nM", False
    raise ValueError(f"Unknown plot kind: {kind}")


def render_plot_png(session: PlotSession, kind: str) -> bytes:
    if kind not in session.available_plots:
        raise ValueError(f"Plot kind '{kind}' is not available for this upload.")
    plotter, rows, x_key, y_key, peak_to_100 = _plot_config(session, kind)
    return plotter.render(rows, x_key, y_key, peak_to_100=peak_to_100)



    # The following functions are for generating a PDF report, which includes a summary page with the Mn, Mw, PDI values and a list of included plots.
def _pdf_obj(number: int, body: bytes) -> bytes:
    return b"%d 0 obj\n" % number + body + b"\nendobj\n"


def _pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _x_tick_label(tick: float, x_log: bool) -> str:
    if x_log:
        if abs(tick - round(tick)) < 1e-9:
            return f"10^{int(round(tick))}"
        return _format_tick(10.0 ** tick)
    return _format_tick(tick)


def _pdf_summary_stream(session: PlotSession, plot_count: int) -> bytes:
    # ops is a list of PDF commands for drawinf the summary page
    ops = [
        "BT /F1 20 Tf 72 742 Td (gpcML RawPlot Report) Tj ET",
        "BT /F1 11 Tf 72 716 Td (Processed chromatogram export) Tj ET",
        "0.8 w 0.12 0.16 0.22 RG",
        "72 694 m 540 694 l S",
        "BT /F1 14 Tf 72 658 Td (Molecular weight summary) Tj ET",
    ]

    if session.summary:
        summary_rows = [
            ("Mn", session.summary.get("Mn")),
            ("Mw", session.summary.get("Mw")),
            ("PDI", session.summary.get("PDI")),
        ]
        y = 628
        for label, value in summary_rows:
            formatted = f"{value:.6g}" if value is not None else "n/a"
            ops.append("BT /F1 12 Tf 96 %d Td (%s) Tj ET" % (y, _pdf_text(label)))
            ops.append("BT /F1 12 Tf 174 %d Td (%s) Tj ET" % (y, _pdf_text(formatted)))
            y -= 24
    else:
        ops.append("BT /F1 12 Tf 96 628 Td (No Mn/Mw/PDI summary was available for this upload.) Tj ET")

    ops.extend([
        "BT /F1 14 Tf 72 510 Td (Included plots) Tj ET",
        "BT /F1 12 Tf 96 480 Td (%s plot page%s) Tj ET" % (plot_count, "" if plot_count == 1 else "s"),
    ])
    y = 454
    for kind in session.available_plots or ["raw"]:
        plotter, _rows, _x_key, _y_key, _peak_to_100 = _plot_config(session, kind)
        ops.append("BT /F1 10 Tf 116 %d Td (- %s) Tj ET" % (y, _pdf_text(plotter.title)))
        y -= 18
        if y < 180:
            break

    if session.warnings:
        ops.append("BT /F1 14 Tf 72 152 Td (Processing notes) Tj ET")
        y = 124
        for warning in session.warnings[:3]:
            text = warning if len(warning) <= 86 else warning[:83] + "..."
            ops.append("BT /F1 9 Tf 96 %d Td (%s) Tj ET" % (y, _pdf_text(text)))
            y -= 16

    ops.append("BT /F1 9 Tf 72 42 Td (Summary is shown here so plot pages remain plot-only.) Tj ET")
    return "\n".join(ops).encode("latin-1", errors="replace")


def _pdf_plot_stream(session: PlotSession, kind: str) -> bytes:
    # This function generates the PDF content stream for a single plot page, which includes the plot title, axis labels, grid lines, ticks, and the plot line itself.
    plotter, rows, x_key, y_key, peak_to_100 = _plot_config(session, kind)
    points = [(row[x_key], row[y_key]) for row in rows if math.isfinite(row[x_key]) and math.isfinite(row[y_key])]
    if peak_to_100:
        ys = _normalize_peak([point[1] for point in points])
        points = [(point[0], y) for point, y in zip(points, ys)]
    if plotter.x_log:
        points = [(math.log10(x), y) for x, y in points if x > 0]
    if len(points) < 2:
        points = [(0.0, 0.0), (1.0, 0.0)]

    xs, ys = [point[0] for point in points], [point[1] for point in points]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    if x_min == x_max:
        x_min -= 1.0
        x_max += 1.0
    y_min, y_max, y_ticks = _nice_axis_ticks(y_min, y_max)
    if plotter.x_log:
        x_ticks, x_minor_ticks = _log_x_ticks(x_min, x_max)
        if not x_ticks:
            x_ticks = [x_min + (x_max - x_min) * i / 4 for i in range(5)]
    else:
        x_ticks, x_minor_ticks = [x_min + (x_max - x_min) * i / 4 for i in range(5)], []

    left, bottom, width, height = 72.0, 92.0, 468.0, 310.0

    def sx(x: float) -> float:
        return left + ((x - x_min) / (x_max - x_min)) * width

    def sy(y: float) -> float:
        return bottom + ((y - y_min) / (y_max - y_min)) * height

    ops = [
        "0.12 0.16 0.22 rg",
        "BT /F1 16 Tf 72 742 Td (%s) Tj ET" % _pdf_text(plotter.title),
        "BT /F1 10 Tf 270 54 Td (%s) Tj ET" % _pdf_text(plotter.xlabel),
        "BT /F1 10 Tf 18 247 Td (%s) Tj ET" % _pdf_text(plotter.ylabel),
        "0.1 w 0.85 0.87 0.90 RG",
    ]
    for tick in x_ticks:
        x = sx(tick)
        ops.append(f"{x:.2f} {bottom:.2f} m {x:.2f} {bottom + height:.2f} l S")
    for tick in y_ticks:
        y = sy(tick)
        ops.append(f"{left:.2f} {y:.2f} m {left + width:.2f} {y:.2f} l S")
    ops.extend([
        "0.8 w 0.12 0.16 0.22 RG",
        f"{left:.2f} {bottom:.2f} m {left + width:.2f} {bottom:.2f} l S",
        f"{left:.2f} {bottom:.2f} m {left:.2f} {bottom + height:.2f} l S",
        "0.6 w 0.12 0.16 0.22 RG",
    ])
    for tick in x_ticks:
        x = sx(tick)
        ops.append(f"{x:.2f} {bottom:.2f} m {x:.2f} {bottom - 5:.2f} l S")
        ops.append("0.12 0.16 0.22 rg")
        ops.append("BT /F1 8 Tf %.2f %.2f Td (%s) Tj ET" % (x - 10, bottom - 18, _pdf_text(_x_tick_label(tick, plotter.x_log))))
    for tick in y_ticks:
        y = sy(tick)
        ops.append(f"{left - 5:.2f} {y:.2f} m {left:.2f} {y:.2f} l S")
        ops.append("0.12 0.16 0.22 rg")
        ops.append("BT /F1 8 Tf %.2f %.2f Td (%s) Tj ET" % (left - 48, y - 3, _pdf_text(_format_tick(tick))))
    ops.extend([
        "0.35 w 0.45 0.53 0.64 RG",
    ])
    for tick in x_minor_ticks:
        x = sx(tick)
        ops.append(f"{x:.2f} {bottom:.2f} m {x:.2f} {bottom + 8:.2f} l S")
    ops.extend([
        "1.4 w 0.15 0.39 0.92 RG",
    ])
    for idx, (x, y) in enumerate(points):
        op = "m" if idx == 0 else "l"
        ops.append(f"{sx(x):.2f} {sy(y):.2f} {op}")
    ops.append("S")

    return "\n".join(ops).encode("latin-1", errors="replace")


def render_plot_pdf(session: PlotSession) -> bytes:
    # renders the entire PDF report
    plot_kinds = session.available_plots or ["raw"]
    include_summary_page = bool(session.summary)
    page_count = len(plot_kinds) + (1 if include_summary_page else 0)
    font_obj = 3 + page_count * 2
    objects = [
        _pdf_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"),
    ]

    page_ids = [3 + i * 2 for i in range(page_count)]
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids).encode()
    objects.append(_pdf_obj(2, b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % page_count))

    streams = []
    if include_summary_page:
        streams.append(_pdf_summary_stream(session, len(plot_kinds)))
    streams.extend(_pdf_plot_stream(session, kind) for kind in plot_kinds)

    for idx, stream_bytes in enumerate(streams):
        page_obj = 3 + idx * 2
        content_obj = page_obj + 1
        stream = zlib.compress(stream_bytes)
        objects.append(_pdf_obj(page_obj, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_obj} 0 R >> >> /Contents {content_obj} 0 R >>".encode()))
        objects.append(_pdf_obj(content_obj, b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream"))

    objects.append(_pdf_obj(font_obj, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(pdf))
        pdf.extend(obj)
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n".encode())
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(pdf)
