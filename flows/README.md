# Flujos de Kestra (`flows/`)

Esta carpeta es la **fuente de verdad** de los flujos de Kestra de `wb-data-hub`.
Lo que está en `main` es lo que corre en producción: el flujo
[`system/git_sync_flows.yml`](wonderbrands/system/git_sync_flows.yml) lo aplica a Kestra cada 15 minutos.

> **Regla de oro:** los flujos se crean, modifican y eliminan **solo por PR a `main`**.
> Cualquier edición hecha en la UI de Kestra se sobrescribe en ≤ 15 min.

---

## Cómo funciona la sincronización

```
           PR + merge                     cada 15 min (git_sync_flows)
  rama ──────────────▶ main ──────────────────────────────────────────▶ Kestra
                                                                          │
                       kestra-drift ◀──────────── respaldo previo ────────┘
```

Cada corrida de `git_sync_flows` hace, en este orden:

1. **Respaldo** (`backup_*`, tarea `PushFlows`): escribe el estado *actual* de Kestra en la rama
   `kestra-drift` y commitea solo si algo cambió. Si alguien editó un flujo en la UI, esa edición
   queda guardada en Git antes de que el paso 2 la pise.
2. **Sync** (`sync_*`, tarea `SyncFlows`): aplica `main` a Kestra, namespace por namespace.
   - Archivo nuevo en el repo → **crea** el flujo.
   - Archivo modificado → **actualiza** el flujo.
   - Archivo borrado del repo → **elimina** el flujo de Kestra (`delete: true`).

Namespaces administrados por este repo:

| Namespace | Carpeta |
|---|---|
| `wonderbrands.financial.*` | `flows/wonderbrands/financial/` |
| `wonderbrands.shipping_labels.*` | `flows/wonderbrands/shipping_labels/` |
| `wonderbrands.system.*` | `flows/wonderbrands/system/` |

Los demás namespaces de Kestra (`wonderbrands.coffee.*`, `wonderbrands.masters.*`) **viven en otro repo**
y este sync no los toca. No agregues aquí flujos de esos namespaces.

---

## Reglas

1. **Carpeta = namespace.** `wonderbrands.financial.odoo` → `flows/wonderbrands/financial/odoo/`.
2. **Nombre de archivo = `id` del flujo.** `id: coppel_shipping_labels_pipeline` →
   `coppel_shipping_labels_pipeline.yml`.
3. **Un flujo por archivo**, extensión `.yml`.
4. **Nunca editar en la UI de Kestra** flujos de estos namespaces. Para probar, usa el sandbox (ver abajo).
5. **Secrets siempre con `{{ secret('NOMBRE') }}`.** Nunca credenciales en el YAML.
6. Todo flujo productivo lleva un bloque `errors` con alerta a Slack.

---

## Cómo hacer cada cambio

### Modificar un flujo

```bash
git switch main && git pull
git switch -c upd/coppel-cron
# editar flows/wonderbrands/shipping_labels/coppel/coppel_shipping_labels_pipeline.yml
git commit -am "[UPD] Coppel: cron cada 30 min"
git push -u origin upd/coppel-cron
```

Abrir PR → merge. El cambio llega a Kestra en ≤ 15 min.
¿Lo necesitas ya? Ejecuta `wonderbrands.system.git_sync_flows` a mano en la UI.

### Crear un flujo

1. Crear el archivo en la carpeta de su namespace, con nombre = `id`:
   `flows/wonderbrands/financial/odoo/odoo_mi_nuevo_flow.yml`
2. Probarlo en el sandbox (ver abajo).
3. PR → merge.

Plantilla base (patrón de los flujos actuales):

```yaml
id: odoo_mi_nuevo_flow
namespace: wonderbrands.financial.odoo
description: "Qué hace este flujo, en una línea"

concurrency:
  limit: 1

variables:
  repo_url: "https://github.com/wonderbrands/wb-data-hub.git"
  branch: "main"

tasks:
  - id: working_dir
    type: io.kestra.plugin.core.flow.WorkingDirectory
    tasks:
      - id: clone_repo
        type: io.kestra.plugin.git.Clone
        url: "{{ vars.repo_url }}"
        branch: "{{ vars.branch }}"
        username: "{{ secret('GIT_USER') }}"
        password: "{{ secret('GIT_TOKEN_WB') }}"

      - id: run_script
        type: io.kestra.plugin.scripts.shell.Commands
        containerImage: wonderbrands-python:v1
        taskRunner:
          type: io.kestra.plugin.scripts.runner.docker.Docker
          pullPolicy: NEVER
        env:
          LOG_LEVEL: "INFO"
        commands:
          - python processes/wonderbrands/financial/odoo/mi_script.py

triggers:
  - id: cron_diario
    type: io.kestra.plugin.core.trigger.Schedule
    cron: "0 8 * * *"
    timezone: "America/Mexico_City"

errors:
  - id: slack_alert
    type: io.kestra.plugin.slack.notifications.SlackIncomingWebhook
    url: "{{ secret('SLACK_WEBHOOK') }}"
    payload: |
      {
        "text": "🚨 *Fallo en odoo_mi_nuevo_flow* 🚨\n*ID de ejecución:* {{ execution.id }}\n*Fecha:* {{ execution.startDate }}"
      }
```

