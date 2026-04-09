#!/usr/bin/env python3
"""
constancia_sat_batch.py
Descarga masiva de Constancias de Situacion Fiscal del SAT Mexico.

Uso:
    python constancia_sat_batch.py --csv clientes.csv
    python constancia_sat_batch.py --csv clientes.csv --output ./salidas_sat --headless --delay 5
    python constancia_sat_batch.py --csv clientes.csv --dry-run
    python constancia_sat_batch.py --csv clientes.csv --gemini-key AIza...
"""

import argparse
import base64
import csv
import logging
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import zlib
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import os

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException, StaleElementReferenceException, WebDriverException
    )
except ImportError:
    sys.exit("Error: instala las dependencias con  pip install -r requirements.txt")

try:
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    sys.exit("Error: instala las dependencias con  pip install -r requirements.txt")

try:
    import undetected_chromedriver as uc
    UC_AVAILABLE = True
except ImportError:
    UC_AVAILABLE = False

try:
    import capsolver
    CAPSOLVER_AVAILABLE = True
except ImportError:
    CAPSOLVER_AVAILABLE = False

import urllib.request
import json as _json

try:
    import ddddocr
    _ocr = ddddocr.DdddOcr(show_ad=False)
    DDDDOCR_AVAILABLE = True
except ImportError:
    DDDDOCR_AVAILABLE = False

try:
    import easyocr as _easyocr
    _easyocr_reader = None  # lazy init — primera vez que se use
    EASYOCR_AVAILABLE = True
except ImportError:
    EASYOCR_AVAILABLE = False

try:
    from sat_captcha_ml import solve_captcha as solve_captcha_ml
    SAT_ML_AVAILABLE = True
except ImportError:
    SAT_ML_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
SAT_LOGIN_URL = "https://wwwmat.sat.gob.mx/personas/iniciar-sesion"
SAT_CONSTANCIA_URLS = [
    "https://wwwmat.sat.gob.mx/aplicacion/operacion/53027/genera-tu-constancia-de-situacion-fiscal.",
    "https://wwwmat.sat.gob.mx/aplicacion/login/53027/genera-tu-constancia-de-situacion-fiscal.",
    "https://wwwmat.sat.gob.mx/aplicacion/53027/genera-tu-constancia-de-situacion-fiscal",
    "https://wwwmat.sat.gob.mx/aplicacion/53027/genera-tu-constancia-de-situacion-fiscal.",
]

RFC_PATTERN = re.compile(r"^[A-Z&Ñ]{3,4}[0-9]{6}[A-Z0-9]{3}$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger("sat_batch")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# ---------------------------------------------------------------------------
# Carga y validacion del CSV
# ---------------------------------------------------------------------------
def load_csv(path: str, logger: logging.Logger) -> list:
    required = {"rfc", "password"}
    clientes = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = {h.strip().lower() for h in (reader.fieldnames or [])}
            missing = required - headers
            if missing:
                logger.error("Al CSV le faltan columnas: %s", missing)
                sys.exit(1)
            for i, row in enumerate(reader, start=2):
                rfc = row.get("rfc", "").strip().upper()
                pwd = row.get("password", "").strip()
                name = row.get("nombre", rfc).strip()
                if not rfc or not pwd:
                    logger.warning("Fila %d: RFC o contrasena vacios — omitida", i)
                    continue
                if not RFC_PATTERN.match(rfc):
                    logger.warning("Fila %d: RFC '%s' con formato inusual — se intentara", i, rfc)
                clientes.append({"rfc": rfc, "password": pwd, "nombre": name, "fila": i})
    except FileNotFoundError:
        logger.error("Archivo no encontrado: %s", path)
        sys.exit(1)
    return clientes


def _cached_chromedriver_path() -> str | None:
    drivers_json = Path.home() / ".wdm" / "drivers.json"
    if not drivers_json.exists():
        return None
    try:
        import json

        data = json.loads(drivers_json.read_text(encoding="utf-8"))
    except Exception:
        return None

    for value in data.values():
        candidate = value.get("binary_path")
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _detect_chrome_binary(explicit_path: str = "") -> str | None:
    candidates: list[str] = []
    if explicit_path:
        candidates.append(explicit_path)

    for env_name in ("CHROME_BINARY", "GOOGLE_CHROME_BIN"):
        value = os.getenv(env_name, "").strip()
        if value:
            candidates.append(value)

    for name in ("google-chrome", "chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    candidates.extend(
        [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
            "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/snap/bin/chromium",
        ]
    )

    seen = set()
    for candidate in candidates:
        normalized = str(Path(candidate).expanduser())
        if normalized in seen:
            continue
        seen.add(normalized)
        if Path(normalized).exists():
            return normalized
    return None


def _chrome_major_version(chrome_binary: str | None) -> int | None:
    if not chrome_binary:
        return None
    try:
        proc = subprocess.run(
            [chrome_binary, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    version_text = (proc.stdout or proc.stderr or "").strip()
    match = re.search(r"(\d+)\.", version_text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _build_chrome_options(
    headless: bool,
    abs_dl: str,
    chrome_binary: str | None,
    chrome_profile_dir: str = "",
):
    prefs = {
        "download.default_directory": abs_dl,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
    }
    opts = webdriver.ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1366,768")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--lang=es-MX")
    opts.add_experimental_option("prefs", prefs)
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    if chrome_profile_dir:
        opts.add_argument(f"--user-data-dir={chrome_profile_dir}")
    if chrome_binary:
        opts.binary_location = chrome_binary
    return opts, prefs


def _prepare_chrome_session(driver, logger: logging.Logger) -> None:
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": (
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
            },
        )
    except Exception as exc:
        logger.debug("No se pudo aplicar script anti-deteccion: %s", exc)


def _report_row(cliente: dict, resultado: str, detalle: str = "", pdf_path: str = "") -> dict:
    return {
        "rfc": cliente["rfc"],
        "nombre": cliente["nombre"],
        "fila_csv": cliente["fila"],
        "resultado": resultado,
        "detalle": detalle,
        "pdf_path": pdf_path,
    }


def _append_captcha_dataset_row(dataset_dir: Path, row: dict) -> None:
    dataset_dir.mkdir(parents=True, exist_ok=True)
    dataset_csv = dataset_dir / "captchas.csv"
    is_new = not dataset_csv.exists()
    with open(dataset_csv, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp",
                "rfc",
                "cliente",
                "modo",
                "captcha_path",
                "captured_text",
                "resultado",
                "detalle",
            ],
        )
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def _capture_captcha_image(driver, dataset_dir: Path, rfc: str) -> tuple[bytes, Path]:
    img_elem = driver.find_element(
        By.CSS_SELECTOR,
        "img[src^='data:image'], img[id*='captcha'], img[src*='captcha']"
    )
    img_src = img_elem.get_attribute("src") or ""
    if not img_src.startswith("data:image"):
        raise ValueError("CAPTCHA sin imagen base64 reconocible")

    img_bytes = base64.b64decode(img_src.split(",", 1)[1])
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    captcha_dir = dataset_dir / "raw"
    captcha_dir.mkdir(parents=True, exist_ok=True)
    file_path = captcha_dir / f"{timestamp}_{rfc}.png"
    file_path.write_bytes(img_bytes)
    return img_bytes, file_path


def _resolve_output_layout(output_root: Path, captcha_dataset: str = "") -> dict[str, Path]:
    output_root = output_root.expanduser()
    output_root.mkdir(parents=True, exist_ok=True)

    pdf_dir = output_root / "pdfs"
    debug_dir = output_root / "debug"
    report_dir = output_root / "reports"

    for path in (pdf_dir, debug_dir, report_dir):
        path.mkdir(parents=True, exist_ok=True)

    dataset_dir = Path(captcha_dataset).expanduser() if captcha_dataset else output_root / "captcha_dataset"
    dataset_dir.mkdir(parents=True, exist_ok=True)

    return {
        "root": output_root,
        "pdfs": pdf_dir,
        "debug": debug_dir,
        "reports": report_dir,
        "captcha_dataset": dataset_dir,
    }


