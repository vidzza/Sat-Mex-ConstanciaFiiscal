# SAT Batch — Descarga masiva de Constancias de Situación Fiscal

Herramienta de línea de comandos para automatizar la descarga de la
Constancia de Situación Fiscal del SAT México para múltiples contribuyentes,
a partir de un archivo CSV.

---

## Requisitos

- Python 3.10 o superior
- Google Chrome instalado
- Conexión a Internet

```bash
pip install -r requirements.txt
```

Si tu Python del sistema bloquea `pip`, usa un entorno virtual:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

---

## Formato del CSV de clientes

El archivo CSV debe contener al menos las columnas `rfc` y `password`.
La columna `nombre` es opcional.

```
rfc,password,nombre
XAXX010101000,MiClave123,Juan Pérez López
VECJ880326XXX,OtraClave!,Empresa SA de CV
```

Exporta el CSV desde Excel usando **Guardar como > CSV UTF-8**.
Si quieres una base editable sin datos reales, usa [clientes_template.csv](/Users/alejandrorodriguezloya/Documents/Github-proyects/constancia_sat_batch/Sat-Mex-ConstanciaFiiscal/clientes_template.csv).

---

## Uso

```bash
# Modo operativo para despacho / contador:
# el sistema llena RFC y contraseña, el operador solo captura el CAPTCHA.
# Recomendado para corridas reales.
.venv/bin/python constancia_sat_batch.py \
  --csv clientes.csv \
  --captcha-mode manual \
  --timeout 60 \
  --retries 1 \
  --delay 0 \
  --keep-open-on-fail \
  --reuse-session \
  --chrome-profile-dir ./.sat_chrome_profile

# OCR experimental del CAPTCHA
.venv/bin/python constancia_sat_batch.py --csv clientes.csv --captcha-mode ocr

# Validar CSV sin navegar
.venv/bin/python constancia_sat_batch.py --csv clientes.csv --dry-run
```

---

## Flags disponibles

| Flag | Tipo | Default | Descripción |
|------|------|---------|-------------|
| `--csv` | string | *(requerido)* | Ruta al CSV de clientes |
| `--output` | string | `./constancias` | Carpeta raíz de salidas; crea `pdfs/`, `reports/`, `debug/` |
| `--headless` | flag | `False` | Ejecutar sin ventana del navegador |
| `--browser` | string | `chrome` | Navegador soportado: `chrome` |
| `--chrome-binary` | string | vacío | Ruta al ejecutable de Chrome (opcional) |
| `--delay` | float | `4` | Segundos de espera entre clientes |
| `--timeout` | int | `30` | Tiempo máximo de espera por cliente (seg) |
| `--retries` | int | `2` | Reintentos en caso de fallo por cliente |
| `--log` | string | `sat_batch.log` | Ruta del archivo de log |
| `--dry-run` | flag | `False` | Solo valida el CSV, no navega |
| `--captcha-mode` | string | `manual` | `manual` para contador, `ocr` como modo experimental |
| `--captcha-dataset` | string | `<output>/captcha_dataset` | Override para la carpeta con imágenes reales y etiquetas capturadas del CAPTCHA |
| `--captcha-key` | string | vacío | API key de CapSolver |
| `--gemini-key` | string | vacío | API key de Gemini |
| `--keep-open-on-fail` | flag | `False` | No cierra Chrome al final cuando hay fallos (debug visual) |
| `--reuse-session` | flag | `False` | Reutiliza el perfil de Chrome entre corridas para depuración operativa |
| `--chrome-profile-dir` | string | `./.sat_chrome_profile` | Carpeta del perfil cuando se usa `--reuse-session` |

---

## Archivos generados

```
constancias/
├── pdfs/
│   ├── constancia_XAXX010101000.pdf
│   └── constancia_VECJ880326XXX.pdf
├── reports/
│   └── reporte_20260407_210000.csv
├── debug/
│   ├── error_GODE561231GR8_1712345678.png
│   ├── pre_pdf_GODE561231GR8_1712345678.png
│   └── invalid_screen_GODE561231GR8_1712345678.pdf
├── captcha_dataset/
│   ├── captchas.csv
│   └── raw/
│       └── 20260408_015902_620189_RFC_DEMO.png
```

