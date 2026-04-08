# SAT Batch — Descarga masiva de Constancias de Situación Fiscal

Herramienta de línea de comandos para automatizar la descarga de la
Constancia de Situación Fiscal del SAT México para múltiples contribuyentes,
a partir de un archivo CSV.

---

## Requisitos

- Python 3.10 o superior
- Google Chrome, Firefox o Edge instalado
- Conexión a Internet

```bash
pip install -r requirements.txt
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

---

## Uso

```bash
# Modo interactivo con ventana visible (recomendado para depuración)
python constancia_sat_batch.py --csv clientes.csv

# Modo producción
python constancia_sat_batch.py --csv clientes.csv --output ./pdfs --headless

# Validar CSV sin navegar
python constancia_sat_batch.py --csv clientes.csv --dry-run
```

---

## Flags disponibles

| Flag | Tipo | Default | Descripción |
|------|------|---------|-------------|
| `--csv` | string | *(requerido)* | Ruta al CSV de clientes |
| `--output` | string | `./constancias` | Carpeta destino de los PDFs |
| `--headless` | flag | `False` | Ejecutar sin ventana del navegador |
| `--browser` | string | `chrome` | Navegador: `chrome`, `firefox` o `edge` |
| `--delay` | float | `4` | Segundos de espera entre clientes |
| `--timeout` | int | `30` | Tiempo máximo de espera por cliente (seg) |
| `--retries` | int | `2` | Reintentos en caso de fallo por cliente |
| `--log` | string | `sat_batch.log` | Ruta del archivo de log |
| `--dry-run` | flag | `False` | Solo valida el CSV, no navega |

---

## Archivos generados

```
constancias/
├── constancia_XAXX010101000.pdf
├── constancia_VECJ880326XXX.pdf
├── error_GODE561231GR8_1712345678.png   # screenshot en caso de fallo
└── reporte_20260407_210000.csv          # resumen con resultado por RFC
```

---

## Notas

**CAPTCHA**
Si el SAT presenta CAPTCHA, ejecuta sin `--headless` y resuélvelo manualmente.
El script continuará tras la interacción.

**Portal cambiante**
El SAT actualiza su portal periódicamente. Si los selectores dejan de funcionar,
inspecciona el HTML con DevTools y actualiza los selectores en la función
`descargar_constancia()` dentro del script.

**Seguridad**
No subas `clientes.csv` a repositorios públicos. Agrega la entrada al `.gitignore`:

```
clientes.csv
constancias/
sat_batch.log
```

**Autenticación**
Esta herramienta utiliza contraseña CIEC. La autenticación por e.firma
(`.key` / `.cer`) no está soportada.

---

## Solución de problemas

| Síntoma | Causa probable | Solución |
|---------|---------------|----------|
| `ChromeDriverException` | Versión de Chrome desactualizada | Actualiza Chrome o usa `--browser firefox` |
| Login falla siempre | Credenciales incorrectas | Verifica RFC y contraseña en el portal manualmente |
| PDF no se descarga | Selectores CSS desactualizados | Quita `--headless` y observa el flujo |
| Error 503 | Mantenimiento del portal SAT | Reintenta en unos minutos |

---

## Licencia

MIT