def _manual_captcha_entry(driver, captcha_input, captcha_path: Path, rfc: str, logger: logging.Logger) -> str:
    driver.execute_script(
        """
        arguments[0].scrollIntoView({block: 'center'});
        arguments[0].focus();
        arguments[0].select();
        window.__sat_manual_captcha_value = arguments[0].value || '';
        if (!arguments[0].dataset.codexBound) {
            const update = () => { window.__sat_manual_captcha_value = arguments[0].value || ''; };
            const stopEnter = (event) => {
                if (event.key === 'Enter') {
                    update();
                    event.preventDefault();
                    event.stopPropagation();
                }
            };
            arguments[0].addEventListener('input', update);
            arguments[0].addEventListener('change', update);
            arguments[0].addEventListener('keyup', update);
            arguments[0].addEventListener('keydown', stopEnter, true);
            arguments[0].dataset.codexBound = '1';
        }
        """,
        captcha_input,
    )
    logger.info("[%s] CAPTCHA listo: %s", rfc, captcha_path.name)
    logger.info("[%s] Escribe el CAPTCHA en el navegador y luego presiona Enter en esta terminal.", rfc)

    def read_captcha_value() -> str:
        value = ""
        try:
            value = (captcha_input.get_attribute("value") or "").strip()
        except (StaleElementReferenceException, NoSuchElementException, WebDriverException):
            value = ""
        if value:
            return value
        try:
            fresh_input = driver.find_element(By.ID, "userCaptcha")
            value = (fresh_input.get_attribute("value") or "").strip()
        except Exception:
            value = ""
        if value:
            return value
        try:
            value = (
                driver.execute_script(
                    "return (window.__sat_manual_captcha_value || "
                    "(document.getElementById('userCaptcha') && document.getElementById('userCaptcha').value) || '')"
                )
                or ""
            ).strip()
        except Exception:
            value = ""
        return value

    def fill_captcha_value(text: str) -> None:
        try:
            fresh_input = driver.find_element(By.ID, "userCaptcha")
            fresh_input.clear()
            fresh_input.send_keys(text)
            driver.execute_script("window.__sat_manual_captcha_value = arguments[0];", text)
        except Exception:
            pass

    try:
        input(f"[{rfc}] Captura el CAPTCHA en el navegador y presiona Enter para continuar...")
    except EOFError:
        logger.error("[%s] No hay entrada interactiva disponible para captura manual del CAPTCHA.", rfc)
        return ""

    entered = read_captcha_value()
    for retry in range(3):
        if entered:
            logger.info("[%s] CAPTCHA capturado manualmente: '%s'", rfc, entered)
            return entered

        logger.warning("[%s] El campo CAPTCHA sigue vacio despues de la pausa manual.", rfc)
        logger.info("[%s] Esperando unos segundos por si Enter se presiono antes de terminar de escribir...", rfc)
        deadline = time.time() + 8
        while time.time() < deadline:
            entered = read_captcha_value()
            if entered:
                logger.info("[%s] CAPTCHA detectado despues de reintento automatico: '%s'", rfc, entered)
                return entered
            time.sleep(0.5)

        try:
            fallback = input(
                f"[{rfc}] Campo CAPTCHA vacio. Escribe aqui el CAPTCHA, o presiona Enter para reintentar leyendo el navegador, o escribe 'cancelar' para abortar: "
            ).strip()
        except EOFError:
            fallback = ""

        if fallback.lower() == "cancelar":
            logger.error("[%s] Captura manual cancelada por el operador.", rfc)
            return ""

        if fallback:
            fill_captcha_value(fallback)
            entered = fallback
            logger.info("[%s] CAPTCHA capturado manualmente por terminal: '%s'", rfc, entered)
            return entered

        entered = read_captcha_value()
        if entered:
            logger.info("[%s] CAPTCHA detectado al reintentar lectura del navegador: '%s'", rfc, entered)
            return entered

    logger.error("[%s] No se capturo CAPTCHA manual desde navegador ni terminal.", rfc)
    return ""


def _submit_login_form(driver) -> bool:
    selectors = [
        "input#submit",
        'input[name="submit"]',
        'input[type="submit"]',
        "#btnEntrar",
        "#btnLogin",
        'button[type="submit"]',
    ]
    for sel in selectors:
        try:
            elem = driver.find_element(By.CSS_SELECTOR, sel)
            driver.execute_script("arguments[0].click();", elem)
            return True
        except NoSuchElementException:
            continue

    xpaths = [
        "//input[@id='submit']",
        "//input[@value='Enviar']",
        "//input[@type='submit']",
        "//button[@type='submit']",
    ]
    for xpath in xpaths:
        try:
            elem = driver.find_element(By.XPATH, xpath)
            driver.execute_script("arguments[0].click();", elem)
            return True
        except NoSuchElementException:
            continue
    return False


def _find_password_field(driver):
    for pwd_id in ("ciec", "password"):
        try:
            return driver.find_element(By.ID, pwd_id)
        except NoSuchElementException:
            continue
    try:
        return driver.find_element(By.CSS_SELECTOR, 'input[type="password"]')
    except NoSuchElementException:
        return None


def _ensure_login_form_values(
    driver,
    rfc: str,
    password: str,
    captcha_text: str,
    logger: logging.Logger,
) -> None:
    def refill(field, expected: str, label: str, mask_value: bool = False) -> None:
        try:
            current = (field.get_attribute("value") or "").strip()
        except Exception:
            current = ""
        if current == expected:
            return
        field.clear()
        field.send_keys(expected)
        shown = "***" if mask_value else expected
        logger.info("[%s] Campo %s rehidratado antes del submit: '%s'", rfc, label, shown)

    try:
        rfc_field = driver.find_element(By.ID, "rfc")
        refill(rfc_field, rfc.strip(), "RFC")
    except Exception as exc:
        logger.debug("[%s] No se pudo verificar RFC antes del submit: %s", rfc, exc)

    try:
        pwd_field = _find_password_field(driver)
        if pwd_field is not None:
            refill(pwd_field, password, "contrasena", mask_value=True)
        else:
            logger.debug("[%s] No se encontro campo password al verificar submit.", rfc)
    except Exception as exc:
        logger.debug("[%s] No se pudo verificar password antes del submit: %s", rfc, exc)

    if not captcha_text:
        return
    try:
        captcha_field = driver.find_element(By.ID, "userCaptcha")
        refill(captcha_field, captcha_text, "captcha")
        try:
            driver.execute_script("window.__sat_manual_captcha_value = arguments[0];", captcha_text)
        except Exception:
            pass
    except Exception as exc:
        logger.debug("[%s] No se pudo verificar CAPTCHA antes del submit: %s", rfc, exc)


def _read_login_feedback(driver) -> tuple[str, str]:
    def _extract_text() -> str:
        try:
            driver.switch_to.frame("iframetoload")
            text = driver.find_element(By.TAG_NAME, "body").text.lower()
            driver.switch_to.default_content()
            return text
        except Exception:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            return driver.page_source.lower()

    text = _get_page_text(driver)
    extracted = _extract_text()
    if extracted and extracted not in text:
        text = f"{text}\n{extracted}"
    if "captcha no válido" in text or "captcha no valido" in text:
        return "captcha", "Captcha no valido"
    if (
        "rfc o contraseña incorrectos" in text
        or "rfc o contrasena incorrectos" in text
        or "contraseña son incorrectos" in text
        or "contrasena son incorrectos" in text
        or "credenciales incorrectas" in text
        or "datos incorrectos" in text
    ):
        return "credenciales", "Credenciales incorrectas"
    if "iniciar sesión" in text or "iniciar sesion" in text or "acceso por contraseña" in text or "acceso por contrase" in text:
        return "login", "Login aun visible"
    if "error" in text:
        return "error", "Error del portal SAT"
    return "desconocido", "Estado de login no identificado"


def _is_authenticated_context(driver) -> bool:
    try:
        text = (driver.page_source or "").lower()
    except Exception:
        return False
    markers = [
        "cerrar sesión",
        "cerrar sesion",
        "mis expedientes",
        "servicios disponibles",
        "mi portal",
        "otros trámites y servicios",
        "otros tramites y servicios",
    ]
    return any(marker in text for marker in markers)


def _wait_for_login_outcome(driver, timeout: int) -> tuple[str, str]:
    deadline = time.time() + max(timeout, 20)
    while time.time() < deadline:
        try:
            current = (driver.current_url or "").lower()
        except Exception:
            current = ""

        if current and "iniciar-sesion" not in current:
            return "ok", "Sesion iniciada"

        if _is_authenticated_context(driver):
            return "ok", "Sesion iniciada (contexto autenticado detectado)"

        status, detalle = _read_login_feedback(driver)
        if status in {"credenciales", "captcha", "error"}:
            return status, detalle

        time.sleep(1.0)

    return _read_login_feedback(driver)


def _switch_to_login_frame(driver, timeout: int) -> bool:
    wait = WebDriverWait(driver, max(timeout, 60))
    frame_locators = [
        (By.ID, "iframetoload"),
        (By.CSS_SELECTOR, "iframe#iframetoload"),
        (By.CSS_SELECTOR, "iframe[src*='sat.gob.mx']"),
    ]

    for locator in frame_locators:
        try:
            wait.until(EC.frame_to_be_available_and_switch_to_it(locator))
            return True
        except TimeoutException:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            continue
    return False


def _get_page_text(driver) -> str:
    snippets: list[str] = []
    js = """
        return (
            (document.body && document.body.innerText) ||
            (document.documentElement && document.documentElement.innerText) ||
            ''
        );
    """

    try:
        text = driver.execute_script(js)
        if isinstance(text, str) and text.strip():
            snippets.append(text.strip())
    except Exception:
        pass

    try:
        for frame in driver.find_elements(By.CSS_SELECTOR, "iframe, frame")[:3]:
            try:
                driver.switch_to.frame(frame)
                frame_text = driver.execute_script(js)
                if isinstance(frame_text, str) and frame_text.strip():
                    snippets.append(frame_text.strip())
            except Exception:
                continue
            finally:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
    except Exception:
        pass

    if snippets:
        return "\n".join(snippets).lower()

    try:
        return (driver.page_source or "").lower()
    except Exception:
        return ""


