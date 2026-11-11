# ETL Fitness Life

Extrae los datos de MongoDB Atlas, los transforma al modelo estrella y los carga en Azure SQL, desde donde los consume Power BI.

```
MongoDB Atlas  ──►  ETL (Python)  ──►  Azure SQL (Data Warehouse)  ──►  Power BI
                    PyMongo · SQLAlchemy · pymssql
```

## Modelo estrella

- **Hechos:** `dw.FactMemberActivity`, con cuatro tipos de evento diferenciados por `DimEventType`:
  - `ATTENDANCE` — una fila por ingreso al gimnasio (`AttendanceMinutes`).
  - `PAYMENT` — una fila por pago (`PaymentAmount`, fechas de vigencia).
  - `CLASS_ENROLLMENT` — una fila por usuario inscrito en una clase funcional.
  - `CHURN_PREDICTION` — una fila por usuario y día con la predicción de abandono.
- **Dimensiones:** `DimDate`, `DimTime`, `DimUser`, `DimPlan`, `DimTraining`, `DimPaymentStatus`, `DimChurnRisk`, `DimEventType`.

Colecciones de origen: `users`, `plans`, `payments`, `attendances`, `functionalTrainings`, `churnPredictions`.

> **Medidas en Power BI:** como hay una sola tabla de hechos, toda medida debe filtrar por `DimEventType[EventType]`.

### Esquemas en Azure SQL

| Esquema | Uso |
|---|---|
| `dw` | Modelo estrella publicado (lo lee Power BI). |
| `stg` | Copia de trabajo que se recarga en cada corrida antes de publicar. |
| `hist` | `ChurnPredictionSnapshot`: una foto por usuario y **fecha de cálculo** de la predicción (`calculatedAt`, no la fecha de corrida del ETL). **Nunca se borra**; si el mismo día hay varios cálculos, gana el último. Si el modelo predictivo no recalcula un día, ese día no tiene foto. |
| `etl` | `LoadLog`: una fila por corrida (éxito o fallo), útil para mostrar la fecha de última actualización. |

## Cómo carga los datos

Cada corrida hace una **recarga completa con publicación atómica**:

1. Extrae las 6 colecciones (solo lectura; nunca trae contraseñas, tokens QR ni firmas de pago).
2. Construye dimensiones y hechos en memoria.
3. Dentro de **una sola transacción**: carga `stg`, valida y reemplaza `dw`. La validación bloquea la publicación si:
   - `users` o `plans` vienen vacías;
   - más del 1 % de los hechos apunta a usuarios o planes inexistentes;
   - los conteos por tipo de evento no coinciden con el origen o hay `SourceEventId` duplicados;
   - la carga nueva tiene menos del 50 % de los hechos ya publicados (`ETL_MIN_PUBLISH_RATIO`).
4. Si algo falla, la transacción se revierte y Power BI sigue viendo los datos anteriores. Azure SQL usa `READ_COMMITTED_SNAPSHOT`, así que las consultas nunca quedan bloqueadas durante la carga.

Re-ejecutar el proceso no duplica datos. Valores numéricos inválidos (NaN) se cargan como vacíos y los textos que exceden su columna se recortan; ambos casos quedan como advertencia en `etl.LoadLog`. Los mensajes de error nunca incluyen valores de filas (correos, documentos, teléfonos).

## Reglas de transformación

| Regla | Detalle |
|---|---|
| Zona horaria | MongoDB guarda instantes en UTC y la aplicación (JVM en hora Colombia) los convierte a hora local al mostrarlos. El ETL hace la misma conversión (`ETL_SOURCE_TIMEZONE`, por defecto `America/Bogota`), verificada contra el dashboard original: ingresos de noviembre $13.790.000 y mapa de calor de accesos idénticos. Los datos de prueba sembrados quedan con ingresos entre la 1:00 y las 15:59. |
| Claves sustitutas | Asignadas por el ETL (1..n). La clave `0` es el miembro "No aplica / Desconocido". |
| `PredictionConfidence` | El origen guarda un porcentaje (0–100); el DW guarda una proporción (0–1). |
| `DimPlan.WeeklyGroupClasses` | Se deriva de los beneficios. `NULL` = clases grupales ilimitadas. "Todo de &lt;Plan&gt;" hereda los beneficios de ese plan. |
| `DimPlan.HasLockers` | Verdadero si algún beneficio (incluidos los heredados) menciona casilleros. |
| `DimPlan.PlanLevel` | Tomado de `churnPredictions.planLevel`; si falta, el orden por precio. |
| `DimTime.IsPeakHour` | Una hora es pico si su cantidad de ingresos está en el cuartil superior de las horas con actividad. |
| `DimDate.IsHoliday` | Festivos de Colombia (librería `holidays`). |
| `DimUser.AgeGroup` | Edad calculada a la fecha de carga. |

