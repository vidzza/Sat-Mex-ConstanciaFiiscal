#!/usr/bin/env python3
"""
constancia_sat_batch.py
Descarga masiva de Constancias de Situacion Fiscal del SAT Mexico.

Uso:
    python constancia_sat_batch.py --csv clientes.csv
    python constancia_sat_batch.py --csv clientes.csv --output ./pdfs --headless --delay 5
    python constancia_sat_batch.py --csv clientes.csv --dry-run
"""

import argparse
import csv
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.firefox.service import Service as FirefoxService
    from selenium.webdriver.edge.service import Service as EdgeService
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException, WebDriverException
    )
except ImportError:
    sys.exit("Error: instala las dependencias con  pip install -r requirements.txt")

try:
    from webdriver_manager.chrome import ChromeDriverManager
    from webdriver_manager.firefox import GeckoDriverManager
    from webdriver_manager.microsoft import EdgeChromiumDriverManager
except ImportError:
    sys.exit("Error: instala las dependencias con  pip install -r requirements.txt")


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
SAT_LOGIN_URL = "https://wwwmat.sat.gob.mx/personas/iniciar-sesion"
SAT_CONSTANCIA_URL = (
    "https://wwwmat.sat.gob.mx/aplicacion/53027/"
    "genera-tu-constancia-de-situacion-fiscal"
)

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