def _text_snippet(text: str, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def _is_not_found_page(driver) -> bool:
    text = _get_page_text(driver)
    signals = [
        "página no encontrada",
        "pagina no encontrada",
        "page not found",
        "error 404",
        "http 404",
        "ha ocurrido un error",
    ]
    return any(signal in text for signal in signals)


def _looks_like_login_context(driver) -> bool:
    text = _get_page_text(driver)
    login_signals = [
        "acceso por contraseña",
        "acceso por contrasena",
        "captcha",
        "olvidaste tu contraseña",
        "olvidaste tu contrasena",
        "e.firma portable",
    ]
    return (
        ("iniciar sesión" in text or "iniciar sesion" in text)
        and any(signal in text for signal in login_signals)
    )


def _looks_like_reimpresion_context(driver) -> bool:
    text = _get_page_text(driver)
    signals = [
        "reimpresión de acuses",
        "reimpresion de acuses",
        "generar constancia",
        "no hay trámites",
        "no hay tramites",
        "tipo de trámite",
        "tipo de tramite",
        "número de folio",
        "numero de folio",
        "consulta tramite",
        "consultatramite.jsf",
    ]
    return sum(signal in text for signal in signals) >= 2


def _looks_like_constancia_context(driver) -> bool:
    text = _get_page_text(driver)
    if (
        not text
        or _looks_like_login_context(driver)
        or _has_ejecutar_en_linea_link(driver)
        or _looks_like_reimpresion_context(driver)
    ):
        return False
    strong_hints = [
        "cédula de identificación fiscal",
        "cedula de identificacion fiscal",
        "código qr",
        "codigo qr",
        "lugar y fecha de emisión",
        "lugar y fecha de emision",
        "cadena original",
    ]
    secondary_hints = [
        "constancia de situación fiscal",
        "constancia de situacion fiscal",
        "nombre, denominación o razón social",
        "nombre, denominacion o razon social",
        "régimen capital",
        "regimen capital",
        "datos de identificación del contribuyente",
        "datos de identificacion del contribuyente",
    ]
    if any(hint in text for hint in strong_hints):
        return True
    return sum(hint in text for hint in secondary_hints) >= 2


def _with_frames(driver):
    yield None
    try:
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    except Exception:
        frames = []
    for idx, frame in enumerate(frames[:4]):
        yield idx, frame


def _frame_debug_suffix(frame_ref) -> str:
    if frame_ref is None:
        return "main"
    idx, frame = frame_ref
    try:
        frame_id = (frame.get_attribute("id") or frame.get_attribute("name") or f"frame_{idx}").strip()
    except Exception:
        frame_id = f"frame_{idx}"
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", frame_id) or f"frame_{idx}"
    return safe


def _absolute_sat_url(base_url: str, raw_url: str) -> str:
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return raw_url
    parsed = urllib.parse.urlparse(base_url)
    if raw_url.startswith("/"):
        return f"{parsed.scheme}://{parsed.netloc}{raw_url}"
    base_dir = parsed.path.rsplit("/", 1)[0] + "/"
    return urllib.parse.urljoin(f"{parsed.scheme}://{parsed.netloc}{base_dir}", raw_url)


def _extract_genera_constancia_url_from_page(driver) -> str | None:
    patterns = [
        r"window\.open\('([^']*IdcGeneraConstancia\.jsf[^']*)'",
        r'"([^"]*IdcGeneraConstancia\.jsf[^"]*)"',
        r"'([^']*IdcGeneraConstancia\.jsf[^']*)'",
    ]

    for frame_ref in _with_frames(driver):
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        current_url = driver.current_url or ""
        if frame_ref is not None:
            try:
                _, frame = frame_ref
                driver.switch_to.frame(frame)
                current_url = driver.current_url or current_url
            except Exception:
                continue

        try:
            html = driver.page_source or ""
        except Exception:
            html = ""
        for pattern in patterns:
            match = re.search(pattern, html, flags=re.IGNORECASE)
            if match:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
                return _absolute_sat_url(current_url, match.group(1))

        try:
            for elem in driver.find_elements(By.CSS_SELECTOR, "a[href*='IdcGeneraConstancia.jsf'], button[onclick*='IdcGeneraConstancia.jsf']"):
                href = (elem.get_attribute("href") or elem.get_attribute("onclick") or "").strip()
                if "IdcGeneraConstancia.jsf" not in href:
                    continue
                match = re.search(r"(https?://[^\s'\"()]+IdcGeneraConstancia\.jsf[^\s'\"()]*)", href)
                if match:
                    try:
                        driver.switch_to.default_content()
                    except Exception:
                        pass
                    return match.group(1)
                match = re.search(r"(/[^'\"()]*IdcGeneraConstancia\.jsf[^'\"()]*)", href)
                if match:
                    try:
                        driver.switch_to.default_content()
                    except Exception:
                        pass
                    return _absolute_sat_url(current_url, match.group(1))
        except Exception:
            pass

    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    return None


def _click_generar_constancia(driver, rfc: str, logger: logging.Logger) -> bool:
    xpaths = [
        "//input[contains(translate(@value,'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚ','abcdefghijklmnopqrstuvwxyzáéíóú'),'generar constancia')]",
        "//button[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚ','abcdefghijklmnopqrstuvwxyzáéíóú'),'generar constancia')]",
        "//a[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚ','abcdefghijklmnopqrstuvwxyzáéíóú'),'generar constancia')]",
        "//*[@role='button' and contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÉÍÓÚ','abcdefghijklmnopqrstuvwxyzáéíóú'),'generar constancia')]",
    ]

    for frame_ref in _with_frames(driver):
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        if frame_ref is not None:
            try:
                _, frame = frame_ref
                driver.switch_to.frame(frame)
            except Exception:
                continue

        for xpath in xpaths:
            try:
                elem = driver.find_element(By.XPATH, xpath)
            except Exception:
                continue

            try:
                label = (
                    elem.get_attribute("value")
                    or elem.text
                    or elem.get_attribute("aria-label")
                    or elem.get_attribute("title")
                    or "generar constancia"
                ).strip()
            except Exception:
                label = "generar constancia"

            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", elem)
            except Exception:
                pass
            existing_handles = []
            try:
                existing_handles = list(driver.window_handles)
            except Exception:
                existing_handles = []
            try:
                driver.execute_script("arguments[0].click();", elem)
            except Exception:
                try:
                    elem.click()
                except Exception as exc:
                    logger.debug("[%s] No se pudo activar '%s': %s", rfc, label, exc)
                    continue

            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            logger.info("[%s] Accion de tramite activada: %s", rfc, label)
            time.sleep(2.0)

            try:
                handles_after = list(driver.window_handles)
            except Exception:
                handles_after = []
            new_handles = [h for h in handles_after if h not in existing_handles]
            if new_handles:
                try:
                    driver.switch_to.window(new_handles[-1])
                    logger.info("[%s] Popup de constancia detectado; cambiando a nueva ventana.", rfc)
                except Exception as exc:
                    logger.debug("[%s] No se pudo cambiar al popup de constancia: %s", rfc, exc)
                return True

            popup_url = _extract_genera_constancia_url_from_page(driver)
            if popup_url:
                logger.info("[%s] Siguiendo popup de constancia: %s", rfc, popup_url)
                driver.get(popup_url)
                time.sleep(2.0)
            return True

    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    return False


def _has_ejecutar_en_linea_link(driver) -> bool:
    selectors = [
        "a.actionButton[href*='/aplicacion/login/53027/']",
        "a[href*='/aplicacion/login/53027/']",
    ]
    for selector in selectors:
        try:
            for elem in driver.find_elements(By.CSS_SELECTOR, selector):
                text = (elem.text or "").strip().lower()
                href = (elem.get_attribute("href") or "").strip()
                if "ejecutar en línea" in text or "ejecutar en linea" in text or "/aplicacion/login/53027/" in href:
                    return True
        except Exception:
            continue
    return False


def _follow_ejecutar_en_linea(driver, rfc: str, logger: logging.Logger) -> bool:
    selectors = [
        "a.actionButton[href*='/aplicacion/login/53027/']",
        "a[href*='/aplicacion/login/53027/']",
    ]
    for selector in selectors:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        for elem in elems:
            href = (elem.get_attribute("href") or "").strip()
            text = (elem.text or "").strip().lower()
            if not href:
                continue
            if "ejecutar en línea" not in text and "ejecutar en linea" not in text and "/aplicacion/login/53027/" not in href:
                continue
            if href.startswith("/"):
                href = f"https://wwwmat.sat.gob.mx{href}"
            logger.info("[%s] Siguiendo accion 'Ejecutar en linea': %s", rfc, href)
            driver.get(href)
            time.sleep(1.5)
            return True
    return False


def _get_iframetoload_src(driver) -> str:
    selectors = [
        "iframe#iframetoload",
        "iframe[id*='iframetoload']",
    ]
    for selector in selectors:
        try:
            elem = driver.find_element(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        raw_src = ""
        try:
            raw_src = (elem.get_dom_attribute("src") or "").strip()
        except Exception:
            pass
        resolved_src = (elem.get_attribute("src") or "").strip()
        src = raw_src or resolved_src
        current = (driver.current_url or "").strip()
        if not raw_src or src in {"about:blank", "about:srcdoc"} or src == current:
            return ""
        if src.startswith("/"):
            return f"https://wwwmat.sat.gob.mx{src}"
        return src
    return ""


def _try_constancia_filters(driver, rfc: str, logger: logging.Logger) -> bool:
    seen: set[str] = set()
    candidates: list[tuple[str, str, str]] = []
    try:
        for elem in driver.find_elements(By.CSS_SELECTOR, "a.filter_link[href*='genera-tu-constancia-de-situacion-fiscal']"):
            elem_id = (elem.get_attribute("id") or "").strip()
            label = (elem.text or "").strip()
            href = (elem.get_attribute("href") or "").strip()
            if not elem_id or not label or label in seen or not href:
                continue
            seen.add(label)
            candidates.append((elem_id, label, href))
    except Exception:
        pass

    if not candidates:
        try:
            html = driver.page_source or ""
        except Exception:
            html = ""
        for elem_id, href, label in re.findall(
            r'<a[^>]*class="filter_link"[^>]*id="([^"]+)"[^>]*href="([^"]*genera-tu-constancia-de-situacion-fiscal[^"]*)"[^>]*>([^<]*)</a>',
            html,
            flags=re.IGNORECASE,
        ):
            label = re.sub(r"\s+", " ", label).strip()
            href = href.strip()
            if not elem_id or not label or label in seen or not href:
                continue
            seen.add(label)
            if href.startswith("/"):
                href = f"https://wwwmat.sat.gob.mx{href}"
            candidates.append((elem_id, label, href))

    if not candidates:
        return False

    logger.info("[%s] Intentando activar filtros de constancia (%d candidatos)...", rfc, len(candidates))

    for elem_id, label, href in candidates:
        cat_id = elem_id.removeprefix("setfilter_")
        try:
            elem = driver.find_element(By.ID, elem_id)
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", elem)
            driver.execute_script("arguments[0].click();", elem)
            logger.info("[%s] Filtro constancia probado: %s", rfc, label)
        except Exception as exc:
            logger.debug("[%s] No se pudo activar filtro '%s': %s", rfc, label, exc)
            continue

        try:
            WebDriverWait(driver, 5).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass
        time.sleep(1.5)

        iframe_src = _get_iframetoload_src(driver)
        if iframe_src:
            logger.info("[%s] Filtro '%s' cargo iframe: %s", rfc, label, iframe_src)
            return True
        if _looks_like_constancia_context(driver):
            logger.info("[%s] Filtro '%s' mostro contenido de constancia", rfc, label)
            return True

        try:
            cookie_url = (
                "https://wwwmat.sat.gob.mx/cs/CookieServer"
                f"?name=satfilter&secure=true&timeout=86400&url=%2F&value={urllib.parse.quote(cat_id, safe='')}"
            )
            driver.get(cookie_url)
            driver.get(href)
            logger.info("[%s] Filtro constancia reprocesado via satfilter: %s", rfc, label)
        except Exception as exc:
            logger.debug("[%s] No se pudo forzar satfilter para '%s': %s", rfc, label, exc)
            continue

        try:
            WebDriverWait(driver, 5).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass
        time.sleep(1.5)

        iframe_src = _get_iframetoload_src(driver)
        if iframe_src:
            logger.info("[%s] satfilter '%s' cargo iframe: %s", rfc, label, iframe_src)
            return True
        if _looks_like_constancia_context(driver):
            logger.info("[%s] satfilter '%s' mostro contenido de constancia", rfc, label)
            return True

    return False


def _dump_debug_page(driver, debug_dir: Path, prefix: str, rfc: str, logger: logging.Logger) -> None:
    timestamp = int(time.time())
    html_path = debug_dir / f"{prefix}_{rfc}_{timestamp}.html"
    txt_path = debug_dir / f"{prefix}_{rfc}_{timestamp}.txt"
    try:
        html_path.write_text(driver.page_source or "", encoding="utf-8")
        logger.info("[%s] HTML debug: %s", rfc, html_path.name)
    except Exception as exc:
        logger.debug("[%s] No se pudo guardar HTML debug: %s", rfc, exc)
    try:
        txt_path.write_text(_get_page_text(driver), encoding="utf-8")
        logger.info("[%s] Texto debug: %s", rfc, txt_path.name)
    except Exception as exc:
        logger.debug("[%s] No se pudo guardar texto debug: %s", rfc, exc)
    for frame_ref in _with_frames(driver):
        if frame_ref is None:
            continue
        suffix = _frame_debug_suffix(frame_ref)
        frame_html_path = debug_dir / f"{prefix}_{rfc}_{timestamp}_{suffix}.html"
        frame_txt_path = debug_dir / f"{prefix}_{rfc}_{timestamp}_{suffix}.txt"
        try:
            driver.switch_to.default_content()
            _, frame = frame_ref
            driver.switch_to.frame(frame)
            frame_html_path.write_text(driver.page_source or "", encoding="utf-8")
            logger.info("[%s] HTML debug frame: %s", rfc, frame_html_path.name)
        except Exception as exc:
            logger.debug("[%s] No se pudo guardar HTML debug del frame %s: %s", rfc, suffix, exc)
        try:
            frame_text = (
                driver.execute_script(
                    "return ((document.body && document.body.innerText) || "
                    "(document.documentElement && document.documentElement.innerText) || '');"
                )
                or ""
            )
            frame_txt_path.write_text(frame_text, encoding="utf-8")
            logger.info("[%s] Texto debug frame: %s", rfc, frame_txt_path.name)
        except Exception as exc:
            logger.debug("[%s] No se pudo guardar texto debug del frame %s: %s", rfc, suffix, exc)
        finally:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass


def _wait_for_constancia_ready(
    driver,
    timeout: int,
    rfc: str,
    logger: logging.Logger,
    pdf_dir: Path | None = None,
    known_pdf_files: set[str] | None = None,
) -> tuple[bool, str, Path | None]:
    deadline = time.time() + max(timeout, 25)
    last_url = driver.current_url
    last_title = (driver.title or "").strip()
    last_text = _get_page_text(driver)
    attempted_filters = False
    attempted_action = False
    attempted_generar = False
    download_size_checks: dict[str, tuple[int, float]] = {}

    while time.time() < deadline:
        if pdf_dir is not None:
            downloaded_pdf = _wait_for_new_pdf_download(
                pdf_dir,
                known_pdf_files or set(),
                timeout=0.6,
                rfc=rfc,
                logger=logger,
                size_checks=download_size_checks,
            )
            if downloaded_pdf is not None:
                return True, f"PDF descargado por SAT: {downloaded_pdf.name}", downloaded_pdf

        try:
            WebDriverWait(driver, 3).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass

        current_url = driver.current_url
        current_title = (driver.title or "").strip()
        visible_text = _get_page_text(driver)
        last_url = current_url
        last_title = current_title
        last_text = visible_text

        if _is_not_found_page(driver):
            return False, f"Pagina no encontrada en {current_url}", None
        if "iniciar-sesion" in current_url or _looks_like_login_context(driver):
            return False, f"Sesion redirigida a login en {current_url}", None
        if _looks_like_constancia_context(driver):
            return True, f"contenido visible detectado | title='{current_title}' | text='{_text_snippet(visible_text)}'", None

        if not attempted_action and _has_ejecutar_en_linea_link(driver):
            attempted_action = True
            if _follow_ejecutar_en_linea(driver, rfc, logger):
                continue

        if _looks_like_reimpresion_context(driver):
            if not attempted_generar:
                attempted_generar = True
                logger.info("[%s] Pantalla de reimpresion detectada; intentando 'Generar constancia'...", rfc)
                if _click_generar_constancia(driver, rfc, logger):
                    continue
                logger.warning("[%s] No se pudo activar 'Generar constancia' en la pantalla de reimpresion.", rfc)

        iframe_src = _get_iframetoload_src(driver)
        if iframe_src and iframe_src != current_url and "iniciar-sesion" not in iframe_src:
            logger.info("[%s] Siguiendo iframe de constancia: %s", rfc, iframe_src)
            driver.get(iframe_src)
            time.sleep(1.2)
            continue

        if (
            not attempted_filters
            and not iframe_src
            and (
                "/aplicacion/operacion/53027/" in current_url
                or "genera tu constancia de situación fiscal" in visible_text
                or "genera tu constancia de situacion fiscal" in visible_text
            )
        ):
            attempted_filters = True
            logger.info("[%s] Pantalla intermedia detectada; intentando activar filtros del tramite...", rfc)
            if _try_constancia_filters(driver, rfc, logger):
                continue
            logger.warning("[%s] No se pudo activar ningun filtro del tramite.", rfc)

        nested_target = _extract_constancia_url_from_page(driver)
        if nested_target and nested_target != current_url and "iniciar-sesion" not in nested_target:
            logger.info("[%s] Siguiendo recurso constancia anidado: %s", rfc, nested_target)
            driver.get(nested_target)
            time.sleep(1.2)
            continue

        time.sleep(1.0)

    return (
        False,
        f"Sin contenido visible de constancia | url={last_url} | title='{last_title}' | text='{_text_snippet(last_text)}'",
        None,
    )


def _wait_for_new_pdf_download(
    pdf_dir: Path,
    known_files: set[str],
    timeout: float,
    rfc: str,
    logger: logging.Logger,
    size_checks: dict[str, tuple[int, float]] | None = None,
) -> Path | None:
    deadline = time.time() + max(timeout, 1)
    if size_checks is None:
        size_checks = {}

    while time.time() < deadline:
        try:
            candidates = sorted(pdf_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime)
        except Exception:
            candidates = []

        for path in candidates:
            if path.name in known_files:
                continue
            try:
                size_now = path.stat().st_size
            except FileNotFoundError:
                continue
            if size_now <= 0:
                continue
            previous = size_checks.get(path.name)
            if previous and previous[0] == size_now and (time.time() - previous[1]) >= 0.5:
                logger.info("[%s] PDF descargado por SAT detectado: %s", rfc, path.name)
                return path
            size_checks[path.name] = (size_now, time.time())
        time.sleep(0.5)
    return None


def _extract_pdf_content_stats(pdf_bytes: bytes) -> dict[str, int]:
    stats = {
        "image_objects": len(re.findall(rb"/Subtype\s*/Image\b", pdf_bytes)),
        "text_ops": 0,
        "text_blocks": 0,
    }
    for match in re.finditer(
        rb"<<.*?/Filter\s*/FlateDecode.*?>>\s*stream\r?\n(.*?)\r?\nendstream",
        pdf_bytes,
        re.S,
    ):
        try:
            stream = zlib.decompress(match.group(1))
        except Exception:
            continue
        stats["text_ops"] += stream.count(b"Tj") + stream.count(b"TJ")
        stats["text_blocks"] += stream.count(b"BT")
    return stats


def _pdf_has_meaningful_content(pdf_bytes: bytes) -> tuple[bool, str]:
    stats = _extract_pdf_content_stats(pdf_bytes)
    if stats["image_objects"] >= 1:
        return True, f"imagenes={stats['image_objects']}"
    if stats["text_ops"] >= 4 or stats["text_blocks"] >= 3:
        return True, (
            f"text_ops={stats['text_ops']} text_blocks={stats['text_blocks']} "
            f"imagenes={stats['image_objects']}"
        )
    return False, (
        f"PDF sin contenido suficiente: text_ops={stats['text_ops']} "
        f"text_blocks={stats['text_blocks']} imagenes={stats['image_objects']}"
    )


def _generate_pdf_with_media(driver, media: str) -> bytes:
    driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": media})
    time.sleep(0.5)
    pdf_data = driver.execute_cdp_cmd("Page.printToPDF", {
        "printBackground": True,
        "paperWidth": 8.27,   # A4 en pulgadas
        "paperHeight": 11.69,
        "marginTop": 0.4,
        "marginBottom": 0.4,
        "marginLeft": 0.4,
        "marginRight": 0.4,
    })
    return base64.b64decode(pdf_data["data"])


def _extract_constancia_url_from_page(driver) -> str | None:
    iframe_selectors = [
        "iframe#iframetoload",
        "iframe[id*='iframetoload']",
        "iframe[src*='/aplicacion/operacion/53027/']",
        "iframe[src*='constancia-de-situacion-fiscal']",
    ]
    for selector in iframe_selectors:
        try:
            for elem in driver.find_elements(By.CSS_SELECTOR, selector):
                src = (elem.get_attribute("src") or "").strip()
                if src.startswith("http"):
                    return src
                if src.startswith("/"):
                    return f"https://wwwmat.sat.gob.mx{src}"
        except Exception:
            continue

    link_selectors = [
        "a[href*='/aplicacion/operacion/53027/']",
        "a[href*='/aplicacion/login/53027/']",
        "a[href*='constancia-de-situacion-fiscal']",
    ]
    for selector in link_selectors:
        try:
            for elem in driver.find_elements(By.CSS_SELECTOR, selector):
                href = (elem.get_attribute("href") or "").strip()
                if href.startswith("http"):
                    return href
                if href.startswith("/"):
                    return f"https://wwwmat.sat.gob.mx{href}"
        except Exception:
            continue

    try:
        html = driver.page_source
    except Exception:
        html = ""
    if html:
        absolute = re.search(r"https://wwwmat\.sat\.gob\.mx/aplicacion/operacion/53027/[^\"'\\s]+", html)
        if absolute:
            return absolute.group(0)
        relative = re.search(r"/aplicacion/operacion/53027/[^\"'\\s]+", html)
        if relative:
            return f"https://wwwmat.sat.gob.mx{relative.group(0)}"

    return None


def _resolve_constancia_target_url(driver, timeout: int, rfc: str, logger: logging.Logger) -> tuple[str | None, str]:
    last_detail = "No se detecto ruta de constancia"
    wait_small = WebDriverWait(driver, min(max(timeout, 15), 30))

    current = driver.current_url
    if current:
        if not _is_not_found_page(driver):
            target = _extract_constancia_url_from_page(driver)
            if target:
                return target, f"detectado desde pagina actual {current}"
            if "/aplicacion/operacion/53027/" in current and "iniciar-sesion" not in current:
                return current, f"url operacion ya activa {current}"
            if _looks_like_constancia_context(driver):
                return current, f"contexto constancia en pagina actual {current}"

    for url in SAT_CONSTANCIA_URLS:
        try:
            driver.get(url)
        except Exception as exc:
            last_detail = f"Error al abrir URL {url}: {exc}"
            continue

        try:
            wait_small.until(lambda d: d.execute_script("return document.readyState") == "complete")
        except Exception:
            pass
        time.sleep(1.2)

        current = driver.current_url
        title = (driver.title or "").strip()
        logger.info("[%s] Ruta constancia probada: %s -> %s | title='%s'", rfc, url, current, title)

        if _is_not_found_page(driver):
            last_detail = f"Pagina no encontrada en {current}"
            continue

        target = _extract_constancia_url_from_page(driver)
        if target:
            return target, f"detectado desde {url}"

        if "/aplicacion/operacion/53027/" in current and "iniciar-sesion" not in current:
            return current, f"url operacion desde {url}"

        if _looks_like_constancia_context(driver):
            return current, f"contexto constancia desde {url}"

        last_detail = f"Sin iframe/link util en {current}"

    return None, last_detail


def _reset_sat_session(driver, logger: logging.Logger) -> None:
    try:
        driver.delete_all_cookies()
    except Exception as exc:
        logger.debug("No se pudieron borrar cookies: %s", exc)
    try:
        driver.execute_script(
            "window.localStorage && window.localStorage.clear();"
            "window.sessionStorage && window.sessionStorage.clear();"
        )
    except Exception as exc:
        logger.debug("No se pudo limpiar storage: %s", exc)
    try:
        driver.get("about:blank")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# WebDriver
# ---------------------------------------------------------------------------
def build_driver(
    browser: str,
    headless: bool,
    download_dir: str,
    logger: logging.Logger,
    chrome_binary: str = "",
    chrome_profile_dir: str = "",
):
    abs_dl = str(Path(download_dir).resolve())

    if browser != "chrome":
        raise ValueError("Este flujo actualmente solo soporta Chrome.")

    resolved_binary = _detect_chrome_binary(chrome_binary)
    if chrome_binary and not resolved_binary:
        logger.warning("No se encontro el binario Chrome indicado: %s", chrome_binary)
    if resolved_binary:
        logger.info("Chrome binario detectado: %s", resolved_binary)

    resolved_profile_dir = ""
    if chrome_profile_dir:
        profile_path = Path(chrome_profile_dir).expanduser().resolve()
        profile_path.mkdir(parents=True, exist_ok=True)
        resolved_profile_dir = str(profile_path)
        logger.info("Perfil Chrome reutilizable: %s", resolved_profile_dir)

    opts, prefs = _build_chrome_options(headless, abs_dl, resolved_binary, resolved_profile_dir)
    launch_errors = []

    try:
        logger.info("Iniciando Chrome con Selenium Manager...")
        driver = webdriver.Chrome(options=opts)
        _prepare_chrome_session(driver, logger)
        return driver
    except Exception as exc:
        launch_errors.append(f"Selenium Manager: {exc}")
        logger.warning("Fallo Selenium Manager para Chrome: %s", exc)

    try:
        cached_driver = _cached_chromedriver_path()
        driver_path = cached_driver or ChromeDriverManager().install()
        logger.info("Iniciando ChromeDriver directo: %s", driver_path)
        service = ChromeService(driver_path)
        driver = webdriver.Chrome(service=service, options=opts)
        _prepare_chrome_session(driver, logger)
        return driver
    except Exception as exc:
        launch_errors.append(f"ChromeDriver directo: {exc}")
        logger.warning("Fallo ChromeDriver directo: %s", exc)

    if UC_AVAILABLE:
        try:
            logger.info("Iniciando Chrome con undetected-chromedriver...")
            uc_opts = uc.ChromeOptions()
            for arg in opts.arguments:
                uc_opts.add_argument(arg)
            uc_opts.add_experimental_option("prefs", prefs)
            if resolved_binary:
                uc_opts.binary_location = resolved_binary

            uc_kwargs = {"options": uc_opts, "use_subprocess": True}
            major = _chrome_major_version(resolved_binary)
            if major:
                uc_kwargs["version_main"] = major

            driver = uc.Chrome(**uc_kwargs)
            _prepare_chrome_session(driver, logger)
            return driver
        except Exception as exc:
            launch_errors.append(f"undetected-chromedriver: {exc}")
            logger.warning("Fallo undetected-chromedriver: %s", exc)

    raise RuntimeError("No se pudo iniciar Chrome: " + " | ".join(launch_errors))


# ---------------------------------------------------------------------------
# Esperar descarga
# ---------------------------------------------------------------------------
def wait_for_download(download_dir: str, before: set, timeout: int = 30):
    dl = Path(download_dir)
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = set(dl.glob("*.pdf"))
        new = current - before
        if new:
            return str(max(new, key=lambda p: p.stat().st_mtime))
        time.sleep(0.5)
    return None


# ---------------------------------------------------------------------------
# Descarga individual
# ---------------------------------------------------------------------------
_GEMINI_MODELS = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-2.0-flash-lite"]


def _gemini_captcha(img_b64: str, api_key: str) -> str:
    """Resuelve CAPTCHA con Gemini (gratis: aistudio.google.com).
    Prueba modelos en orden hasta que uno funcione o agote reintentos en 429.
    """
    from google import genai as _genai
    from google.genai import types as _types
    client = _genai.Client(api_key=api_key)
    prompt = (
        "Este es un CAPTCHA. Lee los caracteres alfanumericos y responde UNICAMENTE con esos "
        "caracteres, sin espacios ni explicacion. Solo los caracteres del CAPTCHA."
    )
    last_exc = None
    for model in _GEMINI_MODELS:
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=[
                        _types.Part.from_bytes(data=base64.b64decode(img_b64), mime_type="image/jpeg"),
                        prompt,
                    ],
                )
                return resp.text.strip()
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    # Extraer retry delay si viene en el mensaje
                    import re as _re
                    m = _re.search(r"retryDelay.*?(\d+)s", msg)
                    delay = int(m.group(1)) + 1 if m else 5
                    time.sleep(min(delay, 30))
                    continue  # reintentar mismo modelo
                break  # error no-429 → probar siguiente modelo
    raise last_exc


def _ocr_local(img_bytes: bytes, logger) -> str | None:
    """Intenta resolver CAPTCHA con EasyOCR o ddddocr (lo que este disponible).
    Retorna el texto reconocido o None si falla.
    """
    from PIL import Image, ImageEnhance, ImageFilter
    import io as _io

    def _preprocess(raw: bytes) -> bytes:
        img = Image.open(_io.BytesIO(raw)).convert("L")
        img = ImageEnhance.Contrast(img).enhance(3.0)
        img = img.filter(ImageFilter.SHARPEN)
        img = img.point(lambda x: 0 if x < 128 else 255)
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    # EasyOCR — mejor accuracy que ddddocr en texto alfanumerico simple
    if EASYOCR_AVAILABLE:
        global _easyocr_reader
        try:
            if _easyocr_reader is None:
                _easyocr_reader = _easyocr.Reader(["en"], gpu=False, verbose=False)
            processed = _preprocess(img_bytes)
            img_arr = Image.open(_io.BytesIO(processed))
            results = _easyocr_reader.readtext(processed, detail=0, allowlist="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
            texto = "".join(results).strip()
            if texto:
                logger.info("CAPTCHA resuelto por EasyOCR: '%s'", texto)
                return texto
        except Exception as exc:
            logger.warning("EasyOCR error: %s", exc)

    # ddddocr — fallback local
    if DDDDOCR_AVAILABLE:
        try:
            processed = _preprocess(img_bytes)
            texto = _ocr.classification(processed).strip()
            if texto:
                logger.info("CAPTCHA resuelto por ddddocr: '%s'", texto)
                return texto
        except Exception as exc:
            logger.warning("ddddocr error: %s", exc)

    return None


def _resolver_captcha(
    driver,
    cliente: dict,
    dataset_dir: Path,
    captcha_mode: str,
    captcha_key: str,
    tc_user: str,
    logger: logging.Logger,
) -> dict:
    """Prepara el CAPTCHA y devuelve metadatos del intento actual."""
    # reCAPTCHA — no hay bypass automatico gratuito
    try:
        has_recaptcha = bool(
            driver.find_elements(By.CSS_SELECTOR, "iframe[src*='recaptcha']") or
            driver.find_elements(By.CSS_SELECTOR, "div.g-recaptcha")
        )
    except Exception:
        has_recaptcha = False

    if has_recaptcha:
        logger.error("[%s] reCAPTCHA detectado — este flujo solo soporta el CAPTCHA de imagen.", cliente["rfc"])
        return {"ok": False, "detalle": "reCAPTCHA detectado", "had_captcha": True}

    # CAPTCHA imagen simple — buscar input y imagen
    try:
        captcha_input = driver.find_element(By.ID, "userCaptcha")
    except NoSuchElementException:
        return {"ok": True, "had_captcha": False, "detalle": "Sin CAPTCHA", "captcha_text": "", "captcha_path": ""}

    try:
        img_bytes, captcha_path = _capture_captcha_image(driver, dataset_dir, cliente["rfc"])
    except Exception as exc:
        logger.error("[%s] No se pudo capturar la imagen del CAPTCHA: %s", cliente["rfc"], exc)
        return {"ok": False, "had_captcha": True, "detalle": str(exc)}

    texto = ""
    detalle = ""
    mode_used = captcha_mode

    if captcha_mode == "manual":
        texto = _manual_captcha_entry(driver, captcha_input, captcha_path, cliente["rfc"], logger)
        detalle = "CAPTCHA capturado manualmente"
    else:
        img_b64 = base64.b64encode(img_bytes).decode("ascii")
        if captcha_mode == "ocr" and SAT_ML_AVAILABLE:
            try:
                texto, confidences = solve_captcha_ml(img_bytes)
                avg_conf = sum(confidences) / max(len(confidences), 1)
                logger.info(
                    "[%s] CAPTCHA resuelto por modelo local: '%s' (confianzas=%s, avg=%.3f)",
                    cliente["rfc"],
                    texto,
                    ",".join(f"{value:.2f}" for value in confidences),
                    avg_conf,
                )
                detalle = "Modelo local"
            except Exception as exc:
                logger.warning("[%s] Modelo local CAPTCHA error: %s", cliente["rfc"], exc)
                texto = ""

        if not texto and tc_user:
            try:
                texto = _gemini_captcha(img_b64, tc_user)
                logger.info("[%s] CAPTCHA resuelto por Gemini: '%s'", cliente["rfc"], texto)
                detalle = "Gemini"
                mode_used = "gemini"
            except Exception as exc:
                logger.warning("[%s] Gemini error: %s", cliente["rfc"], exc)

        if not texto and captcha_key and CAPSOLVER_AVAILABLE:
            try:
                capsolver.api_key = captcha_key
                solution = capsolver.solve({"type": "ImageToTextTask", "body": img_b64, "module": "common"})
                texto = solution.get("text", "").strip()
                logger.info("[%s] CAPTCHA resuelto por CapSolver: '%s'", cliente["rfc"], texto)
                detalle = "CapSolver"
                mode_used = "capsolver"
            except Exception as exc:
                logger.warning("[%s] CapSolver error: %s", cliente["rfc"], exc)

        if not texto:
            texto = _ocr_local(img_bytes, logger) or ""
            if texto:
                detalle = "OCR local"
                mode_used = "ocr"

        if texto:
            captcha_input.clear()
            captcha_input.send_keys(texto)

    if not texto:
        logger.error("[%s] No se obtuvo texto de CAPTCHA para este intento.", cliente["rfc"])
        return {
            "ok": False,
            "had_captcha": True,
            "detalle": "CAPTCHA sin texto",
            "captcha_text": "",
            "captcha_path": str(captcha_path),
            "mode": mode_used,
        }

    return {
        "ok": True,
        "had_captcha": True,
        "detalle": detalle,
        "captcha_text": texto,
        "captcha_path": str(captcha_path),
        "mode": mode_used,
    }


def descargar_constancia(
    driver,
    cliente: dict,
    pdf_dir: Path,
    debug_dir: Path,
    timeout: int,
    captcha_mode: str,
    dataset_dir: Path,
    captcha_key: str,
    tc_user: str,
    logger: logging.Logger,
) -> dict:
    rfc = cliente["rfc"]
    pwd = cliente["password"]
    wait = WebDriverWait(driver, timeout)
    captcha_info = {"had_captcha": False, "captcha_text": "", "captcha_path": "", "mode": captcha_mode}
    result_row = _report_row(cliente, "FALLO", "Proceso no completado")
    known_pdf_files = {path.name for path in pdf_dir.glob("*.pdf")}

    try:
        # 1. Login
        driver.get(SAT_LOGIN_URL)

        # Esperar a que el portal cargue: puede redirigir (ya auth) o mostrar el iframe de login
        try:
            wait.until(lambda d: "iniciar-sesion" not in d.current_url or
                       len(d.find_elements(By.ID, "iframetoload")) > 0)
        except TimeoutException:
            pass

        ya_autenticado = "iniciar-sesion" not in driver.current_url

        if not ya_autenticado:
            # Cambiar al iframe que contiene el formulario de login
            if not _switch_to_login_frame(driver, timeout):
                logger.error("[%s] No se cargo el iframe de login del SAT", rfc)
                shot = debug_dir / f"error_login_iframe_{rfc}_{int(time.time())}.png"
                driver.save_screenshot(str(shot))
                logger.info("[%s] Screenshot: %s", rfc, shot.name)
                result_row = _report_row(cliente, "FALLO", "No se cargo el iframe de login del SAT")
                return result_row

            # Esperar a que el formulario JSF cargue completamente dentro del iframe
            time.sleep(4)

            # Screenshot de diagnostico del formulario de login (dentro del iframe)
            shot_login = debug_dir / f"debug_login_{rfc}_{int(time.time())}.png"
            driver.save_screenshot(str(shot_login))
            logger.info("[%s] Screenshot login form: %s", rfc, shot_login.name)

            # Buscar y llenar campos dentro del iframe (o detectar sesion ya activa)
            needs_login_form = True
            try:
                wait.until(EC.presence_of_element_located((By.ID, "rfc")))
            except TimeoutException:
                driver.switch_to.default_content()
                current = (driver.current_url or "").lower()
                if _is_authenticated_context(driver) or "buzon" in current:
                    logger.info("[%s] Sesion activa detectada en portal; se omite relogin.", rfc)
                    ya_autenticado = True
                    needs_login_form = False
                else:
                    logger.error("[%s] No se encontro el campo RFC dentro del iframe", rfc)
                    result_row = _report_row(cliente, "FALLO", "No se encontro el campo RFC")
                    return result_row

            if needs_login_form:
                driver.find_element(By.ID, "rfc").clear()
                driver.find_element(By.ID, "rfc").send_keys(rfc)

                # El campo de contrasena puede llamarse "ciec" o "password" segun la version del portal
                pwd_field = _find_password_field(driver)
                if pwd_field is None:
                    logger.error("[%s] No se encontro el campo de contrasena en el iframe", rfc)
                    driver.switch_to.default_content()
                    result_row = _report_row(cliente, "FALLO", "No se encontro el campo de contrasena")
                    return result_row
                pwd_field.clear()
                pwd_field.send_keys(pwd)

                # Resolver CAPTCHA
                captcha_info = _resolver_captcha(
                    driver,
                    cliente,
                    dataset_dir,
                    captcha_mode,
                    captcha_key,
                    tc_user,
                    logger,
                )
                if not captcha_info.get("ok"):
                    driver.switch_to.default_content()
                    result_row = _report_row(cliente, "FALLO", captcha_info.get("detalle", "Captcha"))
                    return result_row

                _ensure_login_form_values(
                    driver,
                    rfc,
                    pwd,
                    captcha_info.get("captcha_text", ""),
                    logger,
                )
                submitted = _submit_login_form(driver)

                driver.switch_to.default_content()

                if not submitted:
                    logger.error("[%s] No se encontro el boton de login en el iframe", rfc)
                    result_row = _report_row(cliente, "FALLO", "No se encontro el boton Enviar")
                    return result_row

                current_url = driver.current_url
                current_title = (driver.title or "").strip()
                logger.info("[%s] Post-submit URL: %s | title='%s'", rfc, current_url, current_title)

                # Verificar login exitoso con multiples señales (URL, contexto autenticado o errores explícitos)
                status, detalle = _wait_for_login_outcome(driver, timeout=max(timeout, 35))
                if status != "ok":
                    shot = debug_dir / f"error_login_postsubmit_{rfc}_{int(time.time())}.png"
                    try:
                        driver.save_screenshot(str(shot))
                        logger.info("[%s] Screenshot login outcome: %s", rfc, shot.name)
                    except Exception:
                        pass
                    _dump_debug_page(driver, debug_dir, "login_failure", rfc, logger)

                    if status == "credenciales":
                        logger.error("[%s] Credenciales incorrectas", rfc)
                        result_row = _report_row(cliente, "FALLO", "Credenciales incorrectas")
                        return result_row
                    if status == "captcha":
                        logger.error("[%s] CAPTCHA invalido", rfc)
                        result_row = _report_row(cliente, "FALLO", "CAPTCHA invalido")
                        return result_row
                    logger.error("[%s] Login no completo: %s", rfc, detalle)
                    result_row = _report_row(cliente, "FALLO", detalle)
                    return result_row

                logger.info("[%s] Login SAT exitoso: %s", rfc, detalle)
        else:
            logger.info("[%s] Sesion ya activa, saltando login", rfc)

        # 3. Resolver la mejor ruta hacia constancia (iframe o pagina directa)
        target_url, target_detail = _resolve_constancia_target_url(driver, timeout, rfc, logger)
        if not target_url:
            logger.error("[%s] No se pudo resolver la ruta de constancia: %s", rfc, target_detail)
            shot = debug_dir / f"error_{rfc}_{int(time.time())}.png"
            driver.save_screenshot(str(shot))
            logger.info("[%s] Screenshot: %s", rfc, shot.name)
            result_row = _report_row(cliente, "FALLO", f"No se resolvio ruta de constancia: {target_detail}")
            return result_row

        logger.info("[%s] Ruta constancia seleccionada: %s (%s)", rfc, target_url, target_detail)
        if driver.current_url != target_url:
            driver.get(target_url)

        ready, ready_detail, downloaded_pdf = _wait_for_constancia_ready(
            driver,
            timeout,
            rfc,
            logger,
            pdf_dir=pdf_dir,
            known_pdf_files=known_pdf_files,
        )
        if not ready:
            logger.error("[%s] La constancia no cargo correctamente: %s", rfc, ready_detail)
            shot = debug_dir / f"error_constancia_{rfc}_{int(time.time())}.png"
            driver.save_screenshot(str(shot))
            logger.info("[%s] Screenshot constancia: %s", rfc, shot.name)
            _dump_debug_page(driver, debug_dir, "error_constancia", rfc, logger)
            result_row = _report_row(cliente, "FALLO", ready_detail)
            return result_row
        logger.info("[%s] Constancia lista para exportar: %s", rfc, ready_detail)

        safe_rfc = re.sub(r"[^A-Za-z0-9_\-]", "_", rfc)
        dest = pdf_dir / f"constancia_{safe_rfc}.pdf"
        if downloaded_pdf is not None:
            if downloaded_pdf != dest:
                if dest.exists():
                    dest.unlink()
                downloaded_pdf.replace(dest)
            logger.info("[%s] PDF guardado: %s", rfc, dest.name)
            result_row = _report_row(cliente, "OK", "Descarga completada", str(dest))
            return result_row

        if _is_not_found_page(driver):
            logger.error("[%s] La ruta de constancia termino en pagina no encontrada: %s", rfc, driver.current_url)
            shot = debug_dir / f"error_{rfc}_{int(time.time())}.png"
            driver.save_screenshot(str(shot))
            logger.info("[%s] Screenshot: %s", rfc, shot.name)
            result_row = _report_row(cliente, "FALLO", "Pagina no encontrada al abrir constancia")
            return result_row

        if "iniciar-sesion" in driver.current_url:
            logger.error("[%s] Se perdio la sesion al navegar a constancia.", rfc)
            result_row = _report_row(cliente, "FALLO", "Sesion expirada al abrir constancia")
            return result_row

        if _looks_like_constancia_context(driver):
            logger.info("[%s] Contexto SAT de constancia detectado en pagina actual.", rfc)
        else:
            logger.warning(
                "[%s] La pagina destino no coincide claramente con constancia. URL actual: %s",
                rfc,
                driver.current_url,
            )

        # 4. Generar PDF con Chrome DevTools Protocol (printToPDF)
        pre_pdf_shot = debug_dir / f"pre_pdf_{rfc}_{int(time.time())}.png"
        try:
            driver.save_screenshot(str(pre_pdf_shot))
            logger.info("[%s] Screenshot pre-PDF: %s", rfc, pre_pdf_shot.name)
        except Exception:
            pass
        _dump_debug_page(driver, debug_dir, "pre_pdf", rfc, logger)

        pdf_bytes = b""
        pdf_detail = "No se intento generar PDF"
        try:
            for media in ("screen", "print"):
                pdf_candidate = _generate_pdf_with_media(driver, media)
                ok_pdf, pdf_detail = _pdf_has_meaningful_content(pdf_candidate)
                logger.info("[%s] Validacion PDF media=%s: %s", rfc, media, pdf_detail)
                if ok_pdf:
                    pdf_bytes = pdf_candidate
                    logger.info("[%s] PDF valido generado usando media=%s", rfc, media)
                    break
                debug_pdf = debug_dir / f"invalid_{media}_{rfc}_{int(time.time())}.pdf"
                debug_pdf.write_bytes(pdf_candidate)
                logger.warning("[%s] PDF invalido guardado para debug: %s", rfc, debug_pdf.name)
            try:
                driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": ""})
            except Exception:
                pass
        except Exception as exc:
            logger.error("[%s] Error al generar PDF via CDP: %s", rfc, exc)
            shot = debug_dir / f"error_{rfc}_{int(time.time())}.png"
            driver.save_screenshot(str(shot))
            result_row = _report_row(cliente, "FALLO", f"Error al generar PDF: {exc}")
            return result_row

        if not pdf_bytes:
            logger.error("[%s] Se genero un PDF vacio o invalido: %s", rfc, pdf_detail)
            result_row = _report_row(cliente, "FALLO", pdf_detail)
            return result_row

        dest.write_bytes(pdf_bytes)
        logger.info("[%s] PDF guardado: %s", rfc, dest.name)
        result_row = _report_row(cliente, "OK", "Descarga completada", str(dest))
        return result_row

    except WebDriverException as exc:
        logger.error("[%s] WebDriver error: %s", rfc, exc)
        result_row = _report_row(cliente, "FALLO", f"WebDriver error: {exc}")
        return result_row

    finally:
        if captcha_info.get("had_captcha") and captcha_info.get("captcha_path"):
            _append_captcha_dataset_row(
                dataset_dir,
                {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "rfc": cliente["rfc"],
                    "cliente": cliente["nombre"],
                    "modo": captcha_info.get("mode", captcha_mode),
                    "captcha_path": captcha_info.get("captcha_path", ""),
                    "captured_text": captcha_info.get("captcha_text", ""),
                    "resultado": result_row.get("resultado", "FALLO"),
                    "detalle": result_row.get("detalle", captcha_info.get("detalle", "")),
                },
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Descarga masiva de Constancias de Situacion Fiscal del SAT Mexico"
    )
    parser.add_argument("--csv",      required=True,             help="CSV con columnas: rfc, password [, nombre]")
    parser.add_argument("--output",   default="./constancias",   help="Carpeta raiz de salidas (pdfs/, reports/, debug/)")
    parser.add_argument("--headless", action="store_true",        help="Ejecutar sin ventana del navegador")
    parser.add_argument("--browser",  default="chrome",           choices=["chrome"])
    parser.add_argument(
        "--chrome-binary",
        default=os.getenv("CHROME_BINARY", ""),
        help="Ruta al ejecutable de Chrome (opcional). Tambien se puede usar env CHROME_BINARY.",
    )
    parser.add_argument("--delay",    type=float, default=4,      help="Segundos entre clientes")
    parser.add_argument("--timeout",  type=int,   default=30,     help="Timeout por cliente en segundos")
    parser.add_argument("--retries",       type=int,   default=2,      help="Reintentos por cliente fallido")
    parser.add_argument("--log",           default="sat_batch.log",    help="Ruta del archivo de log")
    parser.add_argument("--dry-run",       action="store_true",        help="Valida el CSV sin navegar")
    parser.add_argument(
        "--captcha-mode",
        default="manual",
        choices=["manual", "ocr"],
        help="Modo de CAPTCHA. 'manual' es el flujo recomendado para despacho; 'ocr' mantiene los intentos automaticos.",
    )
    parser.add_argument(
        "--captcha-dataset",
        default="",
        help="Carpeta donde se guardan las imagenes y etiquetas reales del CAPTCHA. Si se omite, usa <output>/captcha_dataset.",
    )
    parser.add_argument("--captcha-key",  default="",
                        help="API key de CapSolver (capsolver.com) — pago.")
    parser.add_argument("--gemini-key",   default="",
                        help="API key de Google Gemini (GRATIS en aistudio.google.com, "
                             "1500 requests/dia sin tarjeta de credito).")
    parser.add_argument(
        "--keep-open-on-fail",
        action="store_true",
        help="No cierra Chrome al final si hubo errores (debug visual).",
    )
    parser.add_argument(
        "--reuse-session",
        action="store_true",
        help="Reutiliza sesion de Chrome entre corridas para debug de un solo cliente y evitar CAPTCHAs repetidos.",
    )
    parser.add_argument(
        "--chrome-profile-dir",
        default="./.sat_chrome_profile",
        help="Carpeta del perfil Chrome cuando se usa --reuse-session.",
    )
    args = parser.parse_args()

    output_layout = _resolve_output_layout(Path(args.output), args.captcha_dataset)
    pdf_dir = output_layout["pdfs"]
    debug_dir = output_layout["debug"]
    report_dir = output_layout["reports"]
    dataset_dir = output_layout["captcha_dataset"]
    logger = setup_logging(args.log)

    logger.info("=" * 60)
    logger.info("SAT Batch Downloader  |  %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    logger.info("=" * 60)

    clientes = load_csv(args.csv, logger)
    logger.info("Clientes cargados: %d", len(clientes))

    if args.dry_run:
        logger.info("--dry-run activo. Validacion completada sin navegacion.")
        for c in clientes:
            logger.info("  RFC: %-16s  Nombre: %s", c["rfc"], c["nombre"])
        sys.exit(0)

    if args.captcha_mode == "manual" and args.headless:
        logger.error("El modo manual de CAPTCHA requiere navegador visible. Quita --headless.")
        sys.exit(1)

    report_rows = []
    driver    = None

    try:
        driver = build_driver(
            args.browser,
            args.headless,
            str(pdf_dir),
            logger,
            args.chrome_binary,
            args.chrome_profile_dir if args.reuse_session else "",
        )

        if args.headless and args.captcha_mode != "manual":
            logger.warning(
                "ADVERTENCIA: Modo --headless activo. Si el SAT muestra CAPTCHA, "
                "el OCR puede fallar. Ejecuta sin --headless y usa --captcha-mode manual si hay fallas."
            )
        if args.captcha_mode == "manual":
            logger.info("Modo CAPTCHA manual activo. El operador solo captura el CAPTCHA; el resto del flujo es automatico.")
            logger.info("Dataset real de CAPTCHA: %s", dataset_dir.resolve())

        for idx, cliente in enumerate(clientes, 1):
            rfc = cliente["rfc"]
            logger.info("[%d/%d] Procesando: %s (%s)", idx, len(clientes), rfc, cliente["nombre"])

            result = _report_row(cliente, "FALLO", "No se proceso")
            for attempt in range(1, args.retries + 1):
                if attempt > 1:
                    logger.info("[%s] Reintento %d/%d", rfc, attempt, args.retries)
                    time.sleep(3)
                result = descargar_constancia(
                    driver,
                    cliente,
                    pdf_dir,
                    debug_dir,
                    args.timeout,
                    args.captcha_mode,
                    dataset_dir,
                    args.captcha_key,
                    args.gemini_key,
                    logger,
                )
                if result["resultado"] == "OK":
                    break
                if attempt < args.retries:
                    _reset_sat_session(driver, logger)

            report_rows.append(result)
            if result["resultado"] == "OK":
                logger.info("[%s] OK | %s", rfc, result["pdf_path"])
            else:
                logger.warning("[%s] FALLO | %s", rfc, result["detalle"])

            if idx < len(clientes) or not args.reuse_session:
                _reset_sat_session(driver, logger)
            elif args.reuse_session:
                logger.info("[%s] Sesion Chrome preservada para la siguiente corrida.", rfc)

            if idx < len(clientes):
                time.sleep(args.delay)

    except KeyboardInterrupt:
        logger.warning("Proceso interrumpido por el usuario.")
    finally:
        if driver:
            has_failures = any(row.get("resultado") != "OK" for row in report_rows)
            if args.keep_open_on_fail and has_failures and not args.headless:
                logger.warning("Chrome se mantiene abierto por --keep-open-on-fail (hubo fallos).")
            else:
                driver.quit()

    # Resumen
    logger.info("")
    logger.info("=" * 60)
    ok_count = sum(1 for row in report_rows if row["resultado"] == "OK")
    fail_rows = [row for row in report_rows if row["resultado"] != "OK"]
    logger.info("RESUMEN  |  Total: %d  OK: %d  Fallidos: %d",
                len(clientes), ok_count, len(fail_rows))
    for row in fail_rows:
        logger.warning("  FALLO: %s | %s", row["rfc"], row["detalle"])

    # Reporte CSV
    report_path = report_dir / f"reporte_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["rfc", "nombre", "fila_csv", "resultado", "detalle", "pdf_path"],
        )
        writer.writeheader()
        writer.writerows(report_rows)
    logger.info("Reporte: %s", report_path)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
