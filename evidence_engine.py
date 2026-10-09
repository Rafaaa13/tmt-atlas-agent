"""Local-only evidence extraction; no network or model calls.

PDF dependencies are optional. PDF work runs in disposable subprocesses so a
bad page cannot hang a request thread or leave a thread using closed PIL/PDF
objects. ``ingest_file`` is for trusted standalone scripts, not HTTP APIs.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

SUPPORTED_EXTENSIONS = {".pdf", ".csv", ".json", ".md", ".markdown", ".txt"}
MAX_SOURCE_BYTES = 12 * 1024 * 1024
MAX_PDF_PAGES = 50
DEFAULT_PAGE_TIMEOUT_SECONDS = 15.0
FACT_COLUMNS = (
    "company_id", "metric_key", "value", "unit", "currency", "period_start",
    "period_end", "period_type", "accounting_basis", "scope",
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _chunk_lines(text: str, *, source_id: str, chunk_lines: int = 12) -> list[dict[str, Any]]:
    lines = text.splitlines()
    chunks: list[dict[str, Any]] = []
    for start in range(0, len(lines), chunk_lines):
        selected = lines[start:start + chunk_lines]
        quote = "\n".join(selected).strip()
        if quote:
            chunks.append({
                "id": f"{source_id}:line:{start + 1}", "source_id": source_id,
                "locator_type": "line", "locator_value": f"L{start + 1}-L{start + len(selected)}",
                "page_number": None, "quote": quote, "normalized_text": _clean(quote),
            })
    return chunks


def _close(resource: Any) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _pdf_operation(data: bytes, options: dict[str, Any]) -> Any:
    """Runs only in a worker process; owns and closes all native objects."""
    operation = options["operation"]
    if operation == "count":
        failures = []
        available = False
        try:
            import pdfplumber
        except ImportError:
            pass
        else:
            available = True
            try:
                with pdfplumber.open(io.BytesIO(data)) as pdf:
                    return {"count": len(pdf.pages), "text_available": True, "warnings": []}
            except Exception as exc:
                failures.append(str(exc))
        try:
            import pypdfium2 as pdfium
        except ImportError:
            pass
        else:
            available = True
            document = None
            try:
                document = pdfium.PdfDocument(data)
                return {"count": len(document), "text_available": False,
                        "warnings": ["pdfplumber 不可用或文本层解析失败；可选择本地 OCR。"]}
            except Exception as exc:
                failures.append(str(exc))
            finally:
                _close(document)
        if not available:
            raise ImportError("未安装 pdfplumber 或 pypdfium2；未解析 PDF")
        raise ValueError("PDF 无法解析或已加密：" + "; ".join(failures))
    if operation == "languages":
        import pytesseract
        return sorted(pytesseract.get_languages(config=""))
    if operation == "text":
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            page = pdf.pages[options["index"]]
            try:
                return page.extract_text() or ""
            finally:
                _close(page)
    if operation != "ocr":
        raise ValueError("unknown PDF operation")
    import pypdfium2 as pdfium
    import pytesseract
    document = page = bitmap = image = None
    try:
        document = pdfium.PdfDocument(data)
        page = document[options["index"]]
        width, height = page.get_size()
        if not all(math.isfinite(v) and v > 0 for v in (width, height)):
            raise ValueError("PDF 页面尺寸无效")
        # Bound the raster as well as file size: a tiny PDF can describe an
        # enormous canvas. Typical A4 pages still render at 144 dpi.
        scale = min(2.0, 6000 / max(width, height), math.sqrt(16_000_000 / (width * height)))
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil()
        return pytesseract.image_to_string(
            image, lang=options["language"], timeout=options["timeout"]
        ) or ""
    finally:
        for resource in (image, bitmap, page, document):
            _close(resource)


def _pdf_worker_main() -> None:
    """Private subprocess entry point; input is bytes, never a caller path."""
    try:
        result = _pdf_operation(sys.stdin.buffer.read(), json.loads(sys.argv[1]))
        response = {"ok": True, "result": result}
    except Exception as exc:
        response = {"ok": False, "error": str(exc),
                    "kind": "unavailable" if isinstance(exc, ImportError) else "invalid"}
    sys.stdout.write(json.dumps(response, ensure_ascii=False))


def _run_pdf_operation(data: bytes, operation: str, *, page_timeout: float, **options: Any) -> Any:
    timeout = float(page_timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("page_timeout 必须为正数")
    options.update(operation=operation, timeout=max(0.01, timeout * 0.8))
    # Popen's context manager closes all pipe handles even when communicate
    # times out before it has closed stdin itself.
    with subprocess.Popen(
        [sys.executable, "-c", "from evidence_engine import _pdf_worker_main; _pdf_worker_main()",
         json.dumps(options)],
        cwd=str(Path(__file__).resolve().parent),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=(os.name == "posix"),
    ) as process:
        try:
            stdout, stderr = process.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # Kill the process group, including a running Tesseract child.
            # Cancelling a future would leave native rendering/OCR alive.
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.communicate()
            raise TimeoutError(f"PDF {operation} 超时（{timeout:g}s）") from exc
        if process.returncode:
            raise ValueError("PDF 子进程失败：" + stderr.decode("utf-8", errors="replace")[:400])
    try:
        response = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("PDF 子进程返回无效结果") from exc
    if not response["ok"]:
        if response["kind"] == "unavailable":
            raise ImportError(response["error"])
        raise ValueError(response["error"])
    return response["result"]


def _available_ocr_language(*, page_timeout: float = DEFAULT_PAGE_TIMEOUT_SECONDS) -> tuple[str | None, list[str]]:
    try:
        available = set(_run_pdf_operation(b"", "languages", page_timeout=page_timeout))
    except (ImportError, ValueError, TimeoutError) as exc:
        return None, [f"无法检查本地 Tesseract 语言包，已跳过 OCR：{exc}"]
    languages = [lang for lang in ("eng", "chi_sim") if lang in available]
    if len(languages) == 2:
        return "eng+chi_sim", []
    if languages:
        missing = "chi_sim" if languages == ["eng"] else "eng"
        return languages[0], [f"未发现 {missing} 语言包，OCR 已回退为 {languages[0]}。"]
    return None, ["未发现 eng 或 chi_sim 语言包，已跳过 OCR。"]


def extract_pdf_bytes(data: bytes, source_id: str, *, ocr: bool = False,
                      max_pages: int = MAX_PDF_PAGES,
                      page_timeout: float = DEFAULT_PAGE_TIMEOUT_SECONDS) -> tuple[str, list[dict[str, Any]], list[str]]:
    if b"%PDF-" not in data[:1024]:
        raise ValueError("PDF 文件头无效")
    try:
        info = _run_pdf_operation(data, "count", page_timeout=page_timeout)
    except ImportError as exc:
        return "", [], [str(exc)]
    except TimeoutError as exc:
        raise ValueError(str(exc)) from exc
    count = info["count"]
    limit = min(int(max_pages), MAX_PDF_PAGES)
    if count > limit:
        raise ValueError(f"PDF 页数 {count} 超过上限 {limit}")
    if count <= 0:
        raise ValueError("PDF 没有可读取的页面")
    warnings = list(info["warnings"])
    pages = [""] * count
    if info["text_available"]:
        for index in range(count):
            try:
                pages[index] = _run_pdf_operation(data, "text", index=index, page_timeout=page_timeout)
            except (ImportError, ValueError, TimeoutError) as exc:
                warnings.append(f"第 {index + 1} 页文本层解析失败：{exc}")
    if not any(text.strip() for text in pages):
        warnings.append("未提取到 PDF 文本层。")
    if ocr and any(not text.strip() for text in pages):
        language, notes = _available_ocr_language(page_timeout=page_timeout)
        warnings.extend(notes)
        if language:
            used_ocr = False
            for index, text in enumerate(pages):
                if text.strip():
                    continue
                try:
                    pages[index] = _run_pdf_operation(data, "ocr", index=index, language=language,
                                                      page_timeout=page_timeout)
                    used_ocr = True
                except (ImportError, ValueError, TimeoutError) as exc:
                    warnings.append(f"第 {index + 1} 页 OCR 失败：{exc}")
            if used_ocr:
                warnings.append("部分页面使用了本地 OCR；OCR 结果需要人工核验。")
    evidence = []
    for page_number, text in enumerate(pages, 1):
        for chunk_index, chunk in enumerate(_chunk_lines(text, source_id=source_id), 1):
            chunk.update(id=f"{source_id}:page:{page_number}:chunk:{chunk_index}",
                         locator_type="page", locator_value=f"p.{page_number} · {chunk['locator_value']}",
                         page_number=page_number)
            evidence.append(chunk)
    return "\n\n".join(pages), evidence, warnings


def extract_pdf(path: Path, source_id: str, *, ocr: bool = False,
                max_pages: int = MAX_PDF_PAGES,
                page_timeout: float = DEFAULT_PAGE_TIMEOUT_SECONDS) -> tuple[str, list[dict[str, Any]], list[str]]:
    return extract_pdf_bytes(path.read_bytes(), source_id, ocr=ocr, max_pages=max_pages, page_timeout=page_timeout)


def _decode(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("文本必须使用 UTF-8 编码") from exc
    if "\x00" in text:
        raise ValueError("文本包含 NUL；疑似二进制文件")
    return text


def extract_text_like_bytes(data: bytes, source_id: str) -> tuple[str, list[dict[str, Any]], list[str]]:
    text = _decode(data)
    return text, _chunk_lines(text, source_id=source_id), []


def extract_text_like(path: Path, source_id: str) -> tuple[str, list[dict[str, Any]], list[str]]:
    return extract_text_like_bytes(path.read_bytes(), source_id)


def extract_csv_bytes(data: bytes, source_id: str) -> tuple[str, list[dict[str, Any]], list[str], list[dict[str, str]], list[int]]:
    """Generic CSV stays evidence-only; matching-schema CSV proposes facts.

    Locators use physical input line numbers, including multiline quoted cells.
    A malformed row shape fails the entire import rather than shifting columns.
    """
    text = _decode(data)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    evidence, fact_rows, line_numbers = [], [], []
    try:
        headers = next(reader, [])
        headers = [header.strip() for header in headers]
        if not headers or not all(headers) or len(set(headers)) != len(headers):
            raise ValueError("CSV 表头为空或存在重复列")
        missing = [column for column in FACT_COLUMNS if column not in headers]
        warnings = ["CSV 未匹配事实字段，仅保存证据；缺少：" + ", ".join(missing)] if missing else []
        previous_line = reader.line_num
        for cells in reader:
            start, end = previous_line + 1, reader.line_num
            previous_line = end
            if not cells or not any(cell.strip() for cell in cells):
                continue
            if len(cells) != len(headers):
                raise ValueError(f"CSV 第 {start} 行字段数量与表头不一致")
            row = dict(zip(headers, cells))
            quote = ", ".join(f"{key}={value}" for key, value in row.items())
            evidence.append({
                "id": f"{source_id}:row:{start}", "source_id": source_id, "locator_type": "row",
                "locator_value": f"row {start}" if start == end else f"row {start}-{end}",
                "page_number": None, "quote": quote, "normalized_text": _clean(quote),
            })
            if not missing:
                fact_rows.append({key: row[key] for key in FACT_COLUMNS})
                line_numbers.append(start)
    except csv.Error as exc:
        raise ValueError(f"CSV 解析失败：{exc}") from exc
    return text, evidence, warnings, fact_rows, line_numbers


def extract_csv(path: Path, source_id: str) -> tuple[str, list[dict[str, Any]], list[str]]:
    text, evidence, warnings, _, _ = extract_csv_bytes(path.read_bytes(), source_id)
    return text, evidence, warnings


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"JSON 不允许非有限数值：{value}")


def extract_json_bytes(data: bytes, source_id: str) -> tuple[str, list[dict[str, Any]], list[str]]:
    try:
        value = json.loads(_decode(data), parse_constant=_reject_json_constant)
        text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"JSON 解析失败：{exc}") from exc
    return text, _chunk_lines(text, source_id=source_id), []


def extract_json(path: Path, source_id: str) -> tuple[str, list[dict[str, Any]], list[str]]:
    return extract_json_bytes(path.read_bytes(), source_id)


def extract_bytes(filename: str, data: bytes, source_id: str, *, ocr: bool = False,
                  max_pdf_pages: int = MAX_PDF_PAGES,
                  page_timeout: float = DEFAULT_PAGE_TIMEOUT_SECONDS) -> tuple[str, list[dict[str, Any]], list[str]]:
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError("文件大小超过 12 MiB 上限")
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_bytes(data, source_id, ocr=ocr, max_pages=max_pdf_pages, page_timeout=page_timeout)
    if suffix == ".json":
        return extract_json_bytes(data, source_id)
    if suffix == ".csv":
        text, evidence, warnings, _, _ = extract_csv_bytes(data, source_id)
        return text, evidence, warnings
    if suffix in {".md", ".markdown", ".txt"}:
        return extract_text_like_bytes(data, source_id)
    raise ValueError(f"暂不支持 {suffix or '无扩展名'}；支持 PDF、CSV、JSON、Markdown 和 TXT。")


def ingest_file(path_str: str, *, source_id: str | None = None, ocr: bool = False) -> dict[str, Any]:
    """Trusted local-script wrapper. Never pass API-supplied paths here."""
    path = Path(path_str).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    with path.open("rb") as handle:
        data = handle.read(MAX_SOURCE_BYTES + 1)
    sid = source_id or "file-" + sha256_bytes(data)
    text, evidence, warnings = extract_bytes(path.name, data, sid, ocr=ocr)
    return {
        "source": {
            "id": sid, "title": path.name, "source_type": path.suffix.lower().lstrip("."),
            "sha256": sha256_bytes(data), "size_bytes": len(data), "local_path": str(path),
            "parse_status": "ok" if text.strip() else "empty",
            "ocr_requested": bool(ocr), "warnings": warnings,
        },
        "text": text, "evidence": evidence, "evidence_count": len(evidence),
    }


def search_evidence(evidence: Iterable[dict[str, Any]], query: str, limit: int = 20) -> list[dict[str, Any]]:
    tokens = re.findall(r"\w+", query.lower())
    if not tokens:
        return []
    scored = []
    for item in evidence:
        haystack = (item.get("normalized_text") or item.get("quote") or "").lower()
        score = sum(haystack.count(token) for token in tokens)
        if score:
            scored.append((score, item))
    return [item for _, item in sorted(scored, key=lambda pair: pair[0], reverse=True)[:limit]]
