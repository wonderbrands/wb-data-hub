import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
import time, os, glob, tempfile, shutil
import pandas as pd
import MySQLdb
import creds1
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from datetime import datetime, timedelta

hoy = datetime.now()
hace_30_dias = hoy - timedelta(days=30)

FECHA_INICIO = hace_30_dias.strftime("%d/%m/%Y")
FECHA_FIN    = hoy.strftime("%d/%m/%Y")
# Carpeta temporal del sistema, se borra al final del script
DOWNLOAD_DIR = tempfile.mkdtemp(prefix="edifact_")

# ── Selenium ──────────────────────────────────────────────────────────────────
def version_chrome():
    """Versión mayor del Chrome instalado (registro de Windows), para bajar el ChromeDriver
    que corresponde: Chrome se actualiza solo y un número fijo deja de funcionar."""
    import winreg
    for raiz in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(raiz, r"Software\Google\Chrome\BLBeacon") as k:
                return int(winreg.QueryValueEx(k, "version")[0].split(".")[0])
        except OSError:
            pass
    return None  # sin dato: undetected_chromedriver elige solo


options = uc.ChromeOptions()
driver = uc.Chrome(options=options, version_main=version_chrome())

espera = WebDriverWait(driver, 30)

# Directo a la pagina de login: el boton INGRESAR de la portada aparece con una animacion y si se clickea
# antes de que se vea no pasa nada (no iniciaba sesion y despues no encontraba el menu).
driver.get("https://edifacturacionsellers.com/login")
espera.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "input[name='username']")))
driver.find_element(By.CSS_SELECTOR, "input[name='username']").send_keys("xxx@xxx.com")
driver.find_element(By.CSS_SELECTOR, "input[type='password']").send_keys("xxx")
driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
try:
    espera.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#sidebar-nav")))
except Exception:
    print("No pudo iniciar sesion (no aparecio el menu). URL:", driver.current_url)
    driver.quit()
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    exit(1)

# Reportes > Reporte CFDI: directo por la URL (antes clicks por posicion en el menu, con esperas fijas)
driver.get("https://edifacturacionsellers.com/reporte/reporteCfdi")
espera.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "#txtInicio")))
# Los campos son de tipo fecha (vienen con valores por defecto): se cargan en su formato, AAAA-MM-DD
INICIO_ISO = hace_30_dias.strftime("%Y-%m-%d")
FIN_ISO    = hoy.strftime("%Y-%m-%d")
for sel, valor in (("#txtInicio", INICIO_ISO), ("#txtFin", FIN_ISO)):
    driver.execute_script("arguments[0].value = arguments[1];", driver.find_element(By.CSS_SELECTOR, sel), valor)
driver.find_element(By.CSS_SELECTOR, "#main > div.card.mb-3 > div > div.col-md-8 > form > div.card-footer.text-body-secondary > button").click()
time.sleep(5)

# ── Polling: buscar en descargas el reporte con estas fechas hasta que este CONSTRUIDA ─────
# (antes miraba solo la 1ra fila y si todavia no aparecia, el script se caia)
FILAS_CSS = "#main > div.card.mb-3 > div > div.col-md-9 > div > div > div.card-body > div > table > tbody > tr"

def buscar_fila():
    for tr in driver.find_elements(By.CSS_SELECTOR, FILAS_CSS):
        celdas = [td.text.strip() for td in tr.find_elements(By.CSS_SELECTOR, "td")]
        if len(celdas) >= 5 and celdas[2].startswith(INICIO_ISO) and celdas[3].startswith(FIN_ISO):
            return tr, celdas[4]
    return None, None

print("Verificando estado del reporte...")
for intento in range(20):
    driver.get("https://edifacturacionsellers.com/reporte/reporteDescarga")
    time.sleep(3)
    fila, estado = buscar_fila()
    print(f"  Intento {intento+1} — Estado: {estado or 'todavia no aparece'}")
    if estado == "CONSTRUIDA":
        break
    time.sleep(30)
else:
    print("❌ El reporte nunca llegó a CONSTRUIDA.")
    driver.quit()
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    exit(1)

# ── Descargar al directorio temporal ──────────────────────────────────────────
driver.execute_cdp_cmd("Browser.setDownloadBehavior", {
    "behavior": "allow",
    "downloadPath": DOWNLOAD_DIR
})

boton = fila.find_element(By.CSS_SELECTOR, "td:nth-child(1) > form:nth-child(1) > button")
driver.execute_script("arguments[0].click();", boton)
print("  Botón clickeado, esperando descarga...")

time.sleep(5)
csv_file = None
for _ in range(60):
    time.sleep(2)
    archivos = [f for f in glob.glob(os.path.join(DOWNLOAD_DIR, "*.csv")) if not f.endswith(".crdownload")]
    if archivos:
        csv_file = max(archivos, key=os.path.getmtime)
        break

if not csv_file:
    print("❌ No se encontró el CSV.")
    driver.quit()
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)
    exit(1)

print(f"✅ Archivo descargado temporalmente")

# ── Eliminar el reporte que acabamos de descargar (de la web) ────────────────
boton_eliminar = fila.find_element(By.CSS_SELECTOR, "td:nth-child(1) > form:nth-child(2) > button")
driver.execute_script("arguments[0].click();", boton_eliminar)
print("  Reporte eliminado de la página.")
time.sleep(2)

driver.quit()

# ── Leer CSV en memoria ────────────────────────────────────────────────────────
df = pd.read_csv(csv_file, sep=",", encoding="latin-1")
df.columns = [c.strip().lower().replace(" ", "_").replace('"', '').replace(' ', '') for c in df.columns]
df = df.loc[:, df.columns != '']
df = df.loc[:, ~df.columns.str.startswith('unnamed')]
df = df.rename(columns={
    'estadoterceros': 'estado_terceros',
    'domiciliofiscalreceptor': 'domicilio_fiscal_receptor',
})
df = df.drop(columns=['rn'], errors='ignore')

fecha_cols = ['fecha_creacion', 'fecha_envio', 'fecha_publicacion', 'fecha_recepcion_tercero', 'error_fecha']
for col in fecha_cols:
    if col in df.columns:
        df[col] = pd.to_datetime(df[col], errors='coerce').dt.strftime('%Y-%m-%d %H:%M:%S')

df = df.applymap(lambda x: x.encode('latin-1').decode('utf-8') if isinstance(x, str) else x)

# ── Borrar la carpeta temporal entera (archivo y carpeta desaparecen) ────────
shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)

# ── Insertar en MySQL ─────────────────────────────────────────────────────────
db = MySQLdb.connect(creds1.host, creds1.user, creds1.passwd, 'somos_reyes', local_infile=True)
cursor = db.cursor()
cols = ", ".join(f"`{c}`" for c in df.columns)
placeholders = ", ".join(["%s"] * len(df.columns))
updates = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in df.columns if c != "uuid")
sql = f"INSERT INTO somos_reyes.wm_fenix_invoices_report ({cols}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}"
rows = [tuple(None if pd.isna(v) else v for v in row) for row in df.itertuples(index=False)]
cursor.executemany(sql, rows)
db.commit()

print(f"✅ {cursor.rowcount} filas insertadas en somos_reyes.wm_fenix_invoices_report")
cursor.close()
db.close()