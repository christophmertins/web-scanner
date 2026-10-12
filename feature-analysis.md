# Feature Analysis: web-scanner Application

Based on review of the 1559-line Flask application (`app/app.py`) and templates (`index.html`, `gallery.html`).
Date: October 12, 2026

## Existing Features (Complete)

1. **Single-page scans**: JPEG, PNG, PDF output
2. **ADF multi-page scan**: → PDF or ZIP
3. **Multi-session PDF building**: Add pages via `/api/scan` with session ID
4. **Duplex scanning**: Front + back via ADF, auto-weaving
5. **OCR via Tesseract**: Default `deu+eng`, per-file via `/api/ocr`
6. **Paperless-ngx integration**: Upload scan → Paperless; visible in gallery
7. **Auto-scan loop**: Polls ADF with backoff; optional paperless upload
8. **Thumbnail/preview generation**: Auto-rendered in background
9. **Gallery**: Delete, paperless send, OCR from history
10. **Healthcheck endpoint**: `/api/health`
11. **Basic auth support**: configurable via `.env`
12. **OpenAPI/Swagger docs**: `/api/docs` and `/api/openapi.json`

---

## Prioritized Feature Opportunities

### High Priority (User Impact)

| # | Feature | Reason | API/Templating Changes |
|---|---------|--------|----------------------|
| 1 | **Session expiry / cleanup for orphaned PDF sessions** | `jobs` dict persists indefinitely; orphaned sessions from crashed clients never cleaned up. `cleanup_old_jobs(max_age=3600)` only runs on `/api/scan` start. | Add `atexit` handler or background thread to prune sessions older than N minutes. Extend `cleanup_old_jobs` call frequency. |
| 2 | **Rate limiting / scan queue management** | No protection against flood of scan requests; could exhaust scanner or memory. | Add Flask-Limiter or simple token-bucket in code. Add `max_concurrent_scans` config. |
| 3 | **OCR language selection per-file** | `/api/ocr` hardcodes `deu+eng`; no per-file selection in gallery OCR button. | Extend `/api/ocr` to accept `lang` param; update gallery OCR UI to show language selector. |
| 4 | **Scan metadata / tags** | No way to attach user notes/labels to scans. | Add `metadata` field to `/api/scan` request; store in SQLite alongside file; display in gallery. |
| 5 | **Scan history with filters** | Gallery shows all files; no filtering by date, format, paperless status. | Add query params to `/api/devices`/scan history; add filter UI to gallery template. |

### Medium Priority (Polish & Reliability)

| # | Feature | Reason | Notes |
|---|---------|--------|-------|
| 6 | **Remote scan trigger (webhook)** | Allow external systems to trigger a scan via HTTP webhook. | New POST endpoint `/api/scan/webhook` that accepts minimal params; returns job ID. |
| 7 | **Batch scan upload** | Upload multiple scans at once (e.g., from ADF batch). | Could zip results or return list of file URLs. |
| 8 | **Multiple device support with switching** | Currently pick_device() returns first; no UI to switch between scanned devices. | Add device selector UI; store selected device in session or cookie. |
| 9 | **Scanner status monitoring** | `/api/health` mentions `scanner_busy` but no dedicated status endpoint. | Enhance `/api/health` or add `/api/scanner-status` with more detail. |
| 10 | **PDF encryption/permissions** | No way to set password/permissions on output PDFs. | Add `password` param to PDF generation; use PyPDF2/PdfWriter. |
| 11 | **Ocr language selection per-file in gallery** | Gallery OCR button always uses `deu+eng`. | Add language dropdown in gallery OCR modal. |
| 12 | **Dark mode / Light mode toggle** | CSS variables exist but no UI toggle. | Add mode switcher that persists in localStorage; update template. |
| 13 | **Column/responsive grid improvements** | Gallery grid cuts off on mobile; no responsive fallback. | Add `@media` queries or Masonry layout; ensure grid wraps gracefully. |
| 14 | **Session sharing across tabs** | Each tab gets independent job state; no shared state. | Could store jobs in a shared dict keyed by user/session, or use server-side session store. |

### Lower Priority (Nice-to-Have)

| # | Feature |
|---|---------|
| 15 | **Batch scan upload** (multiple files at once) |
| 16 | **Scan progress WebSocket updates** (real-time status) |
| 17 | **User accounts / per-user scan history** |
| 18 | **Scan rotation / deskew** |
| 19 | **Custom scan areas (beyond A4/max)** |
| 20 | **Multi-language UI** (currently German UI strings hardcoded) |

---

## Technical Debt / Code Quality

| Issue | Location | Impact | Fix |
|-------|----------|--------|-----|
| Bare `except Exception` catches | Many lines (e.g., 139, 228, 240, 249, 296, 361, 396, 404, 430, 450, 457, 465, 491, 514, 628, 682) | Errors silently swallowed; hard to debug | Add specific exception types; log the error before returning generic message. |
| Session cleanup runs only on `/api/scan` start | Line 1161 | Orphaned jobs persist indefinitely | Run cleanup on request teardown or background thread. |
| `jobs` dict is in-memory only | Line 67 | Lost on restart; no persistence | Consider SQLite persistence or Redis for job state. |
| No CSRF protection on POST endpoints | All POST routes | Security vulnerability for public deployments | Add CSRF tokens or SameSite cookies. |
| `auto_cfg` global state not thread-safe | Lines 647-690 | Potential race condition in auto-scan loop | Use threading.Lock for state modifications. |
| Missing `Content-Type` headers on some JSON responses | Various | Minor inconsistency | Standardize on `jsonify()` which sets correct headers. |

---

## Open Questions / Decisions Needed

1. **Session persistence**: Should scan jobs survive server restart? (→ SQLite vs in-memory)
2. **Rate limiting**: What's the acceptable throughput? (→ simple limiter vs no limiter)
3. **Device persistence**: Should selected scanner persist across sessions? (→ cookie/DB)
4. **Paperless integration**: Should auto-upload happen on every scan or only upon user request? (→ config option)
5. **OCR languages**: Should we pre-configure additional language packs (e.g., `fra`, `spa`)? (→ depends on Tesseract install)

---

## Suggested Implementation Order (for user selection)

1. **Session expiry + cleanup** (prevents memory leaks)
2. **Rate limiting** (protects against abuse)
3. **OCR language selection per-file** (user-visible improvement)
4 **Scan metadata/tags** (useful for organization)
5. **PDF encryption** (security feature)
6. **Device switching UI** (usability improvement)