## Estructura

```
etl/
├── src/
│   ├── apply_schema.py     # Crea o verifica las tablas (idempotente)
│   ├── run_etl.py          # Ejecuta el ETL completo
│   ├── validate_load.py    # Compara Azure SQL contra MongoDB
│   ├── profile_source.py   # Perfilado de solo lectura del origen
│   ├── config.py · db.py · extract.py · transform.py · load.py
├── sql/
│   └── 001_create_schema.sql
├── requirements.txt
├── .env.example
└── README.md
```

## Ejecución local

1. Python 3.12.
2. Crear el entorno virtual e instalar dependencias:
   ```bash
   cd etl
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Copiar `.env.example` a `.env` y completar las credenciales. **`.env` nunca se versiona.**
4. Crear las tablas (solo la primera vez):
   ```bash
   python src/apply_schema.py
   ```
   El DDL solo crea tablas que no existen: **no modifica columnas de tablas existentes**. Un cambio de columnas requiere un `ALTER TABLE` manual (o un nuevo script `002_*.sql`), aplicado por igual en `dw` y `stg`, y reflejado en `STRING_LIMITS` de `transform.py`.
5. Ejecutar y validar:
   ```bash
   python src/run_etl.py
   python src/validate_load.py
   ```

La conexión a Azure SQL usa `pymssql`, que no requiere instalar el driver ODBC de Microsoft. Si la IP local cambia, debe agregarse en el firewall del servidor de Azure SQL.

## Programación

El workflow [`.github/workflows/etl.yml`](../.github/workflows/etl.yml) ejecuta el ETL y luego `validate_load.py`:

- **Automático:** 6:00, 12:00, 18:00 y 22:00 hora Colombia (`cron: "0 11,17,23,3 * * *"`, en UTC). GitHub puede demorar algunos minutos las ejecuciones programadas.
- **Manual:** pestaña *Actions* → *ETL MongoDB to Azure SQL* → *Run workflow*.
- **Sin solapamiento:** un grupo `concurrency` hace que una corrida manual y una programada nunca se ejecuten a la vez.
- Si la carga o la validación fallan, la corrida queda en rojo y GitHub notifica por correo; los datos publicados anteriormente se conservan.

La frecuencia se cambia editando únicamente la expresión `cron`.

### Secrets requeridos

En *Settings* → *Secrets and variables* → *Actions*: `MONGODB_URI`, `MONGODB_DB`, `AZURE_SQL_SERVER`, `AZURE_SQL_DATABASE`, `AZURE_SQL_USER`, `AZURE_SQL_PASSWORD`. Desde la raíz del repositorio pueden cargarse todos a partir del `.env` local:

```bash
gh secret set -f etl/.env
```

Requisitos de red: el firewall de Azure SQL debe permitir servicios de Azure, y MongoDB Atlas debe aceptar conexiones desde las IP de GitHub Actions (por ejemplo `0.0.0.0/0` en *Network Access*).

## Migración al repositorio principal

Esta carpeta es autocontenida y no depende de ningún otro código del proyecto. Para integrarla al repositorio principal:

1. Copiar la carpeta `etl/` a la raíz del repositorio principal.
2. Copiar `.github/workflows/etl.yml` a `.github/workflows/` del repositorio principal.
3. Registrar los mismos *secrets* del workflow en el repositorio principal.

Para conservar el historial de commits, en lugar de copiar se puede usar `git subtree`:

```bash
git remote add fitnesslife-etl https://github.com/Samuel3945/fitnesslife-etl.git
git fetch fitnesslife-etl
git subtree add --prefix=etl-import fitnesslife-etl main
```

Luego se mueve el contenido de `etl-import/` a su lugar definitivo.