# ---------------------------------------------------------------------------
# WebDriver
# ---------------------------------------------------------------------------
def build_driver(browser: str, headless: bool, download_dir: str):
    abs_dl = str(Path(download_dir).resolve())

    if browser == "chrome":
        opts = webdriver.ChromeOptions()
        if headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1366,768")
        prefs = {
            "download.default_directory": abs_dl,
            "download.prompt_for_download": False,
            "plugins.always_open_pdf_externally": True,
        }
        opts.add_experimental_option("prefs", prefs)
        return webdriver.Chrome(
            service=ChromeService(ChromeDriverManager().install()), options=opts
        )

    if browser == "firefox":
        opts = webdriver.FirefoxOptions()
        if headless:
            opts.add_argument("--headless")
        profile = webdriver.FirefoxProfile()
        profile.set_preference("browser.download.folderList", 2)
        profile.set_preference("browser.download.dir", abs_dl)
        profile.set_preference(
            "browser.helperApps.neverAsk.saveToDisk", "application/pdf"
        )
        profile.set_preference("pdfjs.disabled", True)
        return webdriver.Firefox(
            service=FirefoxService(GeckoDriverManager().install()),
            options=opts,
            firefox_profile=profile,
        )

    if browser == "edge":
        opts = webdriver.EdgeOptions()
        if headless:
            opts.add_argument("--headless=new")
        prefs = {
            "download.default_directory": abs_dl,
            "download.prompt_for_download": False,
            "plugins.always_open_pdf_externally": True,
        }
        opts.add_experimental_option("prefs", prefs)
        return webdriver.Edge(
            service=EdgeService(EdgeChromiumDriverManager().install()), options=opts
        )

    raise ValueError(f"Navegador no soportado: {browser}")


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
def descargar_constancia(
    driver, cliente: dict, output_dir: str, timeout: int, logger: logging.Logger
) -> bool:
    rfc = cliente["rfc"]
    pwd = cliente["password"]
    wait = WebDriverWait(driver, timeout)

    try:
        # 1. Login
        driver.get(SAT_LOGIN_URL)
        wait.until(EC.presence_of_element_located((By.ID, "rfc")))
        driver.find_element(By.ID, "rfc").clear()
        driver.find_element(By.ID, "rfc").send_keys(rfc)
        driver.find_element(By.ID, "password").clear()
        driver.find_element(By.ID, "password").send_keys(pwd)

        submitted = False
        for sel in ["#btnLogin", 'button[type="submit"]', 'input[type="submit"]']:
            try:
                driver.find_element(By.CSS_SELECTOR, sel).click()
                submitted = True
                break
            except NoSuchElementException:
                continue
        if not submitted:
            logger.error("[%s] No se encontro el boton de login", rfc)
            return False

        # 2. Verificar login exitoso
        try:
            wait.until(lambda d: "iniciar-sesion" not in d.current_url)
        except TimeoutException:
            page = driver.page_source.lower()
            if any(x in page for x in ("rfc o contrase", "datos incorrectos", "incorrect")):
                logger.error("[%s] Credenciales incorrectas", rfc)
            else:
                logger.error("[%s] Login no completo (posible CAPTCHA o portal no disponible)", rfc)
            return False

        # 3. Navegar a constancia
        before_files = set(Path(output_dir).glob("*.pdf"))
        driver.get(SAT_CONSTANCIA_URL)
        time.sleep(2)

        # 4. Intentar hacer clic en boton de generacion/descarga
        btn_selectors = [
            "#btnEjecutar",
            'a[href*="constancia"]',
            'button[id*="Generar"]',
            'a.btn-ejecutar',
            'input[value*="Generar"]',
            'a[title*="constancia"]',
            "#constancia",
            'button[onclick*="pdf"]',
            'a[href*=".pdf"]',
            "#btnDescargar",
            'button[title*="PDF"]',
            "a.btn-pdf",
        ]
        for sel in btn_selectors:
            try:
                elem = driver.find_element(By.CSS_SELECTOR, sel)
                driver.execute_script("arguments[0].click();", elem)
                time.sleep(1)
                break
            except NoSuchElementException:
                continue

        time.sleep(2)

        # 5. Esperar PDF y renombrar
        downloaded = wait_for_download(output_dir, before_files, timeout)
        if downloaded:
            safe_rfc = re.sub(r"[^A-Za-z0-9_\-]", "_", rfc)
            dest = Path(output_dir) / f"constancia_{safe_rfc}.pdf"
            Path(downloaded).rename(dest)
            logger.info("[%s] Descargado: %s", rfc, dest.name)
            return True

        # Guardar screenshot como evidencia
        shot = Path(output_dir) / f"error_{rfc}_{int(time.time())}.png"
        driver.save_screenshot(str(shot))
        logger.warning("[%s] PDF no detectado. Screenshot: %s", rfc, shot.name)
        return False

    except WebDriverException as exc:
        logger.error("[%s] WebDriver error: %s", rfc, exc)
        return False

    finally:
        try:
            driver.get("https://wwwmat.sat.gob.mx/personas/cerrar-sesion")
            time.sleep(1)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Descarga masiva de Constancias de Situacion Fiscal del SAT Mexico"
    )
    parser.add_argument("--csv",      required=True,             help="CSV con columnas: rfc, password [, nombre]")
    parser.add_argument("--output",   default="./constancias",   help="Carpeta destino de PDFs")
    parser.add_argument("--headless", action="store_true",        help="Ejecutar sin ventana del navegador")
    parser.add_argument("--browser",  default="chrome",           choices=["chrome", "firefox", "edge"])
    parser.add_argument("--delay",    type=float, default=4,      help="Segundos entre clientes")
    parser.add_argument("--timeout",  type=int,   default=30,     help="Timeout por cliente en segundos")
    parser.add_argument("--retries",  type=int,   default=2,      help="Reintentos por cliente fallido")
    parser.add_argument("--log",      default="sat_batch.log",    help="Ruta del archivo de log")
    parser.add_argument("--dry-run",  action="store_true",        help="Valida el CSV sin navegar")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
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

    ok_list   = []
    fail_list = []
    driver    = None

    try:
        driver = build_driver(args.browser, args.headless, str(output_dir))

        for idx, cliente in enumerate(clientes, 1):
            rfc = cliente["rfc"]
            logger.info("[%d/%d] Procesando: %s (%s)", idx, len(clientes), rfc, cliente["nombre"])

            success = False
            for attempt in range(1, args.retries + 1):
                if attempt > 1:
                    logger.info("[%s] Reintento %d/%d", rfc, attempt, args.retries)
                    time.sleep(3)
                success = descargar_constancia(
                    driver, cliente, str(output_dir), args.timeout, logger
                )
                if success:
                    break

            (ok_list if success else fail_list).append(rfc)

            if idx < len(clientes):
                time.sleep(args.delay)

    except KeyboardInterrupt:
        logger.warning("Proceso interrumpido por el usuario.")
    finally:
        if driver:
            driver.quit()

    # Resumen
    logger.info("")
    logger.info("=" * 60)
    logger.info("RESUMEN  |  Total: %d  OK: %d  Fallidos: %d",
                len(clientes), len(ok_list), len(fail_list))
    for r in fail_list:
        logger.warning("  FALLO: %s", r)

    # Reporte CSV
    report_path = output_dir / f"reporte_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rfc", "resultado"])
        for r in ok_list:
            w.writerow([r, "OK"])
        for r in fail_list:
            w.writerow([r, "FALLO"])
    logger.info("Reporte: %s", report_path)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