### Eliminar un flujo

Borrar el archivo y hacer PR → merge. El siguiente sync **elimina el flujo de Kestra**
(sus ejecuciones pasadas no se borran).

```bash
git rm flows/wonderbrands/shipping_labels/mercadolibre/flujo_viejo.yml
```

> ¿Solo quieres pausarlo? No lo borres: agrega `disabled: true` al flujo (o al trigger).

### Renombrar un flujo (cambiar `id`) o moverlo de namespace

Para Kestra es **eliminar el viejo + crear uno nuevo**. Se hace en un solo PR: renombrar/mover el
archivo y cambiar `id`/`namespace` dentro del YAML. El historial de ejecuciones queda con el id viejo.

### Agregar un namespace nuevo (ej. `wonderbrands.marketing`)

1. Crear `flows/wonderbrands/marketing/` con sus flujos.
2. En [`git_sync_flows.yml`](wonderbrands/system/git_sync_flows.yml) agregar el par de tareas
   `backup_marketing` (PushFlows) y `sync_marketing` (SyncFlows), copiando las existentes.
3. Agregar el namespace a la tabla de este README.

Sin el paso 2, los flujos de esa carpeta **no se sincronizan**.

---

## Probar antes del merge (sandbox)

El namespace `wonderbrands.sandbox` **no lo toca el sync**: sirve para pruebas.

1. En la UI de Kestra crear el flujo con `namespace: wonderbrands.sandbox`.
2. Apuntar `variables.branch` a tu rama de trabajo (para que clone tu código sin mergear).
3. Desactivar efectos reales: triggers en `disabled: true`, flags de dry run del script, etc.
4. Ejecutar y revisar.
5. **Borrar el flujo del sandbox** al terminar y hacer el PR con el namespace real y `branch: "main"`.

---

## Revisar si alguien editó en la UI (drift)

Kestra elimina el salto de línea final al guardar, así que se ignora el whitespace al comparar:

```bash
git fetch origin
git log origin/kestra-drift --oneline -10
git diff --ignore-all-space --ignore-blank-lines origin/main origin/kestra-drift -- flows/wonderbrands
```

- Diff vacío → Kestra está igual que `main`.
- Después de cada merge, el siguiente ciclo también genera un commit `[DRIFT]` (Kestra cambió): es normal.
- Un commit `[DRIFT]` con contenido que **no viene de ningún PR** = alguien editó en la UI.
  Para conservar ese cambio, llévalo a `main` por PR (copiando el archivo desde `kestra-drift`).

---

## Emergencias y rollback

- **Revertir un cambio:** `git revert <commit>` → PR → merge. En ≤ 15 min Kestra vuelve a la versión anterior.
  Para aplicarlo al instante, ejecutar `git_sync_flows` a mano.
- **Recuperar un flujo borrado por error:** revertir el PR que lo eliminó (también está en `kestra-drift`).
- **Hotfix urgente en la UI:** solo si no hay otra opción. Haz el cambio en la UI **y** el PR con el
  mismo cambio de inmediato; si no se mergea en ≤ 15 min, el sync lo revierte (queda guardado en `kestra-drift`).
- **Pausar la sincronización:** deshabilitar el trigger `every_15_min` de `git_sync_flows` en la UI
  (la siguiente ejecución manual lo reactivará con lo que diga `main`).

---

## Secrets usados por la sincronización

| Secret | Uso |
|---|---|
| `GIT_USER` | Usuario de GitHub |
| `GIT_TOKEN_WB_ORG` | Token con *Contents: Read and write* en `wb-data-hub` (respaldo + sync) |
| `SLACK_WEBHOOK` | Alerta si la sincronización falla |

En Kestra OSS los secrets se definen en el `.env` del servidor como `SECRET_<NOMBRE>` con el valor en
base64 (`echo -n "valor" | base64 -w 0`) y requieren recrear el contenedor (`docker compose up -d`).
