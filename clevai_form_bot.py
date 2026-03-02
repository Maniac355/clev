# -*- coding: utf-8 -*-
# Auto-bundled single-file entrypoint for sharing.

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency during early setup
    load_dotenv = None


LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.clevai.edu.vn/api/v1/so/meeting/search"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_WORKERS = 8

STATUS_LABELS = {
    "0": "ABSENCE",
    "1": "ATTEND",
    "3": "QUIT_EARLY",
}
DEFAULT_STATUS_FILTERS = ("0",)


class ClevaiAPIError(RuntimeError):
    """Base exception for API-related failures."""


class ClevaiAuthError(ClevaiAPIError):
    """Raised when API token is invalid or expired."""


class ClevaiHTTPError(ClevaiAPIError):
    """Raised for non-auth HTTP errors and transport failures."""


class ClevaiResponseError(ClevaiAPIError):
    """Raised when response payload structure is invalid."""


def _safe_positive_int(value: Optional[str], default_value: int) -> int:
    if value is None:
        return default_value

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default_value

    if parsed <= 0:
        return default_value

    return parsed


def load_runtime_config(env_path: Optional[str] = None) -> Dict[str, Any]:
    """Load runtime config from .env and environment variables."""
    if load_dotenv:
        load_dotenv(dotenv_path=env_path, override=False)

    base_url = os.getenv("CLEVAI_API_URL") or os.getenv("CLEVAI_URL") or DEFAULT_BASE_URL
    timeout = _safe_positive_int(os.getenv("CLEVAI_API_TIMEOUT"), DEFAULT_TIMEOUT_SECONDS)
    page_size = _safe_positive_int(os.getenv("CLEVAI_PAGE_SIZE"), DEFAULT_PAGE_SIZE)
    max_workers = _safe_positive_int(os.getenv("CLEVAI_MAX_WORKERS"), DEFAULT_MAX_WORKERS)

    return {
        "base_url": base_url,
        "timeout": timeout,
        "page_size": page_size,
        "max_workers": max_workers,
    }


def normalize_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None

    cleaned = token.strip()
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip()

    return cleaned or None


def create_session(token: Optional[str] = None) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    normalized = normalize_token(token)
    if normalized:
        session.headers.update({"Authorization": f"Bearer {normalized}"})

    return session


def validate_filters(so: str, who: str) -> None:
    if not so or not so.strip():
        raise ValueError("`so` is required (manual input).")
    if not who or not who.strip():
        raise ValueError("`who` is required (manual input).")


def normalize_status_filters(status_filters: Optional[Any]) -> List[str]:
    if status_filters is None:
        return list(DEFAULT_STATUS_FILTERS)

    if isinstance(status_filters, str):
        raw_values = [part.strip() for part in status_filters.split(",")]
    elif isinstance(status_filters, (list, tuple, set)):
        raw_values = [str(part).strip() for part in status_filters]
    else:
        raise ValueError("`status_filters` must be None, string, list, tuple, or set.")

    normalized: List[str] = []
    for raw in raw_values:
        if not raw:
            continue
        if raw not in STATUS_LABELS:
            raise ValueError(
                f"Unsupported status '{raw}'. Allowed values: {', '.join(sorted(STATUS_LABELS.keys()))}."
            )
        if raw not in normalized:
            normalized.append(raw)

    if not normalized:
        return list(DEFAULT_STATUS_FILTERS)

    return normalized