---

## Notas

**CAPTCHA**
El modo recomendado hoy es `--captcha-mode manual`.
El script:

1. llena RFC y contraseña
2. guarda la imagen real del CAPTCHA
3. enfoca el campo en el navegador
4. espera a que el operador escriba el CAPTCHA
5. si no detecta el valor escrito, solicita fallback en terminal
6. hace submit y continúa solo

No uses `--headless` con `--captcha-mode manual`.

**Flujo SAT actual**
Después del login, el SAT no entrega la constancia de inmediato. El flujo operativo actual es:

1. abre el trámite
2. entra a la pantalla intermedia
3. activa `Generar Constancia`
4. el SAT abre un popup
5. Chrome descarga el PDF real del SAT
6. el script detecta ese PDF descargado y marca el cliente como `OK`

**Dataset real**
Cada intento de CAPTCHA se guarda en `constancias/captcha_dataset/` por default,
o en la ruta definida con `--captcha-dataset`.
Eso permite construir después un dataset real del SAT para entrenar un solver específico.

**OCR experimental incluido**
El repositorio incluye [sat_captcha_ml.py](/Users/alejandrorodriguezloya/Documents/Github-proyects/constancia_sat_batch/Sat-Mex-ConstanciaFiiscal/sat_captcha_ml.py), [sat_captcha_model.pt](/Users/alejandrorodriguezloya/Documents/Github-proyects/constancia_sat_batch/Sat-Mex-ConstanciaFiiscal/sat_captcha_model.pt) y [sat_captcha_model.meta](/Users/alejandrorodriguezloya/Documents/Github-proyects/constancia_sat_batch/Sat-Mex-ConstanciaFiiscal/sat_captcha_model.meta) como soporte local experimental.
No es el modo recomendado para operación diaria; el flujo recomendado sigue siendo `--captcha-mode manual`.

**Portal cambiante**
El SAT actualiza su portal periódicamente. Si los selectores dejan de funcionar,
inspecciona el HTML con DevTools y actualiza los selectores en la función
`descargar_constancia()` dentro del script.

**Seguridad**
No subas `clientes.csv` a repositorios públicos. Agrega la entrada al `.gitignore`:

```
clientes.csv
clientes_multi_test.csv
constancias/
sat_batch.log
.sat_chrome_profile/
```

**Autenticación**
Esta herramienta utiliza contraseña CIEC. La autenticación por e.firma
(`.key` / `.cer`) no está soportada.

---

## Solución de problemas

| Síntoma | Causa probable | Solución |
|---------|---------------|----------|
| `ChromeDriverException` | Versión de Chrome desactualizada o binario no detectado | Actualiza Chrome o pasa `--chrome-binary /ruta/a/chrome` |
| No aparece el iframe de login | Portal SAT lento o intermitente | Reintenta con timeout mayor, por ejemplo `--timeout 60` |
| Login falla siempre | Credenciales incorrectas | Verifica RFC y contraseña en el portal manualmente |
| Tras login abre “Página no encontrada” | Ruta del trámite cambiante en SAT | Usa la versión actual del script (prueba rutas alternas automáticamente) |
| Al terminar te manda a “Página no encontrada” | Cierre de sesión viejo del SAT | Actualiza al script nuevo (limpia sesión con cookies/storage y evita esa URL) |
| CAPTCHA falla en modo `ocr` | OCR insuficiente para el reto actual | Cambia a `--captcha-mode manual` |
| PDF no se descarga | Selectores CSS desactualizados o sesión incompleta | Observa el flujo sin `--headless` |
| PDF sale vacío o en negro | La página visible o el CSS de impresión del SAT no cargaron bien | Revisa `debug/pre_pdf_*.png` e `invalid_*.pdf`; la corrida ahora debe marcarse como fallo |
| El SAT descarga el PDF pero el script no avanza | Versión antigua del flujo final | Usa la versión actual; ahora detecta el PDF descargado por SAT y termina el cliente |
| Error 503 | Mantenimiento del portal SAT | Reintenta en unos minutos |

---

## Licencia

MIT
