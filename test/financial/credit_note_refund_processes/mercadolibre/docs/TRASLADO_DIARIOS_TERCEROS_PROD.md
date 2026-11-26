# Traslado de CFDI de MeLi a sus diarios de terceros — PRODUCTIVO oct–nov

Finanzas aprobó la opción (a), el 25-nov: trasladar todo y sanear la numeración. Oct–nov se hace en segundo plano antes de la ventana de reapertura. Jun–sep queda para esa ventana, porque la fecha de bloqueo es el 30-sep.

Script: `traslado_diarios_terceros.py` (esta carpeta). Serie exacta → diario:

| Serie | Tipo | Diario |
|---|---|---|
| MELI | factura | MELI |
| MELINC | NC | MLNC |
| MELIOF | factura | MLOF |
| MELIOFNC | NC | MLONC |

## Candados del script

- `LOCAL_ODOO="prod"` apunta a `odoo_urlV18`. Por defecto corre **en seco** y no escribe nada.
- Para escribir hacen falta `DRY_RUN=false` **y** `CONFIRMAR_PROD="TRASLADAR"`.
- Candado cruzado: prod con URL de pruebas, o staging con URL productiva, aborta.
- Solo toma documentos que cumplan dos cosas:
  - tienen el XML del tercero (`<UUID>.xml`, como lo cargan 02 y 06);
  - tienen la serie exacta de MeLi.

  Un CFDI timbrado por Somos Reyes nunca se regresa a borrador.
- No toca, y deja en el log con su motivo:
  - un documento con fecha ≤ candado de la compañía;
  - un nombre serie-folio ya ocupado en el diario destino;
  - un UUID repetido en dos o más documentos;
  - un UUID ya registrado en un diario de terceros.
- Si un diario de origen tiene hash inalterable, aborta todo.
- Si un documento falla a la mitad, la línea **se detiene**. Dice el documento (id) y el paso; puede haber quedado en borrador.
- `HASTA="HH:MM"` (CDMX): la línea no toma documentos nuevos a partir de esa hora.
- **Para detener antes:** crear `finanzas\DETENER` (detiene todas las líneas al terminar el documento en curso) o Ctrl+C. **Nunca** `Stop-Process`, porque puede dejar un documento en borrador.
- El log por documento para Finanzas (`finanzas\Traslado_prod_<fecha>[_parteXdeN].csv`) se escribe documento por documento. Al final queda un `_resumen.csv` con:
  - conteo por mes y diario;
  - totales por cuenta antes y después;
  - costo distinto;
  - omitidos.

## Variables comunes (cada bloque de PowerShell las vuelve a fijar)

```powershell
$dir = "C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wb-data-hub\test\financial\credit_note_refund_processes\mercadolibre"
$py  = "C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wb-data-hub\.venv\Scripts\python.exe"
Set-Location $dir
```

`$dir` es la copia principal **después de fusionar el PR** (`git pull`). Antes del merge, usar la carpeta del worktree `.claude\worktrees\automation-run-log-review-273c7d\test\...`.

## Paso 1 — Corrida en seco oct–nov (solo lectura, ~3–5 min)

```powershell
$env:LOCAL_ODOO="prod"; $env:DRY_RUN="true"; $env:FROM_JOURNALS="INV,DEV,BON"; $env:DATE_FROM="2026-10-01"; $env:DATE_TO="2026-11-30"; $env:BATCH_LIMIT="0"; Remove-Item Env:PART, Env:HASTA, Env:CONFIRMAR_PROD -ErrorAction SilentlyContinue; & $py traslado_diarios_terceros.py
```

Al final imprime el conteo `mes | origen -> destino | resultado`. Referencia de la copia de staging, del mismo código:

| Mes | Se trasladarían | UUID repetido | Serie `FI` (no es MeLi) | MLOF |
|---|---|---|---|---|
| 2026-10 | 40,116 | 32 | 63 | 30 (sin diario en staging) |
| 2026-11 | 39,065 | — | 92 | 9 (sin diario en staging) |