def fetch_page(
    session: requests.Session,
    so: str,
    who: str,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    base_url: str = DEFAULT_BASE_URL,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """
    Call endpoint:
    /so/meeting/search?so=<SO>&who=<WHO>&page=<PAGE>&size=<SIZE>
    """
    validate_filters(so, who)

    params = {
        "so": so.strip(),
        "who": who.strip(),
        "page": page,
        "size": page_size,
    }

    LOGGER.debug("Fetching Clevai page: so=%s who=%s page=%s", so, who, page)

    try:
        response = session.get(base_url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise ClevaiHTTPError(f"Request failed for so={so}, who={who}, page={page}: {exc}") from exc

    if response.status_code in (401, 403):
        raise ClevaiAuthError(
            "Authentication failed (401/403). Token may be missing or expired."
        )

    if response.status_code >= 400:
        body_preview = response.text[:300].replace("\n", " ")
        raise ClevaiHTTPError(
            f"Clevai API returned HTTP {response.status_code} for so={so}, who={who}. "
            f"Body: {body_preview}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        body_preview = response.text[:300].replace("\n", " ")
        raise ClevaiResponseError(
            f"Invalid JSON response for so={so}, who={who}, page={page}. Body: {body_preview}"
        ) from exc

    if not isinstance(payload, dict):
        raise ClevaiResponseError(
            f"Expected response object for so={so}, who={who}, page={page}, got {type(payload).__name__}."
        )

    return payload


def is_record_in_selected_status(item: Dict[str, Any], allowed_statuses: Set[str]) -> bool:
    status_value = item.get("teacher_status")
    if status_value is None:
        return False

    return str(status_value).strip() in allowed_statuses


def extract_fields(payload: Dict[str, Any], status_filters: Optional[Any] = None) -> List[Dict[str, Any]]:
    items = payload.get("content", []) or []
    if not isinstance(items, list):
        raise ClevaiResponseError("Expected `content` to be a list in API response.")

    allowed_statuses = set(normalize_status_filters(status_filters))

    records: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not is_record_in_selected_status(item, allowed_statuses):
            continue
        records.append(
            {
                "clag_code": item.get("clag_code"),
                "gte_usi": item.get("gte_usi"),
                "gte_phone": item.get("gte_phone"),
            }
        )

    return records


def resolve_total_pages(payload: Dict[str, Any]) -> int:
    keys = ("totalPages", "total_pages", "totalPage", "total_page")
    for key in keys:
        value = payload.get(key)
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit():
            parsed = int(value)
            if parsed > 0:
                return parsed

    # If API does not expose this, assume single page to keep behavior safe.
    return 1


def fetch_all_absence_threadpool(
    so: str,
    who: str,
    token: Optional[str] = None,
    status_filters: Optional[Any] = None,
    page_size: Optional[int] = None,
    max_workers: Optional[int] = None,
    base_url: Optional[str] = None,
    timeout: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch all records filtered by manual inputs `so`, `who`, and `status_filters`.
    Returns:
      [{"clag_code": "...", "gte_usi": "...", "gte_phone": "..."}, ...]
    """
    validate_filters(so, who)
    normalized_statuses = normalize_status_filters(status_filters)
    config = load_runtime_config()

    resolved_base_url = base_url or config["base_url"]
    resolved_timeout = timeout or config["timeout"]
    resolved_page_size = page_size or config["page_size"]
    resolved_workers = max_workers or config["max_workers"]

    def fetch_page_with_fresh_session(page: int) -> Dict[str, Any]:
        with create_session(token) as session:
            return fetch_page(
                session=session,
                so=so,
                who=who,
                page=page,
                page_size=resolved_page_size,
                base_url=resolved_base_url,
                timeout=resolved_timeout,
            )

    first_payload = fetch_page_with_fresh_session(page=1)

    total_pages = resolve_total_pages(first_payload)
    records_by_page: Dict[int, List[Dict[str, Any]]] = {
        1: extract_fields(first_payload, status_filters=normalized_statuses)
    }

    if total_pages <= 1:
        return records_by_page[1]

    pages = range(2, total_pages + 1)
    with ThreadPoolExecutor(max_workers=resolved_workers) as executor:
        future_to_page = {
            executor.submit(fetch_page_with_fresh_session, page): page
            for page in pages
        }

        for future in as_completed(future_to_page):
            page = future_to_page[future]
            payload = future.result()
            records_by_page[page] = extract_fields(payload, status_filters=normalized_statuses)

    merged: List[Dict[str, Any]] = []
    for page in range(1, total_pages + 1):
        merged.extend(records_by_page.get(page, []))

    return merged


def fetch_absence_by_so_who(
    so: str,
    who: str,
    token: Optional[str] = None,
    status_filters: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Convenience wrapper for manual SO/WHO/status filtering."""
    return fetch_all_absence_threadpool(
        so=so,
        who=who,
        token=token,
        status_filters=status_filters,
    )


from dataclasses import dataclass, field
import os
import re
from typing import Any, Callable, Dict, List, Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency during early setup
    load_dotenv = None


ProgressCallback = Callable[[int, int, bool, Optional[str]], None]

LABEL_EMAIL = ["Email", "E-mail"]
LABEL_SO = ["SO", "Mã SO", "Ma SO"]
LABEL_WHO = ["WHO", "Ca trực", "Ca truc"]
LABEL_CLAG_CODE = ["Mã lớp", "Ma lop"]
LABEL_GTE_USI = ["Mã GV MAIN", "Ma GV MAIN", "GTE USI"]
LABEL_GTE_PHONE = ["SĐT GV", "SDT GV", "Số điện thoại", "So dien thoai"]
LABEL_NOTE = ["Note", "Ghi chú", "Ghi chu"]

SUBMIT_BUTTON_TEXTS = ("Gửi", "Gui", "Submit")
SUBMIT_ANOTHER_LINK_TEXTS = ("Gửi câu trả lời khác", "Gửi phản hồi khác", "Submit another response")
CONFIRMATION_PATTERNS = (
    "Đã ghi lại câu trả lời",
    "Câu trả lời của bạn đã được ghi lại",
    "Your response has been recorded",
    "Response recorded",
)
VALIDATION_PATTERNS = (
    "Đây là câu hỏi bắt buộc",
    "This is a required question",
    "Vui lòng nhập địa chỉ email hợp lệ",
    "Please enter a valid email address",
    "Câu trả lời phải là một email hợp lệ",
    "Response must be a valid email address",
)


@dataclass
class BotSelectors:
    keep_email_selector: str = ""
    email_selector: str = ""
    so_selector: str = ""
    who_selector: str = ""
    clag_code_selector: str = ""
    gte_usi_selector: str = ""
    gte_phone_selector: str = ""
    note_selector: str = ""
    submit_selector: str = ""
    submit_another_selector: str = ""


@dataclass
class BotConfig:
    form_url: str
    selectors: BotSelectors = field(default_factory=BotSelectors)
    note_text: str = "kvl"
    email_text: Optional[str] = None
    headless: bool = True
    user_data_dir: str = "bot-profile"
    profile_directory: str = ""
    browser_channel: str = "chrome"
    browser_executable_path: str = ""
    timeout_ms: int = 15_000
    submit_delay_ms: int = 2_500
    retry_count: int = 2


@dataclass
class BotRunResult:
    success_count: int
    failed_count: int
    failures: List[Dict[str, Any]]


class BotError(RuntimeError):
    """Base exception for Playwright bot failures."""


def _safe_positive_int(raw_value: Optional[str], default_value: int) -> int:
    if raw_value is None:
        return default_value

    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return default_value

    if parsed <= 0:
        return default_value

    return parsed


def _safe_non_negative_int(raw_value: Optional[str], default_value: int) -> int:
    if raw_value is None:
        return default_value

    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return default_value

    if parsed < 0:
        return default_value

    return parsed


def _default_profile_dir() -> str:
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return os.path.join(local_app_data, "clevai-form-bot-profile")
    return "bot-profile"


def load_bot_config_from_env(
    note_text: Optional[str] = None,
    email_text: Optional[str] = None,
    headless: bool = True,
) -> BotConfig:
    if load_dotenv:
        load_dotenv(override=False)

    selectors = BotSelectors(
        keep_email_selector=os.getenv("FORM_KEEP_EMAIL_SELECTOR", "").strip(),
        email_selector=os.getenv("FORM_EMAIL_SELECTOR", "").strip(),
        so_selector=os.getenv("FORM_SO_SELECTOR", "").strip(),
        who_selector=os.getenv("FORM_WHO_SELECTOR", "").strip(),
        clag_code_selector=(
            os.getenv("FORM_CLAG_CODE_SELECTOR")
            or os.getenv("FORM_CLASS_SELECTOR")
            or ""
        ).strip(),
        gte_usi_selector=(
            os.getenv("FORM_GTE_USI_SELECTOR")
            or os.getenv("FORM_TEACHER_SELECTOR")
            or ""
        ).strip(),
        gte_phone_selector=(
            os.getenv("FORM_GTE_PHONE_SELECTOR")
            or os.getenv("FORM_PHONE_SELECTOR")
            or ""
        ).strip(),
        note_selector=os.getenv("FORM_NOTE_SELECTOR", "").strip(),
        submit_selector=os.getenv("FORM_SUBMIT_SELECTOR", "").strip(),
        submit_another_selector=os.getenv("FORM_SUBMIT_ANOTHER_SELECTOR", "").strip(),
    )

    resolved_note = note_text if note_text is not None else os.getenv("FORM_NOTE_DEFAULT", "kvl")
    resolved_email_raw = email_text if email_text is not None else os.getenv("FORM_EMAIL_DEFAULT", "")
    resolved_email = (resolved_email_raw or "").strip() or None
    timeout_ms = _safe_positive_int(os.getenv("FORM_TIMEOUT_MS"), 9_000)
    submit_delay_ms = _safe_positive_int(
        os.getenv("FORM_SUBMIT_DELAY_MS") or os.getenv("SUBMIT_DELAY_MS"),
        400,
    )
    retry_count = _safe_non_negative_int(os.getenv("FORM_SUBMIT_RETRY"), 0)
    default_profile_dir = _default_profile_dir()
    user_data_dir = (os.getenv("BOT_PROFILE_DIR") or default_profile_dir).strip() or default_profile_dir
    profile_directory = (os.getenv("BROWSER_PROFILE_DIRECTORY") or "").strip()
    browser_channel = (os.getenv("BROWSER_CHANNEL") or "chrome").strip()
    browser_executable_path = (os.getenv("BROWSER_EXECUTABLE_PATH") or "").strip()

    return BotConfig(
        form_url=(os.getenv("GOOGLE_FORM_URL") or "").strip(),
        selectors=selectors,
        note_text=resolved_note,
        email_text=resolved_email,
        headless=headless,
        user_data_dir=user_data_dir,
        profile_directory=profile_directory,
        browser_channel=browser_channel,
        browser_executable_path=browser_executable_path,
        timeout_ms=timeout_ms,
        submit_delay_ms=submit_delay_ms,
        retry_count=retry_count,
    )


def validate_bot_config(config: BotConfig) -> None:
    if not config.form_url:
        raise ValueError("GOOGLE_FORM_URL is required for submission mode.")


def _fill_if_present(page: Any, selector: str, value: Optional[Any], timeout_ms: int) -> None:
    if not selector:
        return
    if value is None:
        return

    locator = page.locator(selector).first
    locator.wait_for(state="visible", timeout=timeout_ms)
    locator.click()
    locator.fill(str(value))


def _click_if_present(page: Any, selector: str, timeout_ms: int) -> None:
    if not selector:
        return

    locator = page.locator(selector).first
    locator.wait_for(state="visible", timeout=timeout_ms)
    locator.click()


def _try_fill_locator(locator: Any, value: Any, timeout_ms: int) -> bool:
    locator.wait_for(state="visible", timeout=timeout_ms)
    if locator.is_disabled():
        raise BotError("Field is disabled. Google Form may require login in browser profile.")
    locator.click()
    locator.fill(str(value))
    return True


def _fill_by_labels(page: Any, labels: List[str], value: Any, timeout_ms: int) -> bool:
    lookup_timeout = min(timeout_ms, 450)

    for label in labels:
        try:
            locator = page.get_by_label(label, exact=False).first
            if _try_fill_locator(locator, value, lookup_timeout):
                return True
        except BotError:
            raise
        except Exception:
            pass

    for label in labels:
        try:
            pattern = re.compile(re.escape(label), re.IGNORECASE)
            locator = page.get_by_role("textbox", name=pattern).first
            if _try_fill_locator(locator, value, lookup_timeout):
                return True
        except BotError:
            raise
        except Exception:
            pass

    # Google Forms fallback: locate question block by text, then fill first textbox inside.
    for label in labels:
        try:
            question = page.locator("div[role='listitem']").filter(has_text=label).first
            locator = question.locator("input[type='text'], textarea").first
            if _try_fill_locator(locator, value, lookup_timeout):
                return True
        except BotError:
            raise
        except Exception:
            pass

    return False


def _fill_field(
    page: Any,
    selector: str,
    labels: List[str],
    value: Optional[Any],
    timeout_ms: int,
    required: bool,
    field_name: str,
) -> None:
    if value is None:
        if required:
            raise BotError(f"Missing value for required field: {field_name}")
        return

    if selector:
        try:
            _fill_if_present(page, selector, value, timeout_ms)
            return
        except Exception:
            # Continue with label-based fallback.
            pass

    if _fill_by_labels(page, labels, value, timeout_ms):
        return

    if required:
        labels_view = ", ".join(labels)
        raise BotError(
            f"Unable to fill required field '{field_name}'. "
            f"Selector: '{selector or '(empty)'}', Labels tried: {labels_view}"
        )


def _click_submit(page: Any, config: BotConfig) -> None:
    if config.selectors.submit_selector:
        try:
            submit_button = page.locator(config.selectors.submit_selector).first
            submit_button.wait_for(state="visible", timeout=config.timeout_ms)
            submit_button.click()
            return
        except Exception:
            # Continue with fallback by visible button text.
            pass

    for text in SUBMIT_BUTTON_TEXTS:
        try:
            button = page.get_by_role("button", name=re.compile(re.escape(text), re.IGNORECASE)).first
            button.wait_for(state="visible", timeout=min(config.timeout_ms, 1500))
            button.click()
            return
        except Exception:
            pass

    raise BotError("Unable to find submit button. Set FORM_SUBMIT_SELECTOR in .env.")


def fill_form(
    page: Any,
    record: Dict[str, Any],
    so: str,
    who: str,
    config: BotConfig,
) -> None:
    selectors = config.selectors

    _click_if_present(page, selectors.keep_email_selector, config.timeout_ms)
    _fill_field(
        page=page,
        selector=selectors.email_selector,
        labels=LABEL_EMAIL,
        value=config.email_text,
        timeout_ms=config.timeout_ms,
        required=False,
        field_name="email",
    )
    _fill_field(
        page=page,
        selector=selectors.so_selector,
        labels=LABEL_SO,
        value=so,
        timeout_ms=config.timeout_ms,
        required=False,
        field_name="so",
    )
    _fill_field(
        page=page,
        selector=selectors.who_selector,
        labels=LABEL_WHO,
        value=who,
        timeout_ms=config.timeout_ms,
        required=False,
        field_name="who",
    )
    _fill_field(
        page=page,
        selector=selectors.clag_code_selector,
        labels=LABEL_CLAG_CODE,
        value=record.get("clag_code"),
        timeout_ms=config.timeout_ms,
        required=True,
        field_name="clag_code",
    )
    _fill_field(
        page=page,
        selector=selectors.gte_usi_selector,
        labels=LABEL_GTE_USI,
        value=record.get("gte_usi"),
        timeout_ms=config.timeout_ms,
        required=True,
        field_name="gte_usi",
    )
    _fill_field(
        page=page,
        selector=selectors.gte_phone_selector,
        labels=LABEL_GTE_PHONE,
        value=record.get("gte_phone"),
        timeout_ms=config.timeout_ms,
        required=True,
        field_name="gte_phone",
    )
    _fill_field(
        page=page,
        selector=selectors.note_selector,
        labels=LABEL_NOTE,
        value=config.note_text,
        timeout_ms=config.timeout_ms,
        required=True,
        field_name="note",
    )


def submit_form(page: Any, config: BotConfig) -> None:
    _click_submit(page, config)
    page.wait_for_timeout(config.submit_delay_ms)
    _verify_submit_success(page, config.timeout_ms)

    if config.selectors.submit_another_selector:
        try:
            page.locator(config.selectors.submit_another_selector).first.wait_for(
                state="visible",
                timeout=config.timeout_ms,
            )
        except Exception:
            # Fallback path: the next loop iteration will open form_url directly.
            pass


def _verify_submit_success(page: Any, timeout_ms: int) -> None:
    """Ensure submit really succeeded, not just a button click."""
    try:
        page.wait_for_url(re.compile(r".*/formResponse(?:\\?.*)?$"), timeout=min(timeout_ms, 2500))
        return
    except Exception:
        pass

    for pattern in CONFIRMATION_PATTERNS:
        try:
            locator = page.get_by_text(re.compile(re.escape(pattern), re.IGNORECASE)).first
            locator.wait_for(state="visible", timeout=min(timeout_ms, 1500))
            return
        except Exception:
            pass

    current_url = (page.url or "").lower()
    if "/formresponse" in current_url:
        return

    validation_error = _detect_form_validation_error(page)
    if validation_error:
        raise BotError(f"Submit blocked by form validation: {validation_error}")

    raise BotError(
        "Submit click did not reach confirmation state. "
        "No 'response recorded' text detected."
    )


def _detect_form_validation_error(page: Any) -> Optional[str]:
    for pattern in VALIDATION_PATTERNS:
        try:
            locator = page.get_by_text(re.compile(re.escape(pattern), re.IGNORECASE)).first
            locator.wait_for(state="visible", timeout=800)
            text = (locator.inner_text() or "").strip()
            return text or pattern
        except Exception:
            pass

    try:
        invalid_locator = page.locator("[aria-invalid='true']").first
        if invalid_locator.count() > 0:
            return "One or more required/invalid fields are still not accepted by Google Form."
    except Exception:
        pass

    return None


def _open_submit_another_response(page: Any, config: BotConfig) -> bool:
    """Try to reopen blank form from confirmation page without full reload."""
    timeout_short = min(config.timeout_ms, 700)

    if config.selectors.submit_another_selector:
        try:
            locator = page.locator(config.selectors.submit_another_selector).first
            locator.wait_for(state="visible", timeout=timeout_short)
            locator.click()
            page.locator("input[type='text'], textarea").first.wait_for(
                state="visible",
                timeout=timeout_short,
            )
            return True
        except Exception:
            pass

    # Fast path: click any link that points back to viewform.
    try:
        viewform_link = page.locator("a[href*='viewform']").first
        viewform_link.wait_for(state="visible", timeout=timeout_short)
        viewform_link.click()
        page.locator("input[type='text'], textarea").first.wait_for(
            state="visible",
            timeout=timeout_short,
        )
        return True
    except Exception:
        pass

    # Last quick attempt by visible text.
    for text in SUBMIT_ANOTHER_LINK_TEXTS:
        try:
            link = page.get_by_role("link", name=re.compile(re.escape(text), re.IGNORECASE)).first
            link.wait_for(state="visible", timeout=timeout_short)
            link.click()
            page.locator("input[type='text'], textarea").first.wait_for(
                state="visible",
                timeout=timeout_short,
            )
            return True
        except Exception:
            continue

    return False


def login_google_form(
    config: BotConfig,
    form_url: Optional[str] = None,
    timeout_ms: Optional[int] = None,
) -> int:
    """Open persistent browser for manual Google login and return editable field count."""
    target_url = (form_url or config.form_url or "").strip()
    if not target_url:
        raise ValueError("GOOGLE_FORM_URL is required for login command.")

    resolved_timeout = timeout_ms or config.timeout_ms

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise BotError("Playwright is not installed. Run: pip install playwright") from exc

    with sync_playwright() as playwright:
        context = _launch_persistent_context(playwright, config=config, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(target_url, wait_until="domcontentloaded", timeout=resolved_timeout)

        print("[LOGIN] Browser opened with persistent profile.")
        print("[LOGIN] Sign in to Google if needed, then press Enter here to continue...")
        input()

        try:
            if page.is_closed():
                print("[LOGIN] Browser was closed manually. Login data may already be saved.")
                editable_fields = 0
            else:
                editable_fields = page.locator(
                    "input[type='text']:not([disabled]), textarea:not([disabled])"
                ).count()
        except Exception:
            print("[LOGIN] Browser/page was already closed. Login data may already be saved.")
            editable_fields = 0

        try:
            context.close()
        except Exception:
            pass
        return editable_fields


def _launch_persistent_context(playwright: Any, config: BotConfig, headless: bool) -> Any:
    launch_args = ["--disable-blink-features=AutomationControlled"]
    if config.profile_directory:
        launch_args.append(f"--profile-directory={config.profile_directory}")

    base_kwargs: Dict[str, Any] = {
        "user_data_dir": config.user_data_dir,
        "headless": headless,
        "ignore_default_args": ["--enable-automation"],
        "args": launch_args,
        # Avoid very long startup hangs when Chrome profile is blocked/misconfigured.
        "timeout": min(max(config.timeout_ms * 2, 10_000), 45_000),
    }

    launch_kwargs: Dict[str, Any] = dict(base_kwargs)
    if config.browser_executable_path:
        launch_kwargs["executable_path"] = config.browser_executable_path
    elif config.browser_channel:
        launch_kwargs["channel"] = config.browser_channel

    try:
        return playwright.chromium.launch_persistent_context(**launch_kwargs)
    except Exception as first_exc:
        last_exc: Exception = first_exc
        detected_executable = _detect_browser_executable(config.browser_channel)
        if detected_executable and launch_kwargs.get("executable_path") != detected_executable:
            retry_kwargs = dict(base_kwargs)
            retry_kwargs["executable_path"] = detected_executable
            try:
                return playwright.chromium.launch_persistent_context(**retry_kwargs)
            except Exception as second_exc:
                last_exc = second_exc

        if _is_profile_access_error(last_exc):
            fallback_profile = _default_profile_dir()
            if os.path.abspath(config.user_data_dir) != os.path.abspath(fallback_profile):
                fallback_kwargs = dict(base_kwargs)
                fallback_kwargs["user_data_dir"] = fallback_profile
                if config.browser_executable_path:
                    fallback_kwargs["executable_path"] = config.browser_executable_path
                elif detected_executable:
                    fallback_kwargs["executable_path"] = detected_executable
                elif config.browser_channel:
                    fallback_kwargs["channel"] = config.browser_channel
                try:
                    context = playwright.chromium.launch_persistent_context(**fallback_kwargs)
                    config.user_data_dir = fallback_profile
                    return context
                except Exception as third_exc:
                    last_exc = third_exc

        if _is_profile_locked_error(last_exc):
            raise BotError(
                "Browser profile is in use by another process. "
                "Close all Chrome windows or use another --profile-dir."
            ) from last_exc

        raise BotError(
            "Unable to launch browser. "
            "Check BROWSER_CHANNEL/BROWSER_EXECUTABLE_PATH configuration."
        ) from last_exc


def _detect_browser_executable(channel: str) -> str:
    channel_normalized = (channel or "").strip().lower()

    chrome_candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.join(os.getenv("LOCALAPPDATA") or "", r"Google\Chrome\Application\chrome.exe"),
    ]
    edge_candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.join(os.getenv("LOCALAPPDATA") or "", r"Microsoft\Edge\Application\msedge.exe"),
    ]

    if channel_normalized in ("msedge", "edge"):
        candidates = edge_candidates
    else:
        # Default to Chrome candidates for chrome/chromium/empty.
        candidates = chrome_candidates

    for executable_path in candidates:
        if executable_path and os.path.exists(executable_path):
            return executable_path

    return ""


def _is_profile_access_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "access is denied" in text
        or "safe browsing network" in text
        or "requires a non-default data directory" in text
        or "remote debugging requires a non-default data directory" in text
    )


def _is_profile_locked_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "process singleton" in text
        or "profile appears to be in use" in text
        or "is being used by another process" in text
        or "cannot create default profile directory" in text
    )


def run_bot(
    records: List[Dict[str, Any]],
    so: str,
    who: str,
    config: BotConfig,
    progress_callback: Optional[ProgressCallback] = None,
) -> BotRunResult:
    validate_bot_config(config)

    if not records:
        return BotRunResult(success_count=0, failed_count=0, failures=[])

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise BotError("Playwright is not installed. Run: pip install playwright") from exc

    failures: List[Dict[str, Any]] = []
    success_count = 0
    total = len(records)
    max_attempts = config.retry_count + 1
    next_form_ready = False

    with sync_playwright() as playwright:
        context = _launch_persistent_context(playwright, config=config, headless=config.headless)
        page = context.pages[0] if context.pages else context.new_page()

        try:
            for index, record in enumerate(records, start=1):
                success = False
                last_error: Optional[str] = None

                for attempt in range(1, max_attempts + 1):
                    try:
                        if attempt > 1 or not next_form_ready:
                            page.goto(
                                config.form_url,
                                wait_until="domcontentloaded",
                                timeout=config.timeout_ms,
                            )

                        fill_form(page=page, record=record, so=so, who=who, config=config)
                        submit_form(page=page, config=config)
                        success = True
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        next_form_ready = False
                        if attempt >= max_attempts:
                            break
                        page.wait_for_timeout(500)

                if success:
                    success_count += 1
                    if index < total:
                        next_form_ready = _open_submit_another_response(page, config)
                    else:
                        next_form_ready = False
                else:
                    failures.append(
                        {
                            "index": index,
                            "record": record,
                            "error": last_error or "Unknown error",
                        }
                    )
                    next_form_ready = False

                if progress_callback:
                    progress_callback(index, total, success, last_error)
        finally:
            context.close()

    return BotRunResult(
        success_count=success_count,
        failed_count=len(failures),
        failures=failures,
    )



import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.prompt import Prompt
from rich.table import Table



console = Console()


DEFAULT_FORM_URL = (
    "https://docs.google.com/forms/d/e/1FAIpQLSczXsxPX08gQt9Z9e-_-mJwvM8rcMbdHgtgI4EHkSV_aJ2IQQ/viewform"
)
DEFAULT_SO = "AnhNHT"
DEFAULT_NOTE = "kvl"
DEFAULT_OUTPUT_PATH = "api_live_records.json"
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_WORKERS = 12


def configure_stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def default_bot_profile_dir() -> str:
    local_app_data = os.getenv("LOCALAPPDATA") or ""
    if local_app_data:
        return os.path.join(local_app_data, "clevai-form-bot-profile")
    return "bot-profile"


def prompt_required(label: str, default_value: Optional[str] = None) -> str:
    while True:
        if default_value is not None:
            value = Prompt.ask(label, default=default_value).strip()
        else:
            value = Prompt.ask(label).strip()
        if value:
            return value
        console.print("[red]Bắt buộc nhập giá trị.[/red]")


def prompt_optional(label: str, default_value: str = "") -> str:
    return Prompt.ask(label, default=default_value, show_default=bool(default_value)).strip()


def prompt_mode(default_mode: str = "run") -> str:
    allowed = {"run", "fetch", "submit", "login"}
    if len(sys.argv) < 2:
        return default_mode

    raw_mode = (sys.argv[1] or "").strip().lower()
    if raw_mode in allowed:
        return raw_mode

    console.print(f"[yellow]Mode '{raw_mode}' không hợp lệ. Dùng mặc định: {default_mode}.[/yellow]")
    return default_mode


def prompt_status() -> List[str]:
    status_hint = ", ".join([f"{code}={label}" for code, label in STATUS_LABELS.items()])
    console.print(f"teacher_status: {status_hint}")
    value = Prompt.ask("Nhập teacher_status (ví dụ: 0 hoặc 0,3)", default="0").strip()
    return normalize_status_filters(value)


def format_statuses(status_filters: List[str]) -> str:
    return ", ".join([f"{code}={STATUS_LABELS.get(code, 'UNKNOWN')}" for code in status_filters])


def resolve_token() -> Optional[str]:
    token = prompt_optional("Token (Enter để bỏ qua)")
    if token:
        return token

    env_token = (os.getenv("CLEVAI_BEARER_TOKEN") or "").strip()
    return env_token or None




def fetch_records(
    so: str,
    who: str,
    token: Optional[str],
    status_filters: List[str],
) -> List[Dict[str, Any]]:
    with console.status(
        f"Fetching records for so={so}, who={who}, status={','.join(status_filters)}..."
    ):
        rows = fetch_all_absence_threadpool(
            so=so,
            who=who,
            token=token,
            status_filters=status_filters,
            page_size=DEFAULT_PAGE_SIZE,
            max_workers=DEFAULT_MAX_WORKERS,
        )

    console.print(
        f"[green]Fetched {len(rows)} record(s).[/green] "
        f"(status: {format_statuses(status_filters)})"
    )
    if not rows and not token:
        console.print(
            "[yellow]API trả rỗng khi không có token. "
            "Nếu bạn mở API trên trình duyệt có dữ liệu, hãy nhập token.[/yellow]"
        )
    return rows


def save_json_output(path: str, rows: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as output_file:
        json.dump(rows, output_file, ensure_ascii=False, indent=2)


def load_json_records(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as input_file:
        parsed = json.load(input_file)

    if isinstance(parsed, list):
        rows = parsed
    elif isinstance(parsed, dict) and isinstance(parsed.get("records"), list):
        rows = parsed["records"]
    elif isinstance(parsed, dict):
        rows = [parsed]
    else:
        raise ValueError("JSON input phải là list, object, hoặc {'records': [...]} .")

    return [item for item in rows if isinstance(item, dict)]


def build_bot_config(
    form_url: str,
    profile_dir: str,
    headed: bool,
    note_text: str,
) -> BotConfig:
    config = load_bot_config_from_env(
        note_text=note_text,
        email_text=None,
        headless=not headed,
    )
    config.form_url = form_url.strip()
    config.user_data_dir = profile_dir.strip()
    config.profile_directory = ""
    config.browser_channel = "chrome"
    return config


def prompt_fetch_inputs() -> Tuple[str, str, Optional[str], List[str]]:
    so_default = (os.getenv("CLEVAI_SO_DEFAULT") or "").strip() or DEFAULT_SO
    so = prompt_required("SO", so_default)
    who = prompt_required("WHO")
    token = resolve_token()
    status_filters = prompt_status()
    return so, who, token, status_filters


def prompt_runtime_submit_config(note_text: str, headed: bool = False) -> BotConfig:
    form_url = (os.getenv("GOOGLE_FORM_URL") or DEFAULT_FORM_URL).strip()
    profile_dir = (os.getenv("BOT_PROFILE_DIR") or default_bot_profile_dir()).strip()
    if not profile_dir:
        profile_dir = default_bot_profile_dir()
    return build_bot_config(
        form_url=form_url,
        profile_dir=profile_dir,
        headed=headed,
        note_text=note_text,
    )


def prompt_login_config() -> BotConfig:
    form_url = (os.getenv("GOOGLE_FORM_URL") or DEFAULT_FORM_URL).strip()
    return build_bot_config(
        form_url=form_url,
        profile_dir=default_bot_profile_dir(),
        headed=True,
        note_text=DEFAULT_NOTE,
    )


def fetch_from_cli_and_save() -> Tuple[str, str, List[Dict[str, Any]]]:
    so, who, token, status_filters = prompt_fetch_inputs()
    rows = fetch_records(so=so, who=who, token=token, status_filters=status_filters)
    save_json_output(DEFAULT_OUTPUT_PATH, rows)
    console.print(f"[cyan]Saved JSON output:[/cyan] {DEFAULT_OUTPUT_PATH}")
    return so, who, rows


def submit_records(rows: List[Dict[str, Any]], so: str, who: str, config: BotConfig) -> int:
    if not rows:
        console.print("[yellow]Không có record để submit.[/yellow]")
        return 0

    started_at = time.monotonic()

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("Đã gửi {task.completed}/{task.total}"),
            TextColumn("✅ {task.fields[ok]}  ❌ {task.fields[failed]}"),
            TimeElapsedColumn(),
            transient=False,
            console=console,
        ) as progress:
            task_id = progress.add_task("Đang submit form...", total=len(rows), ok=0, failed=0)

            def on_progress(
                current_index: int,
                total_count: int,
                success: bool,
                error: Optional[str],
            ) -> None:
                task = progress.tasks[task_id]
                current_ok = int(task.fields.get("ok", 0))
                current_failed = int(task.fields.get("failed", 0))

                if success:
                    current_ok += 1
                    description = f"Đang submit... ({current_index}/{total_count})"
                else:
                    current_failed += 1
                    description = f"Có lỗi ở bản ghi {current_index}/{total_count}"

                progress.update(
                    task_id,
                    advance=1,
                    description=description,
                    ok=current_ok,
                    failed=current_failed,
                )

                if success:
                    progress.console.print(f"[green]✓ Đã gửi {current_index}/{total_count}[/green]")
                elif error:
                    progress.console.print(f"[red]✗ Lỗi {current_index}/{total_count}:[/red] {error}")

            result = run_bot(
                records=rows,
                so=so,
                who=who,
                config=config,
                progress_callback=on_progress,
            )
    except (BotError, ValueError) as exc:
        console.print(f"[red][BOT ERROR][/red] {exc}")
        return 1

    elapsed_seconds = round(time.monotonic() - started_at, 2)
    total_count = len(rows)
    success_rate = (result.success_count / total_count * 100.0) if total_count > 0 else 0.0

    summary_table = Table(title="Kết Quả Submit", show_header=True, header_style="bold cyan")
    summary_table.add_column("Mục", style="bold")
    summary_table.add_column("Giá trị")
    summary_table.add_row("Tổng bản ghi", str(total_count))
    summary_table.add_row("Thành công", str(result.success_count))
    summary_table.add_row("Thất bại", str(result.failed_count))
    summary_table.add_row("Tỉ lệ thành công", f"{success_rate:.1f}%")
    summary_table.add_row("Thời gian", f"{elapsed_seconds}s")
    console.print(summary_table)

    if result.failures:
        console.print("[yellow]Failed records:[/yellow]")
        for item in result.failures:
            console.print(
                f"- index={item['index']} clag_code={item['record'].get('clag_code')} error={item['error']}"
            )
    return 0


def cmd_login() -> int:
    config = prompt_login_config()
    try:
        editable_fields = login_google_form(
            config=config,
            form_url=config.form_url,
            timeout_ms=30000,
        )
    except (BotError, ValueError) as exc:
        console.print(f"[red][LOGIN ERROR][/red] {exc}")
        return 1

    if editable_fields > 0:
        console.print(f"[green]Login profile saved.[/green] Editable fields detected: {editable_fields}")
    else:
        console.print(
            "[yellow]Login xong nhưng chưa thấy ô editable. "
            "Kiểm tra quyền form hoặc account.[/yellow]"
        )
    return 0


def cmd_fetch() -> int:
    try:
        fetch_from_cli_and_save()
        return 0
    except ClevaiAuthError as exc:
        console.print(f"[red][AUTH ERROR][/red] {exc}")
        return 1
    except (ClevaiAPIError, ValueError) as exc:
        console.print(f"[red][FETCH ERROR][/red] {exc}")
        return 1


def cmd_submit() -> int:
    input_path = prompt_required("JSON input path", DEFAULT_OUTPUT_PATH)
    so = prompt_optional("SO (nếu form có trường SO)")
    who = prompt_optional("WHO (nếu form có trường WHO)")
    note_text = prompt_optional("Note", DEFAULT_NOTE) or DEFAULT_NOTE

    try:
        rows = load_json_records(input_path)
    except Exception as exc:
        console.print(f"[red][INPUT ERROR][/red] Cannot read records from {input_path}: {exc}")
        return 1

    config = prompt_runtime_submit_config(note_text=note_text, headed=False)
    return submit_records(rows=rows, so=so, who=who, config=config)


def cmd_run() -> int:
    try:
        so, who, rows = fetch_from_cli_and_save()
        should_submit = Prompt.ask("Submit lên form luôn? (y/n)", choices=["y", "n"], default="y")
        if should_submit != "y":
            return 0

        note_text = prompt_optional("Note", DEFAULT_NOTE) or DEFAULT_NOTE
        config = prompt_runtime_submit_config(note_text=note_text, headed=False)
        return submit_records(rows=rows, so=so, who=who, config=config)
    except ClevaiAuthError as exc:
        console.print(f"[red][AUTH ERROR][/red] {exc}")
        return 1
    except (ClevaiAPIError, ValueError) as exc:
        console.print(f"[red][RUN ERROR][/red] {exc}")
        return 1


def main() -> int:
    configure_stdio_utf8()
    mode = prompt_mode(default_mode="run")

    handlers = {
        "run": cmd_run,
        "fetch": cmd_fetch,
        "submit": cmd_submit,
        "login": cmd_login,
    }
    return handlers[mode]()


if __name__ == "__main__":
    raise SystemExit(main())
