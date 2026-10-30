"""
Diagnóstico de un token de Mercado Libre contra el 403 de facturas (PolicyAgent).
Solo lectura: 3 GET. Edita las variables de abajo y corre: python test_ml_token_permisos.py
No dejes el token guardado en el archivo al hacer commit.
"""
import requests

# ── Variables ──────────────────────────────────────────────────
ML_TOKEN        = ""                    # token de ML a probar (Oficiales o ventas)
ORDER_ID        = "2000018515754174"    # orden de la tienda a probar
EXPECTED_SELLER = "160190870"           # 160190870 Oficiales | 25523702 ventas
# ───────────────────────────────────────────────────────────────

API = "https://api.mercadolibre.com"
headers = {"Authorization": f"Bearer {ML_TOKEN}"}


def get(path):
    return requests.get(f"{API}{path}", headers=headers, timeout=15)


# 1) El token tiene formato APP_USR-<app_id>-<fecha>-<hash>-<user_id>
parts = ML_TOKEN.split("-")
app_id, token_user = (parts[1], parts[-1]) if len(parts) >= 5 else ("?", "?")
token_stamp = parts[2] if len(parts) >= 5 else "?"   # segmento de fecha de emisión (parece MMDDHH); cambia con cada token nuevo
print(f"1) app_id: {app_id} | user_id del token: {token_user} | coincide con {EXPECTED_SELLER}: {token_user == EXPECTED_SELLER} | emisión: {token_stamp}")

# 2) Cuenta a la que ML dice que pertenece el token
r = get("/users/me")
print(f"2) /users/me -> HTTP {r.status_code}", end=" ")
print(f"id={r.json().get('id')} nickname={r.json().get('nickname')}" if r.ok else r.text[:150])

# 3) Acceso a órdenes (lo que usa guías)
r = get(f"/orders/{ORDER_ID}")
print(f"3) /orders/{ORDER_ID} -> HTTP {r.status_code}", "" if r.ok else r.text[:150])

# 4) Acceso a facturas (lo que falla)
r = get(f"/invoices/io/documents/stream/order/{ORDER_ID}/xml")
print(f"4) /invoices/.../xml -> HTTP {r.status_code} {r.text[:200] if not r.ok else ''}")

print("\n2 y 3 = 200 con 4 = 403 -> falta permiso de Facturación en la app (activar y reautorizar). "
      "4 = 404 -> permiso OK, la orden aún no tiene factura.")