Qué revisar:
- **UUID repetido:** en staging, 8 SO facturadas **4 veces con el mismo CFDI** el 7-oct a las 16:41, con 5 s de diferencia y solo una pagada; por ejemplo, SO10330967 y SO10330996. Son facturas duplicadas que Finanzas debe cancelar (sobra el ingreso de 3 de cada 4). No se trasladan hasta que quede una por UUID.
- **Serie `FI`:** XML de otro tercero; no se tocan.
- **MLOF / MLONC:** si salen como `OMITIDO sin diario`, falta crear el diario en productivo.
- **DEV / BON:** las NC del piloto deben salir hacia MLNC.

## En Kestra: `ml_traslado_diarios_terceros_flow` (recomendado para los pasos 2 y 3)

El flujo corre el script de `processes/` con 6 líneas en paralelo y deja los CSV y los logs de cada línea como archivos de la ejecución.

- **Paso 2, a mano desde Kestra:** "Execute" con los inputs:
  - **facturas:** `lines=1`, `batch_limit=20`, `series=MELI`, `from_journals=INV`, `date_from=2026-10-01`, `date_to=2026-10-31`;
  - **NC:** `lines=1`, `batch_limit=5`, `series=MELINC`, `from_journals=DEV,BON`, `date_from=2026-10-01`, `date_to=2026-11-30`.

  Revisar el CSV en "Outputs" con el criterio de abajo.
- **Paso 3, automático:** con el VoBo del Paso 2, se quita `disabled: true` del disparador `after_midnight_ml_chain` (PR a `main`).
  - Arranca cuando termina `ml_credit_notes_flow` de la cadena de medianoche y se corta solo a las 05:45 CDMX. Si la cadena termina después de las 02:30, esa noche no trabaja.
  - Usa `series=MELI,MELINC`; Oficiales queda fuera, porque su cadena corre a las 03:00.
  - Cada noche retoma lo que quede.
  - Al terminar oct–nov se vuelve a apagar el disparador.
- **Detener:** la línea termina el documento en curso solo con `HASTA`. "Kill" en Kestra puede dejar un documento en borrador (el log dice cuál); se termina a mano como en "Si una línea sale con FALLO".

Los pasos manuales de abajo siguen sirviendo para correrlo desde la PC.

## Paso 2 — Lote chico real (20 facturas de octubre + 5 NC)

Fuera de los horarios de cobros de MeLi (ver Paso 3). Tarda ~2 min.

```powershell
$env:LOCAL_ODOO="prod"; $env:DRY_RUN="false"; $env:CONFIRMAR_PROD="TRASLADAR"; $env:FROM_JOURNALS="INV"; $env:DATE_FROM="2026-10-01"; $env:DATE_TO="2026-10-31"; $env:BATCH_LIMIT="20"; Remove-Item Env:PART, Env:HASTA -ErrorAction SilentlyContinue; & $py traslado_diarios_terceros.py
```

```powershell
$env:LOCAL_ODOO="prod"; $env:DRY_RUN="false"; $env:CONFIRMAR_PROD="TRASLADAR"; $env:FROM_JOURNALS="DEV,BON"; $env:DATE_FROM="2026-10-01"; $env:DATE_TO="2026-11-30"; $env:BATCH_LIMIT="5"; Remove-Item Env:PART, Env:HASTA -ErrorAction SilentlyContinue; & $py traslado_diarios_terceros.py
```

Criterio para seguir:
- todos los documentos en `OK`;
- `Totales por cuenta` con diferencia 0.00 en todas las cuentas;
- `Costo distinto (REVISAR): 0`.

En Odoo, abrir 2 o 3 documentos y confirmar:
- nombre = serie-folio y diario correcto;
- estado de pago igual que antes;
- CxC conciliada con el cobro;
- 205.06.003 conciliada con la salida (en la NC tipo B, la 107.06.001 con el retorno);
- el XML sigue adjunto.

El chatter registra el nombre anterior.

## Paso 3 — Masivo, de noche, por ventanas sin cobros de MeLi

Horarios en CDMX. Los crons de ML no tienen `timezone` y corren en UTC.

