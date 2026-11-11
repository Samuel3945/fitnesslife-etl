# ETL Fitness Life

Extrae los datos de MongoDB Atlas, los transforma al modelo estrella y los carga en Azure SQL, desde donde los consume Power BI.

```
MongoDB Atlas  ──►  ETL (Python)  ──►  Azure SQL (Data Warehouse)  ──►  Power BI
                    PyMongo · Pandas · SQLAlchemy
```

## Modelo estrella

- **Hechos:** `FactMemberActivity` (asistencias, pagos, clases y predicciones de abandono, diferenciados por `DimEventType`).
- **Dimensiones:** `DimDate`, `DimTime`, `DimUser`, `DimPlan`, `DimTraining`, `DimPaymentStatus`, `DimChurnRisk`, `DimEventType`.

Colecciones de origen: `users`, `plans`, `payments`, `attendances`, `functionalTrainings`, `churnPredictions`.

## Estructura

```
etl/
├── src/              # Código del ETL (extract, transform, load)
├── sql/              # DDL del Data Warehouse
├── requirements.txt
├── .env.example      # Plantilla de variables de entorno (sin credenciales)
└── README.md
```

## Ejecución local

1. Python 3.12 y el driver ODBC 18 para SQL Server.
2. Crear el entorno virtual e instalar dependencias:
   ```bash
   cd etl
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Copiar `.env.example` a `.env` y completar las credenciales. **`.env` nunca se versiona.**

## Programación

El ETL se ejecuta 4 veces al día (6:00, 12:00, 18:00 y 22:00, hora Colombia) mediante GitHub Actions, y también puede lanzarse manualmente. La frecuencia se cambia editando únicamente la expresión `cron` del workflow.

El proceso es incremental e idempotente: re-ejecutarlo no duplica datos.

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