| Hora CDMX | Qué corre | ¿Toca facturas de MeLi? |
|---|---|---|
| 18:00, 00:00, 06:00 | `ml_billing` → `ml_payments` → `ml_credit_notes` (encadenados) | Sí: cobros y NC |
| 21:00, 03:00 | Oficiales billing → payments | Solo Oficiales (~40 docs) |
| 23:00 / 23:30 | `ml_payments` (+NC) / Oficiales payments | Sí |
| 02:30 | Amazon | No (solo carga Odoo) |
| 04:30 y 19:30 | 1a1 | No (excluye ML) |

**Ventana principal: 01:15 → 05:45.** Antes de lanzar, confirmar en Kestra que la cadena de las 00:00 (`ml_billing_process_flow` → `ml_payments_flow` → `ml_credit_notes_flow`) ya terminó.

Ventana opcional, si Finanzas ya no trabaja en Odoo: **19:45 → 22:45**.

El riesgo de traslapar con los cobros: si 04 aplica un cobro en el segundo exacto entre la foto y el borrador, ese cobro queda sin conciliar y el script no lo detecta. Por eso se corre fuera de los cobros.

**Octubre primero:** Finanzas no debe mover la fecha de bloqueo a 31-oct hasta que octubre termine. Si lo hace, el script lo reporta como `PERIODO_CERRADO`.

Lanzar 6 líneas. Cada noche se repite igual, porque el script vuelve a tomar lo que siga en INV/DEV/BON:

```powershell
$env:LOCAL_ODOO="prod"; $env:DRY_RUN="false"; $env:CONFIRMAR_PROD="TRASLADAR"; $env:FROM_JOURNALS="INV,DEV,BON"; $env:DATE_FROM="2026-10-01"; $env:DATE_TO="2026-10-31"; $env:BATCH_LIMIT="0"; $env:HASTA="05:45"; Remove-Item "$dir\finanzas\DETENER" -ErrorAction SilentlyContinue; $hoy = Get-Date -Format yyyyMMdd_HHmm; 0..5 | ForEach-Object { $env:PART="$_/6"; Start-Process -FilePath $py -ArgumentList "traslado_diarios_terceros.py" -WorkingDirectory $dir -WindowStyle Minimized -RedirectStandardOutput "$dir\finanzas\linea_${hoy}_$_.log" -RedirectStandardError "$dir\finanzas\linea_${hoy}_$_.err" }
```

Cuando octubre quede en cero (Paso 1 con `DATE_TO=2026-10-31` ya no da `SE TRASLADARÍA`), repetir con `DATE_FROM="2026-11-01"` y `DATE_TO="2026-11-30"`.

**Ritmo esperado:**
- Una línea va a ~2.1 s por documento en staging. Con 4 líneas, el agregado fue de ~1.3 s.
- Con 6 líneas en productivo, estimo 3,000–4,000 documentos por hora: **~15–18 mil por ventana de 4.5 h**, unas 5 noches para ~80 mil.
- Si la primera noche va bien y el `.err` sale limpio, se puede subir a 8 líneas (`0..7` y `PART="$_/8"`) o sumar la ventana de la tarde.

**Vigilar:**

```powershell
Get-ChildItem "$dir\finanzas\linea_*.log" | Sort-Object LastWriteTime | Select-Object -Last 6 | ForEach-Object { Get-Content $_ -Tail 2 }
```

```powershell
Select-String -Path "$dir\finanzas\Traslado_prod_2*.csv" -Pattern "FALLO|REVISAR" | Select-Object -Last 20
```

```powershell
Get-ChildItem "$dir\finanzas\linea_*.err" | Where-Object Length -gt 0
```

**Detener antes de la hora:**

```powershell
New-Item "$dir\finanzas\DETENER" -ItemType File
```

**Si una línea sale con `FALLO`:**
1. Abrir en Odoo el documento que dice el log (id).
2. Si quedó en borrador o con nombre `/`, terminarlo a mano: diario destino, nombre serie-folio, publicar y conciliar.
3. Relanzar solo esa parte (`PART="i/6"`).

Las demás líneas siguen solas.

## Al terminar

- Repetir el Paso 1 (oct–nov): solo deben quedar omitidos explicados (UUID repetido, serie `FI`).
- Entregar a Finanzas:
  - los `Traslado_prod_*.csv` (log por documento);
  - los `_resumen.csv` (totales por cuenta y omitidos);
  - la lista de UUID repetidos para cancelar.
